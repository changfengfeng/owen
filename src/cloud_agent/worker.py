import asyncio
import contextlib
import logging
import signal
import uuid

from cloud_agent.agent import CodingAgent
from cloud_agent.config import Settings, get_settings
from cloud_agent.db import init_db, make_engine, require_schema, sessions
from cloud_agent.queue import TaskQueue
from cloud_agent.sandbox import DockerSandbox, destroy_task

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings: Settings, queue: TaskQueue):
        self.settings, self.queue = settings, queue
        self.id = uuid.uuid4().hex
        self.stopping = asyncio.Event()

    async def execute(self, task):
        sandbox = DockerSandbox(self.settings, task.id)

        async def emit(kind, data, tokens=0):
            if kind != "usage" and self.queue.cancelled(task.id, self.id):
                raise asyncio.CancelledError
            self.queue.record(task.id, self.id, kind, data, tokens)

        async def heartbeat():
            while True:
                await asyncio.sleep(2)
                if not self.queue.heartbeat(task.id, self.id):
                    work.cancel()
                    return

        async def run():
            await emit("setup", {"message": "Creating isolated offline execution environment"})
            await sandbox.start(task.repo_url)
            result = await CodingAgent(self.settings).run(
                task.prompt, sandbox, emit, task.budget_tokens
            )
            patch = await sandbox.diff()
            return result, patch

        work = asyncio.create_task(run())
        keeper = asyncio.create_task(heartbeat())
        status, summary, patch, error = "failed", "", "", ""
        try:
            async with asyncio.timeout(self.settings.task_timeout_seconds):
                result, patch = await work
                summary, status = result.summary, "succeeded"
        except asyncio.CancelledError:
            status, summary = "cancelled", "Task interrupted or cancellation requested"
        except TimeoutError:
            error = "Task exceeded its wall-clock time limit"
        except Exception as exc:
            # Provider payloads and credentials must never be stored in user-visible errors.
            from cloud_agent.agent import AgentError
            from cloud_agent.sandbox import SandboxError

            error = (
                str(exc)[:1000]
                if isinstance(exc, (AgentError, SandboxError))
                else "Worker execution failed"
            )
            log.warning("Task %s failed (%s)", task.id, type(exc).__name__)
        finally:
            keeper.cancel()
            work.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await keeper
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await work
            try:
                await asyncio.shield(sandbox.close())
            except Exception:
                # Leave the lease running: the reaper must destroy the sandbox before settling it.
                log.error("Could not destroy sandbox for task %s; lease retained", task.id)
                return
            self.queue.finish(task.id, self.id, status, summary, patch, error)

    async def reap(self):
        for task in self.queue.expired():
            try:
                if self.queue.begin_reap(task.id):
                    await destroy_task(self.settings, task.id)
                    self.queue.fail_expired(task.id)
            except Exception:
                log.error("Could not reap expired sandbox %s", task.id)

    async def run(self, once: bool = False):
        while not self.stopping.is_set():
            await self.reap()
            task = self.queue.claim(self.id)
            if task:
                await self.execute(task)
            elif not once:
                try:
                    await asyncio.wait_for(self.stopping.wait(), timeout=self.settings.poll_seconds)
                except TimeoutError:
                    pass
            if once:
                break


def main():
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    engine = make_engine(settings)
    if settings.environment == "production":
        require_schema(engine)
    else:
        init_db(engine)
    worker = Worker(settings, TaskQueue(sessions(engine), settings))

    async def run():
        loop = asyncio.get_running_loop()
        current = asyncio.current_task()

        def stop():
            worker.stopping.set()
            if current is not None:
                current.cancel()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop)
        try:
            await worker.run()
        except asyncio.CancelledError:
            pass

    asyncio.run(run())


if __name__ == "__main__":
    main()
