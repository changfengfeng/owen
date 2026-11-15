import json
import os

import pytest
from pydantic import SecretStr

from cloud_agent.db import init_db, make_engine, sessions
from cloud_agent.models import Membership, Tenant, User
from cloud_agent.queue import TaskQueue
from cloud_agent.worker import Worker


@pytest.mark.live
@pytest.mark.integration
async def test_real_model_codes_and_runs_tests(tmp_path):
    key = os.environ.get("CLOUD_LLM_API_KEY")
    if not key:
        pytest.skip("CLOUD_LLM_API_KEY is not configured")
    from cloud_agent.config import Settings

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'live.db'}",
        llm_api_key=SecretStr(key),
        task_timeout_seconds=300,
    )
    engine = make_engine(settings)
    init_db(engine)
    queue = TaskQueue(sessions(engine), settings)
    with queue.factory.begin() as db:
        tenant, user = (
            Tenant(name="Live verification"),
            User(email="live@example.com", password_hash="unused"),
        )
        db.add_all([tenant, user])
        db.flush()
        db.add(Membership(user_id=user.id, tenant_id=tenant.id))
        tenant_id, user_id = tenant.id, user.id
    task = queue.submit(
        tenant_id,
        user_id,
        "Create slugify.py implementing slugify(text): lowercase ASCII text, replace consecutive "
        "non-alphanumeric characters with a single dash, strip leading/trailing dashes. "
        "Add test_slugify.py with at least 4 unittest tests including empty input, punctuation, "
        "and repeated separators. Run python -m unittest -v. Keep the implementation small.",
    )
    await Worker(settings, queue).run(once=True)
    finished = queue.get(tenant_id, task.id)
    assert finished.status == "succeeded", finished.error
    assert "slugify.py" in finished.patch and "test_slugify.py" in finished.patch
    results = [
        json.loads(event.data)
        for event in queue.events(tenant_id, task.id)
        if event.kind == "tool_result"
    ]
    assert any(
        "Ran " in result.get("output", "") and "OK" in result["output"] and result["exit_code"] == 0
        for result in results
    )
    assert finished.used_tokens > 0 and queue.usage(tenant_id)["reserved_tokens"] == 0
    print(
        f"\nLive model passed: used_tokens={finished.used_tokens}, patch_bytes={len(finished.patch.encode())}"
    )
    engine.dispose()
