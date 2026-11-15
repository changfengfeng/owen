import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from cloud_agent.models import Membership, Task, Tenant, User, now
from cloud_agent.queue import QueueError


def seed(queue, daily=20, concurrent=2, monthly=500_000):
    with queue.factory.begin() as db:
        tenant = Tenant(
            name="test", daily_tasks=daily, concurrent_tasks=concurrent, monthly_tokens=monthly
        )
        user = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="unused")
        db.add_all([tenant, user])
        db.flush()
        db.add(Membership(user_id=user.id, tenant_id=tenant.id))
        return tenant.id, user.id


def test_quota_and_settlement(app, account):
    queue = app.state.queue
    tenant, user = account["tenant_id"], account["user_id"]
    task = queue.submit(tenant, user, "task")
    claimed = queue.claim("worker")
    assert claimed.id == task.id
    assert queue.heartbeat(task.id, "stranger") is False
    queue.record(task.id, "worker", "usage", {"tokens": 123}, 123)
    assert queue.finish(task.id, "stranger", "succeeded") is False
    assert queue.finish(task.id, "worker", "succeeded", "done", "patch") is True
    assert queue.finish(task.id, "worker", "succeeded") is False
    usage = queue.usage(tenant)
    assert (
        usage["monthly_tokens"] == 123
        and usage["reserved_tokens"] == 0
        and usage["active_tasks"] == 0
    )


def test_daily_and_monthly_limits(app):
    queue = app.state.queue
    tenant, user = seed(queue, daily=1)
    queue.submit(tenant, user, "one")
    with pytest.raises(QueueError, match="Daily"):
        queue.submit(tenant, user, "two")
    tenant, user = seed(queue, monthly=queue.settings.task_token_budget)
    task = queue.submit(tenant, user, "one")
    with pytest.raises(QueueError, match="Monthly"):
        queue.submit(tenant, user, "two")
    queue.cancel(tenant, task.id)
    assert queue.submit(tenant, user, "two")


def test_cancellation_and_lease_reaping(app):
    queue = app.state.queue
    tenant, user = seed(queue, concurrent=1)
    first = queue.submit(tenant, user, "one")
    queue.submit(tenant, user, "two")
    assert queue.claim("worker").id == first.id
    assert queue.claim("other") is None
    queue.cancel(tenant, first.id)
    assert queue.cancelled(first.id, "worker")
    assert queue.finish(first.id, "worker", "succeeded")
    assert queue.get(tenant, first.id).status == "cancelled"
    second = queue.claim("other")
    with queue.factory.begin() as db:
        db.get(Task, second.id).lease_expires_at = now() - 1
    assert queue.begin_reap(second.id)
    assert not queue.heartbeat(second.id, "other")
    assert not queue.finish(second.id, "other", "succeeded")
    assert queue.fail_expired(second.id)
    assert not queue.fail_expired(second.id)
    assert queue.usage(tenant)["monthly_tokens"] == queue.settings.task_token_budget
    assert queue.usage(tenant)["active_tasks"] == 0


def test_healthy_lease_cannot_be_reaped(app):
    queue = app.state.queue
    tenant, user = seed(queue)
    task = queue.submit(tenant, user, "one")
    queue.claim("worker")
    assert not queue.begin_reap(task.id)
    assert queue.heartbeat(task.id, "worker")


def concurrent_quota(queue):
    tenant, user = seed(queue, daily=3, concurrent=2)

    def submit(index):
        try:
            return queue.submit(tenant, user, str(index)).id
        except QueueError:
            return None

    with ThreadPoolExecutor(max_workers=10) as pool:
        admitted = [value for value in pool.map(submit, range(10)) if value]
    assert len(admitted) == 3
    assert queue.usage(tenant)["reserved_tokens"] == 3 * queue.settings.task_token_budget
    with ThreadPoolExecutor(max_workers=10) as pool:
        claimed = [value for value in pool.map(lambda i: queue.claim(f"w{i}"), range(10)) if value]
    assert len(claimed) == 2 and len({task.id for task in claimed}) == 2
    assert queue.usage(tenant)["active_tasks"] == 2


def test_concurrent_sqlite_admission_and_claim(app):
    concurrent_quota(app.state.queue)


def test_concurrent_postgres_admission_and_claim(postgres_queue):
    concurrent_quota(postgres_queue)


def test_scheduler_does_not_starve_other_tenants(app):
    queue = app.state.queue
    first, user = seed(queue, daily=150, concurrent=1, monthly=10_000_000)
    queue.submit(first, user, "running")
    queue.claim("worker")
    for _ in range(105):
        queue.submit(first, user, "pending")
    second, other = seed(queue)
    task = queue.submit(second, other, "must run")
    assert queue.claim("other").id == task.id


def test_scheduler_looks_past_one_hundred_saturated_tenants(app):
    queue = app.state.queue
    for _ in range(101):
        tenant, user = seed(queue, concurrent=1)
        with queue.factory.begin() as db:
            db.get(Tenant, tenant).active_tasks = 1
        queue.submit(tenant, user, "waiting for capacity")
    tenant, user = seed(queue)
    task = queue.submit(tenant, user, "available capacity")
    assert queue.claim("worker").id == task.id


def test_no_cross_tenant_rows_in_event_access(app):
    queue = app.state.queue
    a, user = seed(queue)
    b, _ = seed(queue)
    task = queue.submit(a, user, "secret")
    with pytest.raises(QueueError):
        queue.events(b, task.id)
    with queue.factory() as db:
        assert db.scalar(select(Task).where(Task.id == task.id)).tenant_id == a
