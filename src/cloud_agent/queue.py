import json
import re
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import func, or_, select, text

from cloud_agent.config import Settings
from cloud_agent.models import Task, TaskEvent, Tenant, Usage, now

TERMINAL = {"succeeded", "failed", "cancelled"}
REPO = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?\Z")


class QueueError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def validate_repo(url: str) -> str:
    if not url:
        return ""
    match = REPO.fullmatch(url)
    if not match or any(part in {".", ".."} for part in match.groups()):
        raise QueueError("Only public https://github.com/owner/repository URLs are supported", 422)
    return f"https://github.com/{match[1]}/{match[2]}"


class TaskQueue:
    def __init__(self, factory, settings: Settings):
        self.factory, self.settings = factory, settings

    @contextmanager
    def transaction(self):
        with self.factory.begin() as db:
            if db.bind.dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            yield db

    def _tenant(self, db, tenant_id: str):
        tenant = db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
        if tenant is None:
            raise QueueError("Workspace not found", 404)
        return tenant

    def _usage(self, db, tenant_id: str, period: str):
        usage = db.get(Usage, (tenant_id, period))
        if usage is None:
            usage = Usage(tenant_id=tenant_id, period=period, tasks=0, tokens=0, reserved_tokens=0)
            db.add(usage)
            db.flush()
        return usage

    def _event(self, db, task: Task, kind: str, data: dict):
        encoded = json.dumps(data, ensure_ascii=False)
        if len(encoded.encode()) > 100_000:
            raise QueueError("Event exceeds size limit")
        db.add(TaskEvent(task_id=task.id, kind=kind, data=encoded))

    def submit(self, tenant_id: str, user_id: str, prompt: str, repo_url: str = "", key=None):
        repo_url = validate_repo(repo_url)
        prompt = prompt.strip()
        if not 1 <= len(prompt) <= 12_000:
            raise QueueError("Task prompt must contain 1–12000 characters", 422)
        if key is not None and (not key or len(key) > 128):
            raise QueueError("Invalid idempotency key", 422)
        stamp = datetime.now(UTC)
        day, month = stamp.strftime("%Y-%m-%d"), stamp.strftime("%Y-%m")
        with self.transaction() as db:
            tenant = self._tenant(db, tenant_id)
            if key:
                existing = db.scalar(
                    select(Task).where(Task.tenant_id == tenant_id, Task.idempotency_key == key)
                )
                if existing:
                    if existing.prompt != prompt or existing.repo_url != repo_url:
                        raise QueueError("Idempotency key already used for another request")
                    return existing
            daily, monthly = self._usage(db, tenant_id, day), self._usage(db, tenant_id, month)
            budget = self.settings.task_token_budget
            if daily.tasks >= tenant.daily_tasks:
                raise QueueError("Daily task quota exhausted", 429)
            if monthly.tokens + monthly.reserved_tokens + budget > tenant.monthly_tokens:
                raise QueueError(
                    "Monthly token quota exhausted (including queued reservations)", 429
                )
            daily.tasks += 1
            monthly.reserved_tokens += budget
            task = Task(
                tenant_id=tenant_id,
                user_id=user_id,
                prompt=prompt,
                repo_url=repo_url,
                budget_tokens=budget,
                usage_period=month,
                idempotency_key=key,
            )
            db.add(task)
            db.flush()
            self._event(db, task, "queued", {"message": "Task admitted; token budget reserved"})
            return task

    def get(self, tenant_id: str, task_id: str):
        with self.factory() as db:
            task = db.scalar(select(Task).where(Task.id == task_id, Task.tenant_id == tenant_id))
            if task is None:
                raise QueueError("Task not found", 404)
            return task

    def list(self, tenant_id: str, before: int | None = None):
        with self.factory() as db:
            query = select(Task).where(Task.tenant_id == tenant_id)
            if before is not None:
                query = query.where(Task.created_at < before)
            return list(db.scalars(query.order_by(Task.submitted_at_ns.desc()).limit(100)))

    def usage(self, tenant_id: str):
        stamp = datetime.now(UTC)
        with self.factory() as db:
            tenant = db.get(Tenant, tenant_id)
            daily = db.get(Usage, (tenant_id, stamp.strftime("%Y-%m-%d")))
            monthly = db.get(Usage, (tenant_id, stamp.strftime("%Y-%m")))
            return {
                "workspace": tenant.name,
                "daily_tasks": daily.tasks if daily else 0,
                "daily_task_limit": tenant.daily_tasks,
                "active_tasks": tenant.active_tasks,
                "concurrent_task_limit": tenant.concurrent_tasks,
                "monthly_tokens": monthly.tokens if monthly else 0,
                "reserved_tokens": monthly.reserved_tokens if monthly else 0,
                "monthly_token_limit": tenant.monthly_tokens,
                "task_token_budget": self.settings.task_token_budget,
            }

    def claim(self, worker_id: str):
        with self.transaction() as db:
            candidates = db.scalars(
                select(Task.tenant_id)
                .join(Tenant, Tenant.id == Task.tenant_id)
                .where(Task.status == "queued", Tenant.active_tasks < Tenant.concurrent_tasks)
                .group_by(Task.tenant_id)
                .order_by(func.min(Task.submitted_at_ns))
                .limit(100)
            ).all()
            for tenant_id in dict.fromkeys(candidates):
                tenant = db.scalar(
                    select(Tenant).where(Tenant.id == tenant_id).with_for_update(skip_locked=True)
                )
                if tenant is None or tenant.active_tasks >= tenant.concurrent_tasks:
                    continue
                task = db.scalar(
                    select(Task)
                    .where(Task.tenant_id == tenant_id, Task.status == "queued")
                    .order_by(Task.submitted_at_ns)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if task is None:
                    continue
                task.status, task.lease_owner = "running", worker_id
                task.started_at, task.lease_expires_at = now(), now() + self.settings.lease_seconds
                tenant.active_tasks += 1
                self._event(db, task, "running", {"message": "Worker acquired execution lease"})
                return task
        return None

    def heartbeat(self, task_id: str, worker_id: str) -> bool:
        with self.transaction() as db:
            task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if not task or task.status != "running" or task.lease_owner != worker_id:
                return False
            if task.lease_expires_at <= now() or task.cancel_requested:
                return False
            if now() - task.started_at >= self.settings.task_timeout_seconds:
                return False
            task.lease_expires_at = now() + self.settings.lease_seconds
            return True

    def cancelled(self, task_id: str, worker_id: str) -> bool:
        with self.factory() as db:
            task = db.get(Task, task_id)
            return (
                not task
                or task.status != "running"
                or task.lease_owner != worker_id
                or task.cancel_requested
                or task.lease_expires_at <= now()
            )

    def record(self, task_id: str, worker_id: str, kind: str, data: dict, tokens: int = 0):
        with self.transaction() as db:
            task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if (
                not task
                or task.status != "running"
                or task.lease_owner != worker_id
                or task.lease_expires_at <= now()
            ):
                raise QueueError("Execution lease lost")
            if tokens < 0 or task.used_tokens + tokens > task.budget_tokens:
                raise QueueError("Task token budget exhausted")
            task.used_tokens += tokens
            self._event(db, task, kind, data)

    def _settle(self, db, task: Task, tenant: Tenant, status: str, summary="", patch="", error=""):
        usage = self._usage(db, task.tenant_id, task.usage_period)
        usage.reserved_tokens -= task.budget_tokens
        usage.tokens += task.used_tokens
        if task.status in {"running", "reaping"}:
            tenant.active_tasks -= 1
        task.status, task.summary, task.patch, task.error = status, summary, patch, error
        task.finished_at, task.lease_expires_at = now(), None
        self._event(
            db,
            task,
            status,
            {"message": error or summary or status, "used_tokens": task.used_tokens},
        )

    def finish(
        self, task_id: str, worker_id: str, status: str, summary="", patch="", error=""
    ) -> bool:
        if status not in TERMINAL:
            raise ValueError("Invalid terminal state")
        with self.transaction() as db:
            row = db.get(Task, task_id)
            if not row:
                return False
            tenant = self._tenant(db, row.tenant_id)
            task = db.scalar(
                select(Task)
                .where(Task.id == task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if task.status != "running" or task.lease_owner != worker_id:
                return False
            if task.cancel_requested:
                status, summary, patch = "cancelled", "Cancelled by user", ""
            self._settle(db, task, tenant, status, summary, patch, error)
            return True

    def cancel(self, tenant_id: str, task_id: str):
        with self.transaction() as db:
            tenant = self._tenant(db, tenant_id)
            task = db.scalar(
                select(Task)
                .where(Task.id == task_id, Task.tenant_id == tenant_id)
                .with_for_update()
            )
            if not task:
                raise QueueError("Task not found", 404)
            if task.status == "queued":
                self._settle(db, task, tenant, "cancelled", summary="Cancelled before execution")
            elif task.status == "running" and not task.cancel_requested:
                task.cancel_requested = True
                self._event(db, task, "cancelling", {"message": "Cancellation requested"})
            return task

    def expired(self):
        with self.factory() as db:
            return list(
                db.scalars(
                    select(Task).where(
                        or_(
                            Task.status == "reaping",
                            (Task.status == "running") & (Task.lease_expires_at <= now()),
                        )
                    )
                )
            )

    def begin_reap(self, task_id: str) -> bool:
        with self.transaction() as db:
            task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if not task or task.status not in {"running", "reaping"}:
                return False
            if task.status == "running" and task.lease_expires_at > now():
                return False
            task.status = "reaping"
            return True

    def fail_expired(self, task_id: str) -> bool:
        with self.transaction() as db:
            row = db.get(Task, task_id)
            if not row:
                return False
            tenant = self._tenant(db, row.tenant_id)
            task = db.scalar(
                select(Task)
                .where(Task.id == task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if task.status != "reaping":
                return False
            # A lost worker might have made an unrecorded paid request. Charge its reservation.
            task.used_tokens = task.budget_tokens
            self._settle(
                db, task, tenant, "failed", error="Worker lease expired; task was not retried"
            )
            return True

    def events(self, tenant_id: str, task_id: str, after: int = 0):
        self.get(tenant_id, task_id)
        with self.factory() as db:
            return list(
                db.scalars(
                    select(TaskEvent)
                    .where(TaskEvent.task_id == task_id, TaskEvent.id > after)
                    .order_by(TaskEvent.id)
                    .limit(200)
                )
            )
