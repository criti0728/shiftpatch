from concurrent.futures import ThreadPoolExecutor
import asyncio
import threading
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.auth import SESSION_COOKIE, GUEST_COOKIE, token_hash
from app.errors import DomainError
from app.main import create_app


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIFTPATCH_AI_MODE", "calculation_only")
    monkeypatch.setenv("SHIFTPATCH_AI_COOLDOWN_SECONDS", "0")
    monkeypatch.delenv("SHIFTPATCH_ENV", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("SHIFTPATCH_PUBLIC_ORIGIN", raising=False)
    monkeypatch.delenv("SHIFTPATCH_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("SHIFTPATCH_COOKIE_SECURE", raising=False)
    return create_app(tmp_path / "sessions.db")


def guest(client):
    response = client.post("/api/session/guest")
    assert response.status_code == 200, response.text
    return response.json()


def write(client, store_id, path, body):
    return client.post(f"/api/stores/{store_id}{path}", json=body, headers={"Idempotency-Key": str(uuid4())})


def overview(client, store_id):
    response = client.get(f"/api/stores/{store_id}/overview")
    assert response.status_code == 200, response.text
    return response.json()


def test_unauthenticated_cannot_read_or_mutate_any_sample(app):
    with TestClient(app) as client:
        assert client.get("/api/session").json()["authenticated"] is False
        assert client.get("/api/stores/demo/overview").status_code == 401
        assert write(client, "demo", "/orders", {"items": [{"menu_id": "latte", "quantity": 1}]}).status_code == 401
        assert write(client, "demo", "/demo/reset", {}).status_code == 401


def test_guests_have_isolated_state_and_cannot_access_each_other(app):
    with TestClient(app) as a, TestClient(app) as b:
        first, second = guest(a), guest(b)
        a_id, b_id = first["store"]["id"], second["store"]["id"]
        assert a_id != b_id
        before = overview(b, b_id)
        assert write(a, a_id, "/inventory-adjustments", {"ingredient_id": "milk", "on_hand": 6000}).status_code == 200
        assert overview(a, a_id)["inventory"] != before["inventory"]
        assert overview(b, b_id) == before
        assert a.get(f"/api/stores/{b_id}/overview").status_code == 404
        assert write(a, b_id, "/demo/reset", {}).status_code == 404
        assert a.get("/api/stores/demo/overview").status_code == 404
        config = overview(a, a_id)["config"]
        assert config["auth"] == "guest" and config["sample_store"] is True
        assert config["storage"] == "sqlite"


def test_resume_rotates_cookie_without_creating_another_store(app):
    with TestClient(app) as client:
        first = guest(client)
        raw = client.cookies.get(SESSION_COOKIE)
        assert app.state.identities.get_session(raw) is None
        assert app.state.identities.get_session(token_hash(raw))["store_id"] == first["store"]["id"]
        assert write(client, first["store"]["id"], "/inventory-adjustments", {"ingredient_id": "milk", "on_hand": 6000}).status_code == 200
        second = guest(client)
        assert first["store"] == second["store"]
        assert client.cookies.get(SESSION_COOKIE) != raw
        assert app.state.identities.get_session(token_hash(raw)) is None
        assert next(i for i in overview(client, second["store"]["id"])["inventory"] if i["id"] == "milk")["on_hand"] == 6000


def test_logout_revokes_access_but_saved_guest_can_resume(app):
    with TestClient(app) as client:
        first = guest(client)
        raw = client.cookies.get(SESSION_COOKIE)
        assert client.delete("/api/session").status_code == 200
        assert app.state.identities.get_session(token_hash(raw)) is None
        session = client.get("/api/session").json()
        assert session["authenticated"] is False and session["has_saved_guest"] is True
        assert client.get(f'/api/stores/{first["store"]["id"]}/overview').status_code == 401
        assert guest(client)["store"] == first["store"]


def test_cookie_expiry_and_resume_cookie_cannot_act_as_session(app):
    with TestClient(app) as client:
        first = guest(client)
        raw = client.cookies.get(SESSION_COOKIE)
        record = app.state.identities.get_session(token_hash(raw))
        record["expires_at"] = time.time() - 1
        app.state.identities.put_session(token_hash(raw), record)
        assert client.get("/api/session").json()["authenticated"] is False
        assert app.state.identities.get_session(token_hash(raw)) is None
        client.cookies.set(SESSION_COOKIE, client.cookies.get(GUEST_COOKIE), domain="testserver.local", path="/")
        assert client.get(f'/api/stores/{first["store"]["id"]}/overview').status_code == 401


def test_reset_restores_only_owned_sample_and_retains_identity(app):
    with TestClient(app) as a, TestClient(app) as b:
        a_id, b_id = guest(a)["store"]["id"], guest(b)["store"]["id"]
        assert write(a, a_id, "/inventory-adjustments", {"ingredient_id": "milk", "on_hand": 6000}).status_code == 200
        assert write(b, b_id, "/inventory-adjustments", {"ingredient_id": "milk", "on_hand": 7000}).status_code == 200
        before_a, before_b = overview(a, a_id), overview(b, b_id)
        assert write(a, a_id, "/demo/reset", {}).status_code == 200
        after = overview(a, a_id)
        assert after["store"]["id"] == a_id
        assert after["revision"] > before_a["revision"]
        assert next(i for i in after["inventory"] if i["id"] == "milk")["on_hand"] == 4000
        assert overview(b, b_id) == before_b


def test_guest_persists_across_app_restart(app):
    with TestClient(app) as original:
        first = guest(original)
        cookies = dict(original.cookies)
    restarted = create_app(app.state.repository.path)
    with TestClient(restarted) as client:
        client.cookies.update(cookies)
        assert client.get("/api/session").json()["store"] == first["store"]


def test_firebase_exchange_clones_first_guest_but_resumes_existing_account(app, monkeypatch):
    monkeypatch.setattr("app.auth.verify_firebase_token", lambda token: {"uid": "valid-uid", "name": "店長"})
    with TestClient(app) as client:
        first = guest(client)
        assert write(client, first["store"]["id"], "/inventory-adjustments", {"ingredient_id": "milk", "on_hand": 6000}).status_code == 200
        exchange = client.post("/api/session/firebase", json={"id_token": "verified-on-server"})
        assert exchange.status_code == 200, exchange.text
        account = exchange.json()
        assert account["user"]["kind"] == "firebase" and account["store"]["id"] != first["store"]["id"]
        stored = overview(client, account["store"]["id"])
        assert next(i for i in stored["inventory"] if i["id"] == "milk")["on_hand"] == 6000
        assert client.get(f'/api/stores/{first["store"]["id"]}/overview').status_code == 404
        client.delete("/api/session")
        guest(client)
        write(client, first["store"]["id"], "/inventory-adjustments", {"ingredient_id": "milk", "on_hand": 7000})
        again = client.post("/api/session/firebase", json={"id_token": "verified-on-server"}).json()
        assert again["store"] == account["store"]
        assert overview(client, account["store"]["id"]) == stored


def test_firebase_errors_do_not_create_authentication(app, monkeypatch):
    def invalid(token):
        raise DomainError("invalid_token", "再ログインしてください。", 401)
    monkeypatch.setattr("app.auth.verify_firebase_token", invalid)
    with TestClient(app) as client:
        response = client.post("/api/session/firebase", json={"id_token": "forged"})
        assert response.status_code == 401
        assert client.get("/api/session").json()["authenticated"] is False


def test_firebase_disabled_and_public_config(app, monkeypatch):
    for key in ["SHIFTPATCH_FIREBASE_API_KEY", "SHIFTPATCH_FIREBASE_AUTH_DOMAIN", "SHIFTPATCH_FIREBASE_APP_ID"]:
        monkeypatch.delenv(key, raising=False)
    with TestClient(app) as client:
        assert client.post("/api/session/firebase", json={"id_token": "token"}).status_code == 503
        monkeypatch.setenv("SHIFTPATCH_FIREBASE_API_KEY", "public-key")
        monkeypatch.setenv("SHIFTPATCH_FIREBASE_AUTH_DOMAIN", "project.firebaseapp.com")
        monkeypatch.setenv("SHIFTPATCH_FIREBASE_APP_ID", "public-app-id")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "project")
        payload = client.get("/api/session").json()
        assert payload["firebase_enabled"] is True
        assert payload["firebase_config"]["projectId"] == "project"


def test_production_csrf_host_checks_and_secure_cookies(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIFTPATCH_ENV", "production")
    monkeypatch.setenv("SHIFTPATCH_PUBLIC_ORIGIN", "https://sample.example.com")
    monkeypatch.delenv("SHIFTPATCH_ALLOWED_HOSTS", raising=False)
    app = create_app(tmp_path / "production-security.db")
    with TestClient(app, base_url="https://sample.example.com") as client:
        assert client.post("/api/session/guest").status_code == 403
        assert client.post("/api/session/guest", headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.post("/api/session/guest", headers={"Origin": "http://localhost:5173"}).status_code == 403
        response = client.post("/api/session/guest", headers={"Origin": "https://sample.example.com"})
        assert response.status_code == 200
        assert all("HttpOnly" in h and "Secure" in h and "SameSite=lax" in h for h in response.headers.get_list("set-cookie"))
        assert client.get("/api/session", headers={"Host": "evil.example"}).status_code == 400


def test_quotas_persist_across_reset_session_rotation_and_restarts(app, monkeypatch):
    monkeypatch.setenv("SHIFTPATCH_AI_STORE_DAILY_LIMIT", "1")
    with TestClient(app) as client:
        store_id = guest(client)["store"]["id"]
        proposal = {"mode": "in_service", "objective": "balanced"}
        assert write(client, store_id, "/proposals", proposal).status_code == 200
        assert write(client, store_id, "/demo/reset", {}).status_code == 200
        guest(client)
        assert write(client, store_id, "/proposals", proposal).status_code == 429
        cookies = dict(client.cookies)
    restarted = create_app(app.state.repository.path)
    with TestClient(restarted) as client:
        client.cookies.update(cookies)
        assert write(client, store_id, "/proposals", proposal).status_code == 429


def test_global_quota_bounds_new_guests(app, monkeypatch):
    monkeypatch.setenv("SHIFTPATCH_AI_GLOBAL_DAILY_LIMIT", "1")
    with TestClient(app) as a, TestClient(app) as b:
        proposal = {"mode": "pre_open", "objective": "balanced"}
        assert write(a, guest(a)["store"]["id"], "/proposals", proposal).status_code == 200
        assert write(b, guest(b)["store"]["id"], "/proposals", proposal).status_code == 429


def test_global_guest_creation_limit_does_not_block_saved_guest(app, monkeypatch):
    monkeypatch.setenv("SHIFTPATCH_GUEST_GLOBAL_DAILY_LIMIT", "1")
    with TestClient(app) as a, TestClient(app) as b:
        first = guest(a)
        assert b.post("/api/session/guest").status_code == 429
        a.delete("/api/session")
        assert guest(a)["store"] == first["store"]


def test_single_running_proposal_per_owned_sample(app, monkeypatch):
    began, release = threading.Event(), threading.Event()
    async def delayed(snapshot, mode, objective):
        began.set()
        await asyncio.to_thread(release.wait, 5)
        return {"status": "ready", "source": "calculation_only", "summary": "test", "candidates": [], "trace": []}
    monkeypatch.setattr("app.agent.runner.generate_proposal", delayed)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        store_id = guest(client)["store"]["id"]
        body = {"mode": "pre_open", "objective": "balanced"}
        first = pool.submit(write, client, store_id, "/proposals", body)
        assert began.wait(3)
        second = write(client, store_id, "/proposals", body)
        assert second.status_code == 409 and second.json()["detail"]["code"] == "run_in_progress"
        release.set()
        assert first.result().status_code == 200


def test_sqlite_quota_is_atomic_and_expiry_opens_a_new_window(app):
    identities = app.state.identities
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: identities.consume_quota("bounded", 5, 60, now=100), range(20)))
    assert sum(results) == 5
    assert identities.consume_quota("bounded", 5, 60, now=159) is False
    assert identities.consume_quota("bounded", 5, 60, now=160) is True
