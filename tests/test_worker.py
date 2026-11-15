import asyncio

import pytest

from cloud_agent.models import Membership, Task, Tenant, User
from cloud_agent.worker import Worker


def submit(app):
    with app.state.sessions.begin() as db:
        tenant, user = Tenant(name="test"), User(email="worker@example.com", password_hash="unused")
        db.add_all([tenant, user])
        db.flush()
        db.add(Membership(user_id=user.id, tenant_id=tenant.id))
        tenant_id, user_id = tenant.id, user.id
    return app.state.queue.submit(tenant_id, user_id, "task")


async def test_cancellation_records_inflight_usage(app, monkeypatch):
    task = submit(app)

    class Sandbox:
        def __init__(self, *args):
            pass

        async def start(self, *args):
            pass

        async def close(self):
            pass

    class Agent:
        def __init__(self, *args):
            pass

        async def run(self, prompt, sandbox, emit, budget):
            app.state.queue.cancel(task.tenant_id, task.id)
            await emit("usage", {"tokens": 999}, 999)
            await emit("turn", {}, 0)

    monkeypatch.setattr("cloud_agent.worker.DockerSandbox", Sandbox)
    monkeypatch.setattr("cloud_agent.worker.CodingAgent", Agent)
    await Worker(app.state.settings, app.state.queue).run(once=True)
    finished = app.state.queue.get(task.tenant_id, task.id)
    assert finished.status == "cancelled" and finished.used_tokens == 999
    assert app.state.queue.usage(task.tenant_id)["monthly_tokens"] == 999


async def test_failed_cleanup_does_not_release_quota(app, monkeypatch):
    task = submit(app)

    class Sandbox:
        def __init__(self, *args):
            pass

        async def start(self, *args):
            raise ValueError("failure")

        async def close(self):
            raise ValueError("daemon down")

    monkeypatch.setattr("cloud_agent.worker.DockerSandbox", Sandbox)
    await Worker(app.state.settings, app.state.queue).run(once=True)
    assert app.state.queue.get(task.tenant_id, task.id).status == "running"
    assert app.state.queue.usage(task.tenant_id)["active_tasks"] == 1


@pytest.mark.integration
async def test_real_worker_cancellation_destroys_sandbox(app, monkeypatch):
    task = submit(app)

    class Agent:
        def __init__(self, *args):
            pass

        async def run(self, *args):
            app.state.queue.cancel(task.tenant_id, task.id)
            await asyncio.sleep(10)

    monkeypatch.setattr("cloud_agent.worker.CodingAgent", Agent)
    await Worker(app.state.settings, app.state.queue).run(once=True)
    from cloud_agent.sandbox import DockerSandbox

    result = await DockerSandbox(app.state.settings, task.id)._docker(
        "inspect", "cloud-task-" + task.id, check=False
    )
    assert result["exit_code"] != 0
    with app.state.sessions() as db:
        assert db.get(Task, task.id).status == "cancelled"
