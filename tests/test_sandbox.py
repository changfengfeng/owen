import asyncio
import io
import json
import tarfile
import uuid

import httpx
import pytest

from cloud_agent.config import Settings
from cloud_agent.sandbox import DockerSandbox, SandboxError, repository_archive


def tar_bytes(name, *, symlink=False):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        entry = tarfile.TarInfo(name)
        if symlink:
            entry.type, entry.linkname = tarfile.SYMTYPE, "/etc/passwd"
            archive.addfile(entry)
        else:
            entry.size = 5
            archive.addfile(entry, io.BytesIO(b"hello"))
    return buffer.getvalue()


@pytest.mark.parametrize(
    "path,symlink", [("repo/../../escape", False), ("repo/.git/config", False), ("repo/link", True)]
)
async def test_archive_rejects_unsafe_entries(monkeypatch, path, symlink):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: original(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, content=tar_bytes(path, symlink=symlink))
            ),
            **kw,
        ),
    )
    with pytest.raises(SandboxError):
        await repository_archive("https://github.com/owner/repo")


async def test_archive_rewrites_safe_entries(monkeypatch):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: original(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, content=tar_bytes("repo/file.txt"))
            ),
            **kw,
        ),
    )
    data = await repository_archive("https://github.com/owner/repo")
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        member = archive.getmembers()[0]
        assert member.name == "file.txt" and member.uid == 1000


@pytest.mark.integration
@pytest.mark.parametrize("runtime", ["runc", "runsc"])
async def test_real_container_isolation_and_patch(runtime):
    settings = Settings(sandbox_runtime=runtime, allow_insecure_sandbox=runtime == "runc")
    sandbox = DockerSandbox(settings, uuid.uuid4().hex)
    try:
        await sandbox.start()
        result = await sandbox.execute(
            'id -u; test ! -e /var/run/docker.sock; test -z "$CLOUD_LLM_API_KEY"; test -z "$LLM_API_KEY"; test ! -e /home/ubuntu; echo isolated'
        )
        assert result["exit_code"] == 0 and "1000\nisolated" in result["output"]
        network = await sandbox.execute(
            "python -c \"import socket; s=socket.socket(); s.settimeout(1); s.connect(('1.1.1.1',80))\""
        )
        assert network["exit_code"] != 0
        assert (await sandbox.write_file("/etc/escape", "x"))["exit_code"] != 0
        assert (await sandbox.write_file("/workspace/hello.py", "print('hello')\n"))[
            "exit_code"
        ] == 0
        assert "hello" in (await sandbox.read_file("/workspace/hello.py"))["output"]
        await sandbox.execute("git add -A && git commit -qm 'Agent commit'")
        assert "hello.py" in await sandbox.diff()
        inspect = await sandbox._docker("inspect", sandbox.name)
        value = json.loads(inspect["output"])[0]
        config = value["HostConfig"]
        assert config["Runtime"] == runtime
        assert config["NetworkMode"] == "none" and config["ReadonlyRootfs"]
        assert config["CapDrop"] == ["ALL"] and not config["Binds"]
        assert config["PidsLimit"] == 128
    finally:
        await sandbox.close()


@pytest.mark.integration
@pytest.mark.parametrize("runtime", ["runc", "runsc"])
async def test_repository_import_into_readonly_container(monkeypatch, runtime):
    from unittest.mock import AsyncMock

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        entry = tarfile.TarInfo("README")
        entry.size, entry.uid, entry.gid = 5, 1000, 1000
        archive.addfile(entry, io.BytesIO(b"hello"))
    monkeypatch.setattr(
        "cloud_agent.sandbox.repository_archive", AsyncMock(return_value=buffer.getvalue())
    )
    sandbox = DockerSandbox(
        Settings(sandbox_runtime=runtime, allow_insecure_sandbox=runtime == "runc"),
        uuid.uuid4().hex,
    )
    try:
        await sandbox.start("https://github.com/owner/repo")
        assert "hello" in (await sandbox.read_file("/workspace/README"))["output"]
        assert await sandbox.diff() == ""
        await sandbox.write_file("/workspace/README", "updated\n")
        patch = await sandbox.diff()
        assert "-hello" in patch and "+updated" in patch
    finally:
        await sandbox.close()


@pytest.mark.integration
@pytest.mark.parametrize("runtime", ["runc", "runsc"])
async def test_command_timeout_destroys_container(runtime):
    sandbox = DockerSandbox(
        Settings(sandbox_runtime=runtime, allow_insecure_sandbox=runtime == "runc"),
        uuid.uuid4().hex,
    )
    try:
        await sandbox.start()
        with pytest.raises(SandboxError, match="destroyed"):
            await sandbox.execute("sleep 30", timeout=1)
        result = await sandbox._docker("inspect", sandbox.name, check=False)
        assert result["exit_code"] != 0
    finally:
        await sandbox.close()


@pytest.mark.integration
async def test_default_runtime_fails_closed_without_gvisor():
    sandbox = DockerSandbox(Settings(sandbox_runtime="runc"), uuid.uuid4().hex)
    with pytest.raises(SandboxError, match="mandatory"):
        await sandbox.start()


@pytest.mark.integration
@pytest.mark.parametrize("runtime", ["runc", "runsc"])
async def test_output_is_bounded_without_deadlock(runtime):
    sandbox = DockerSandbox(
        Settings(sandbox_runtime=runtime, allow_insecure_sandbox=runtime == "runc"),
        uuid.uuid4().hex,
    )
    try:
        await sandbox.start()
        result = await asyncio.wait_for(sandbox.execute("python -c \"print('x'*1000000)\""), 10)
        assert result["truncated"] and len(result["output"]) < 33_000
    finally:
        await sandbox.close()
