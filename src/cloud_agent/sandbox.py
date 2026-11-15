import asyncio
import io
import json
import os
import tarfile
from pathlib import PurePosixPath

import httpx

from cloud_agent.config import Settings
from cloud_agent.queue import validate_repo


class SandboxError(Exception):
    pass


READ_SCRIPT = """
import pathlib,sys
p=pathlib.Path(sys.argv[1]).resolve()
root=pathlib.Path('/workspace')
if not p.is_relative_to(root): raise ValueError('Path must stay inside /workspace')
if p.is_dir():
 print('\\n'.join(sorted(str(x.relative_to(root)) + ('/' if x.is_dir() else '') for x in p.iterdir())[:500]))
else:
 with p.open('rb') as f: data=f.read(32769)
 print(data[:32768].decode('utf-8',errors='replace'))
 if len(data)>32768: print('[file truncated]')
"""
WRITE_SCRIPT = """
import pathlib,sys
p=pathlib.Path(sys.argv[1]).resolve()
if not p.is_relative_to(pathlib.Path('/workspace')): raise ValueError('Path must stay inside /workspace')
if '.git' in p.parts: raise ValueError('Git metadata is protected')
p.parent.mkdir(parents=True,exist_ok=True)
p.write_text(sys.argv[2])
print('Wrote '+str(p))
"""


async def repository_archive(url: str) -> bytes:
    url = validate_repo(url)
    owner, repository = url.removeprefix("https://github.com/").split("/")
    target = f"https://codeload.github.com/{owner}/{repository}/tar.gz/HEAD"
    compressed = bytearray()
    async with httpx.AsyncClient(timeout=40, follow_redirects=False, trust_env=False) as client:
        async with client.stream("GET", target) as response:
            if response.status_code != 200:
                raise SandboxError("Unable to fetch public GitHub repository")
            async for chunk in response.aiter_bytes():
                compressed.extend(chunk)
                if len(compressed) > 20 * 1024 * 1024:
                    raise SandboxError("Repository archive exceeds 20 MiB")
    result = io.BytesIO()
    total, count = 0, 0
    with tarfile.open(fileobj=io.BytesIO(compressed), mode="r:gz") as source:
        with tarfile.open(fileobj=result, mode="w") as destination:
            for member in source:
                count += 1
                total += member.size
                if count > 10_000 or total > 100 * 1024 * 1024:
                    raise SandboxError("Repository exceeds extraction limits")
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or len(member.name) > 500:
                    raise SandboxError("Unsafe archive path")
                if len(path.parts) <= 1:
                    continue
                relative = PurePosixPath(*path.parts[1:])
                if any(part.casefold() == ".git" for part in relative.parts):
                    raise SandboxError("Git metadata is not accepted")
                if not (member.isfile() or member.isdir()):
                    raise SandboxError(
                        "Symlinks, hardlinks and special archive entries are not accepted"
                    )
                clean = tarfile.TarInfo(str(relative))
                clean.uid = clean.gid = 1000
                clean.mode = 0o755 if member.isdir() or member.mode & 0o111 else 0o644
                clean.type = tarfile.DIRTYPE if member.isdir() else tarfile.REGTYPE
                clean.size = member.size if member.isfile() else 0
                destination.addfile(clean, source.extractfile(member) if member.isfile() else None)
    return result.getvalue()


