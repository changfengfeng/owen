from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from cloud_agent.config import Settings


class Base(DeclarativeBase):
    pass


def make_engine(settings: Settings) -> Engine:
    url = make_url(settings.database_url)
    kwargs = {}
    if url.get_backend_name() == "sqlite":
        if url.database and url.database != ":memory:":
            Path(url.database).resolve().parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(settings.database_url, pool_pre_ping=True, **kwargs)
    if url.get_backend_name() == "sqlite":

        @event.listens_for(engine, "connect")
        def sqlite_pragmas(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")

    return engine


def sessions(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    from cloud_agent import models  # noqa: F401

    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(279451801)"))
        Base.metadata.create_all(connection)


def require_schema(engine: Engine) -> None:
    from cloud_agent import models  # noqa: F401

    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name) or not set(table.columns.keys()) <= {
            column["name"] for column in inspector.get_columns(table.name)
        }:
            raise RuntimeError("Database schema is not initialized; run cloud-agent-db first")


def main() -> None:
    from cloud_agent.config import get_settings

    engine = make_engine(get_settings())
    init_db(engine)
    engine.dispose()
    print("Database schema initialized")
