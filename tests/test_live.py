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
@pytest.mark.parametrize("mode", ["normal", "truncated-write-recovery", "recovery-exhausted"])
async def test_real_model_codes_and_runs_tests(tmp_path, mode):
    key = os.environ.get("CLOUD_LLM_API_KEY")
    if not key:
        pytest.skip("CLOUD_LLM_API_KEY is not configured")
    from cloud_agent.config import Settings

    truncate = mode != "normal"
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'live.db'}",
        llm_api_key=SecretStr(key),
        task_timeout_seconds=300,
        task_token_budget=40_000 if truncate else 24_000,
        llm_max_output_tokens=512 if truncate else 4096,
        llm_max_truncation_retries=0 if mode == "recovery-exhausted" else 2,
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
    prompt = (
        "Create slugify.py implementing slugify(text): lowercase ASCII text, replace consecutive "
        "non-alphanumeric characters with a single dash, strip leading/trailing dashes. "
        "Add test_slugify.py with at least 4 unittest tests including empty input, punctuation, "
        "and repeated separators. Run python -m unittest -v. Keep the implementation small."
    )
    if truncate:
        prompt = (
            "In this empty Python repository create rows.py defining ROWS as a literal list "
            "of exactly 400 strings, row-0000 through row-0399. In your first response use "
            "one write_file call containing the full literal list, one entry per line. "
            "Do not abbreviate or generate entries in that first response. "
            "If recovery from truncation is requested later, a short script generating the "
            "literal file is allowed. Add test_rows.py using unittest to verify all entries "
            "and their order, then actually run python -m unittest -v. Keep explanations short."
        )
    task = queue.submit(tenant_id, user_id, prompt)
    await Worker(settings, queue).run(once=True)
    finished = queue.get(tenant_id, task.id)
    events = queue.events(tenant_id, task.id)
    usage = [json.loads(event.data) for event in events if event.kind == "usage"]
    assert finished.used_tokens > 0 and queue.usage(tenant_id)["reserved_tokens"] == 0
    assert sum(item["tokens"] for item in usage) == finished.used_tokens
    assert all(not item["estimated"] for item in usage)
    if truncate:
        assert any(item["finish_reason"] == "length" for item in usage)
    if mode == "recovery-exhausted":
        assert finished.status == "failed"
        assert "recovery limit exhausted" in finished.error
        assert not any(event.kind == "model_retry" for event in events)
        print(f"\nLive bounded failure passed: used_tokens={finished.used_tokens}")
        engine.dispose()
        return
    assert finished.status == "succeeded", finished.error
    module = "rows" if truncate else "slugify"
    assert f"{module}.py" in finished.patch and f"test_{module}.py" in finished.patch
    results = [json.loads(event.data) for event in events if event.kind == "tool_result"]
    assert any(
        "Ran " in result.get("output", "") and "OK" in result["output"] and result["exit_code"] == 0
        for result in results
    )
    if truncate:
        assert any(event.kind == "model_retry" for event in events)
    print(
        f"\nLive model passed: used_tokens={finished.used_tokens}, patch_bytes={len(finished.patch.encode())}"
    )
    engine.dispose()
