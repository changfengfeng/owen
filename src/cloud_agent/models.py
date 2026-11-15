import time
import uuid

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from cloud_agent.db import Base


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> int:
    return int(time.time())


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120))
    daily_tasks: Mapped[int] = mapped_column(default=20)
    concurrent_tasks: Mapped[int] = mapped_column(default=2)
    monthly_tokens: Mapped[int] = mapped_column(default=500_000)
    active_tasks: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)


class Membership(Base):
    __tablename__ = "memberships"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(20), default="owner")


class LoginSession(Base):
    __tablename__ = "login_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    csrf_token: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[int] = mapped_column(BigInteger)


class AuthAttempt(Base):
    __tablename__ = "auth_attempts"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[int] = mapped_column(BigInteger)
    count: Mapped[int] = mapped_column(default=0)


class Usage(Base):
    __tablename__ = "usage"
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    period: Mapped[str] = mapped_column(String(10), primary_key=True)
    tasks: Mapped[int] = mapped_column(default=0)
    tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    reserved_tokens: Mapped[int] = mapped_column(BigInteger, default=0)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key"),
        Index("ix_tasks_status_created", "status", "submitted_at_ns"),
        Index("ix_tasks_tenant_created", "tenant_id", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    prompt: Mapped[str] = mapped_column(Text)
    repo_url: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(20), default="queued")
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    budget_tokens: Mapped[int] = mapped_column(default=24_000)
    used_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    usage_period: Mapped[str] = mapped_column(String(10))
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    patch: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)
    submitted_at_ns: Mapped[int] = mapped_column(BigInteger, default=time.time_ns)
    started_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    finished_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class TaskEvent(Base):
    __tablename__ = "task_events"
    __table_args__ = (Index("ix_events_task_id", "task_id", "id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    kind: Mapped[str] = mapped_column(String(40))
    data: Mapped[str] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now)
