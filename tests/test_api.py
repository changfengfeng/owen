import json

from fastapi.testclient import TestClient


def test_authentication_csrf_and_logout(client, account):
    assert client.get("/api/auth/me").json()["tenant_id"] == account["tenant_id"]
    client.headers.pop("x-csrf-token")
    assert client.post("/api/tasks", json={"prompt": "hello"}).status_code == 403
    client.headers["x-csrf-token"] = account["csrf_token"]
    assert (
        client.post(
            "/api/tasks", json={"prompt": "hello"}, headers={"origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert client.post("/api/auth/logout", json={}).status_code == 204
    assert client.get("/api/auth/me").status_code == 401
    response = client.post(
        "/api/auth/login", json={"email": "one@example.com", "password": "correct-horse-phrase-123"}
    )
    assert response.status_code == 200
    assert response.json()["csrf_token"] != account["csrf_token"]


def test_cookie_and_security_headers(client, account):
    response = client.post(
        "/api/auth/login", json={"email": "one@example.com", "password": "correct-horse-phrase-123"}
    )
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert response.headers["cache-control"] == "no-store"
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/").status_code == 200


def test_tenant_isolation_for_every_task_endpoint(app, client, account):
    created = client.post("/api/tasks", json={"prompt": "secret task"}).json()
    with TestClient(app) as other:
        value = other.post(
            "/api/auth/register",
            json={"email": "two@example.com", "password": "another-correct-phrase"},
        ).json()
        other.headers["x-csrf-token"] = value["csrf_token"]
        assert other.get("/api/tasks").json()["tasks"] == []
        for suffix in ("", "/events", "/patch"):
            assert other.get(f"/api/tasks/{created['id']}{suffix}").status_code == 404
        assert other.post(f"/api/tasks/{created['id']}/cancel", json={}).status_code == 404
        assert other.get("/api/usage").json()["daily_tasks"] == 0


def test_idempotency_and_cancel_releases_reservation(client, account):
    headers = {"Idempotency-Key": "test-request"}
    task = client.post("/api/tasks", json={"prompt": "test"}, headers=headers).json()
    duplicate = client.post("/api/tasks", json={"prompt": "test"}, headers=headers).json()
    assert duplicate["id"] == task["id"]
    assert client.post("/api/tasks", json={"prompt": "other"}, headers=headers).status_code == 409
    usage = client.get("/api/usage").json()
    assert usage["reserved_tokens"] == task["budget_tokens"] and usage["daily_tasks"] == 1
    assert client.post(f"/api/tasks/{task['id']}/cancel", json={}).json()["status"] == "cancelled"
    assert client.post(f"/api/tasks/{task['id']}/cancel", json={}).status_code == 200
    assert client.get("/api/usage").json()["reserved_tokens"] == 0
    assert client.get(f"/api/tasks/{task['id']}/patch").status_code == 200


def test_sse_replay_and_cursor(app, client, account):
    task = client.post("/api/tasks", json={"prompt": "test"}).json()
    client.post(f"/api/tasks/{task['id']}/cancel", json={})
    response = client.get(f"/api/tasks/{task['id']}/events")
    assert "event: complete" in response.text
    assert '"kind": "queued"' in response.text and '"kind": "cancelled"' in response.text
    event = app.state.queue.events(account["tenant_id"], task["id"])[0]
    replay = client.get(f"/api/tasks/{task['id']}/events", headers={"Last-Event-ID": str(event.id)})
    assert '"kind": "queued"' not in replay.text


def test_invalid_repository_and_payload_limits(client, account):
    for repo in (
        "http://127.0.0.1/repo",
        "https://github.com@evil.example/a/b",
        "https://github.com/a/../b",
        "https://github.com/a/b?x=y",
    ):
        assert (
            client.post("/api/tasks", json={"prompt": "test", "repo_url": repo}).status_code == 422
        )
    assert client.post("/api/tasks", json={"prompt": "   "}).status_code == 422
    assert (
        client.post("/api/tasks", content="x", headers={"content-type": "text/plain"}).status_code
        == 415
    )
    assert (
        client.post(
            "/api/tasks",
            content=json.dumps({"prompt": "x" * 140_000}),
            headers={"content-type": "application/json"},
        ).status_code
        == 413
    )


def test_unauthenticated_access_and_bad_password(client):
    assert client.get("/api/tasks").status_code == 401
    assert (
        client.post(
            "/api/auth/register", json={"email": "a@example.com", "password": "short"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/auth/login",
            json={"email": "none@example.com", "password": "invalid-long-password"},
        ).status_code
        == 401
    )


def test_registration_gate_and_rate_limit(app, client):
    from pydantic import SecretStr

    app.state.settings.signup_code = SecretStr("local-test-code")
    payload = {"email": "one@example.com", "password": "correct-horse-phrase-123"}
    assert client.post("/api/auth/register", json=payload).status_code == 403
    assert (
        client.post(
            "/api/auth/register", json={**payload, "signup_code": "local-test-code"}
        ).status_code
        == 201
    )
    for _ in range(31):
        response = client.post(
            "/api/auth/login", json={**payload, "password": "wrong-password-12345"}
        )
    assert response.status_code == 429


async def test_forwarded_addresses_require_explicitly_trusted_proxy():
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    observed = []

    async def endpoint(scope, receive, send):
        observed.append(scope["client"][0])

    scope = {
        "type": "http",
        "client": ("172.18.0.3", 1234),
        "headers": [(b"x-forwarded-for", b"203.0.113.7")],
    }
    await ProxyHeadersMiddleware(endpoint, trusted_hosts="127.0.0.1")(scope.copy(), None, None)
    await ProxyHeadersMiddleware(endpoint, trusted_hosts="172.18.0.3")(scope.copy(), None, None)
    assert observed == ["172.18.0.3", "203.0.113.7"]
