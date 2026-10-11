"""Guest samples and optional verified Firebase account sessions."""
from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from uuid import uuid4

from fastapi import Request, Response

from .errors import DomainError

SESSION_COOKIE = "shiftpatch_session"
GUEST_COOKIE = "shiftpatch_guest"
SESSION_TTL = 7 * 24 * 60 * 60
GUEST_TTL = 30 * 24 * 60 * 60
_firebase_lock = threading.Lock()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def firebase_config() -> dict | None:
    values = {
        "apiKey": os.getenv("SHIFTPATCH_FIREBASE_API_KEY"),
        "authDomain": os.getenv("SHIFTPATCH_FIREBASE_AUTH_DOMAIN"),
        "projectId": os.getenv("SHIFTPATCH_FIREBASE_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT"),
        "appId": os.getenv("SHIFTPATCH_FIREBASE_APP_ID"),
    }
    return values if all(values.values()) else None


def verify_firebase_token(id_token: str) -> dict:
    config = firebase_config()
    if not config:
        raise DomainError("firebase_disabled", "Google ログインはまだ設定されていません。サンプル店舗をお試しください。", 503)
    try:
        import firebase_admin
        from firebase_admin import auth
        name = "shiftpatch-" + config["projectId"]
        with _firebase_lock:
            try:
                firebase_app = firebase_admin.get_app(name)
            except ValueError:
                firebase_app = firebase_admin.initialize_app(options={"projectId": config["projectId"]}, name=name)
        claims = auth.verify_id_token(id_token, app=firebase_app, check_revoked=True)
    except ImportError:
        raise DomainError("firebase_unavailable", "Google ログインのサーバー設定を確認してください。", 503)
    except Exception:
        # Do not expose token contents or upstream credential diagnostics.
        raise DomainError("invalid_token", "ログインの有効期限が切れました。もう一度ログインしてください。", 401)
    if not claims.get("uid"):
        raise DomainError("invalid_token", "ログイン情報を確認できませんでした。", 401)
    return claims


class SessionService:
    def __init__(self, stores, identities):
        self.stores, self.identities = stores, identities

    def load_token(self, token: str | None, scope: str = "session") -> dict | None:
        if not token or len(token) > 200:
            return None
        digest = token_hash(token)
        record = self.identities.get_session(digest)
        if not record or record.get("scope") != scope:
            return None
        if record["expires_at"] <= time.time():
            self.identities.delete_session(digest)
            return None
        store = self.stores.get_store(record["store_id"])
        if not store or store["owner_id"] != record["user"]["id"]:
            return None
        return record

    def current(self, request: Request) -> dict | None:
        return self.load_token(request.cookies.get(SESSION_COOKIE))

    def require_store(self, request: Request, store_id: str):
        session = self.current(request)
        if not session:
            raise DomainError("session_required", "サンプル店舗を開いてから操作してください。", 401)
        if session["store_id"] != store_id:
            raise DomainError("not_found", "店舗が見つかりません。", 404)
        request.state.session = session
        return self.stores.for_store(store_id)

    def payload(self, session: dict | None, request: Request | None = None) -> dict:
        config = firebase_config()
        value = {"authenticated": session is not None, "firebase_enabled": config is not None, "firebase_config": config}
        if session:
            store = self.stores.get_store(session["store_id"])
            value.update(user=session["user"], store={"id": store["id"], "name": store["name"]})
        elif request:
            value["has_saved_guest"] = self.load_token(request.cookies.get(GUEST_COOKIE), "guest_resume") is not None
        return value

    def _secure(self, request: Request) -> bool:
        configured = os.getenv("SHIFTPATCH_COOKIE_SECURE")
        return configured.lower() in {"1", "true", "yes"} if configured else bool(os.getenv("K_SERVICE")) or request.url.scheme == "https"

    def issue(self, request: Request, response: Response, user: dict, store_id: str, *, guest_resume=False) -> dict:
        cookie = GUEST_COOKIE if guest_resume else SESSION_COOKIE
        ttl = GUEST_TTL if guest_resume else SESSION_TTL
        old = request.cookies.get(cookie)
        if old:
            self.identities.delete_session(token_hash(old))
        token = secrets.token_urlsafe(48)
        record = {"user": user, "store_id": store_id, "scope": "guest_resume" if guest_resume else "session", "expires_at": time.time() + ttl}
        self.identities.put_session(token_hash(token), record)
        if not guest_resume and user["kind"] == "guest" and hasattr(self.stores, "touch_store"):
            self.stores.touch_store(store_id)
        response.set_cookie(cookie, token, httponly=True, secure=self._secure(request), samesite="lax", max_age=ttl, path="/")
        return record

    def guest(self, request: Request, response: Response) -> dict:
        existing = self.current(request)
        if existing and existing["user"]["kind"] != "guest":
            existing = None
        existing = existing or self.load_token(request.cookies.get(GUEST_COOKIE), "guest_resume")
        if existing:
            user, store_id = existing["user"], existing["store_id"]
        else:
            limit = max(1, int(os.getenv("SHIFTPATCH_GUEST_GLOBAL_DAILY_LIMIT", "1000")))
            if not self.identities.consume_quota("guest-global", limit, 86400, now=time.time()):
                raise DomainError("rate_limited", "本日のサンプル店舗の作成上限に達しました。時間をおいて再度お試しください。", 429)
            user = {"id": "guest-" + str(uuid4()), "kind": "guest", "display_name": "ゲスト"}
            store_id = "sample-" + str(uuid4())
            self.stores.create_store(store_id, user["id"])
        self.issue(request, response, user, store_id, guest_resume=True)
        session = self.issue(request, response, user, store_id)
        return self.payload(session)

    def firebase(self, request: Request, response: Response, id_token: str) -> dict:
        claims = verify_firebase_token(id_token)
        user = {"id": "firebase-" + claims["uid"], "kind": "firebase", "display_name": str(claims.get("name") or "Google ユーザー")[:100]}
        store_id = "sample-" + hashlib.sha256(user["id"].encode()).hexdigest()[:40]
        old = self.current(request)
        initial = self.stores.for_store(old["store_id"]).read() if old and old["user"]["kind"] == "guest" else None
        self.stores.create_store(store_id, user["id"], initial_state=initial)
        session = self.issue(request, response, user, store_id)
        return self.payload(session)

    def logout(self, request: Request, response: Response):
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            self.identities.delete_session(token_hash(token))
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=self._secure(request), samesite="lax")
