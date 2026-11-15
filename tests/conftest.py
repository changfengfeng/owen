import os
import uuid

import pytest
from fastapi.testclient import TestClient

from cloud_agent.config import Settings
from cloud_agent.main import create_app


@pytest.fixture
def app(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}", environment="test")
    value = create_app(settings)
    yield value
    value.state.engine.dispose()


@pytest.fixture
def client(app):
    with TestClient(app) as value:
        yield value


@pytest.fixture
def account(client):
    response = client.post(
        "/api/auth/register",
        json={"email": "one@example.com", "password": "correct-horse-phrase-123"},
    )
    assert response.status_code == 201, response.text
    result = response.json()
    client.headers["x-csrf-token"] = result["csrf_token"]
    return result


@pytest.fixture
def postgres_queue():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    from sqlalchemy import text

    from cloud_agent.db import init_db, make_engine, sessions
    from cloud_agent.queue import TaskQueue

    schema = "test_" + uuid.uuid4().hex
    settings = Settings(database_url=url, environment="test")
    admin = make_engine(settings)
    with admin.begin() as db:
        db.execute(text(f"CREATE SCHEMA {schema}"))
    engine = admin.execution_options(schema_translate_map={None: schema})
    init_db(engine)
    yield TaskQueue(sessions(engine), settings)
    with admin.begin() as db:
        db.execute(text(f"DROP SCHEMA {schema} CASCADE"))
    admin.dispose()