class DockerSandbox:
    def __init__(self, settings: Settings, task_id: str):
        self.settings, self.task_id = settings, task_id
        self.name = f"cloud-task-{task_id}"
        self.started = False
        self.baseline = ""

    async def _docker(
        self,
        *args: str,
        timeout: float = 30,
        input_data: bytes | None = None,
        limit: int | None = None,
        check: bool = True,
    ) -> dict:
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            in {
                "PATH",
                "HOME",
                "DOCKER_HOST",
                "DOCKER_CONTEXT",
                "DOCKER_TLS_VERIFY",
                "DOCKER_CERT_PATH",
            }
        }
        process = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
            env=env,
        )
        chunks, size, truncated = [], 0, False
        max_bytes = limit or self.settings.max_output_bytes

        async def drain():
            nonlocal size, truncated
            assert process.stdout is not None
            while chunk := await process.stdout.read(8192):
                if size < max_bytes:
                    chunks.append(chunk[: max_bytes - size])
                size += len(chunk)
                truncated = truncated or size > max_bytes

        async def feed():
            if input_data is not None:
                assert process.stdin is not None
                try:
                    process.stdin.write(input_data)
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    process.stdin.close()

        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(drain(), feed(), process.wait())
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        output = b"".join(chunks).decode("utf-8", errors="replace")
        if truncated:
            output += "\n[output truncated]"
        if check and process.returncode:
            raise SandboxError(f"Container operation failed: {output[:1000]}")
        return {"exit_code": process.returncode, "output": output, "truncated": truncated}

    async def start(self, repo_url: str = ""):
        settings = self.settings
        if settings.sandbox_runtime != "runsc" and not (
            settings.environment != "production" and settings.allow_insecure_sandbox
        ):
            raise SandboxError(
                "gVisor is mandatory; an insecure fallback must be explicitly enabled for development"
            )
        info = await self._docker("info", "--format", "{{json .Runtimes}}")
        if settings.sandbox_runtime not in json.loads(info["output"]):
            raise SandboxError(
                f"Required container runtime {settings.sandbox_runtime!r} is not installed"
            )
        archive = await repository_archive(repo_url) if repo_url else None
        await self._docker(
            "create",
            "--name",
            self.name,
            "--runtime",
            settings.sandbox_runtime,
            "--label",
            "cloud-agent.managed=true",
            "--label",
            f"cloud-agent.task={self.task_id}",
            "--read-only",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            str(settings.sandbox_pids),
            "--memory",
            settings.sandbox_memory,
            "--memory-swap",
            settings.sandbox_memory,
            "--cpus",
            str(settings.sandbox_cpus),
            "--user",
            "1000:1000",
            "--tmpfs",
            "/workspace:rw,exec,nosuid,nodev,size=256m,uid=1000,gid=1000,mode=0700",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=64m,uid=1000,gid=1000,mode=0700",
            "--env",
            "HOME=/tmp",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--workdir",
            "/workspace",
            settings.sandbox_image,
            "sleep",
            "infinity",
        )
        self.started = True
        try:
            await self._docker("start", self.name)
            if archive is not None:
                await self._docker(
                    "exec",
                    "-i",
                    self.name,
                    "python",
                    "-c",
                    "import sys,tarfile; "
                    "archive=tarfile.open(fileobj=sys.stdin.buffer,mode='r|'); "
                    "archive.extractall('/workspace',filter='data')",
                    input_data=archive,
                    timeout=60,
                )
            result = await self.execute(
                "git init -q && git config user.email agent@localhost && "
                "git config user.name 'Cloud Agent' && git add -A && "
                "git -c core.hooksPath=/dev/null commit -qm baseline --allow-empty && git rev-parse HEAD",
                30,
            )
            if result["exit_code"]:
                raise SandboxError("Unable to establish repository baseline")
            self.baseline = result["output"].strip()
        except BaseException:
            await self.close()
            raise

    async def execute(self, command: str, timeout: int = 30) -> dict:
        if not self.started:
            raise SandboxError("Sandbox is not running")
        if len(command) > 20_000:
            raise SandboxError("Command exceeds size limit")
        try:
            return await self._docker(
                "exec",
                self.name,
                "sh",
                "-lc",
                command,
                timeout=min(max(timeout, 1), 120),
                check=False,
            )
        except (TimeoutError, asyncio.CancelledError):
            await self.close()
            raise SandboxError("Command interrupted; sandbox destroyed") from None

    async def read_file(self, path: str) -> dict:
        return await self._docker("exec", self.name, "python", "-c", READ_SCRIPT, path, check=False)

    async def write_file(self, path: str, content: str) -> dict:
        if len(content.encode()) > 65_536:
            raise SandboxError("File write exceeds 64 KiB")
        return await self._docker(
            "exec", self.name, "python", "-c", WRITE_SCRIPT, path, content, check=False
        )

    async def diff(self) -> str:
        await self._docker("exec", self.name, "git", "-c", "core.hooksPath=/dev/null", "add", "-A")
        result = await self._docker(
            "exec",
            self.name,
            "git",
            "--no-pager",
            "diff",
            "--cached",
            "--no-ext-diff",
            "--no-textconv",
            "--binary",
            self.baseline,
            "--",
            limit=self.settings.max_patch_bytes,
        )
        if result["truncated"]:
            raise SandboxError("Patch exceeds artifact size limit")
        return result["output"]

    async def close(self):
        result = await self._docker("rm", "-f", self.name, check=False)
        if result["exit_code"] and "No such container" not in result["output"]:
            raise SandboxError("Could not confirm sandbox destruction")
        self.started = False


async def destroy_task(settings: Settings, task_id: str):
    await DockerSandbox(settings, task_id).close()
