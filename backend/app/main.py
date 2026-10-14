from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import operations as ops
from .incidents import detect
from .agent.schemas import Constraints
from .errors import DomainError
from .auth import SessionService
from .repository import SQLiteIdentityRepository, SQLiteRepository, sample_state
from .timeutils import iso, parse_time

logger = logging.getLogger(__name__)
Quantity = Annotated[int, Field(strict=True, ge=0, le=1_000_000)]
Positive = Annotated[int, Field(strict=True, ge=1, le=20)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StaffPatch(Body):
    absent: bool | None = None
    shift_start: str | None = None
    shift_end: str | None = None
    skills: dict[str, Annotated[int, Field(strict=True, ge=0, le=3600)]] | None = None
    available_dates: list[str] | None = Field(default=None, max_length=62)

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_datetime(cls, value):
        if value is not None:
            parsed = parse_time(value)
            if parsed.tzinfo is None:
                raise ValueError("Timezone is required")
        return value

    @field_validator("available_dates")
    @classmethod
    def valid_dates(cls, value):
        for item in value or []:
            date.fromisoformat(item)
        return value


class Adjustment(Body):
    ingredient_id: str
    on_hand: Quantity


class Receipt(Body):
    quantity: Quantity
    expires_at: str | None = None

    @field_validator('expires_at')
    @classmethod
    def valid_expiry(cls, value):
        if value and parse_time(value).tzinfo is None:
            raise ValueError('Timezone required')
        return value


class LotBody(Body):
    ingredient_id: str
    quantity: Quantity
    expires_at: str

    @field_validator('expires_at')
    @classmethod
    def valid_expiry(cls, value):
        if parse_time(value).tzinfo is None:
            raise ValueError('Timezone required')
        return value


class OrderItem(Body):
    menu_id: str
    quantity: Positive


class OrderBody(Body):
    items: list[OrderItem] = Field(min_length=1, max_length=20)


class TaskBody(Body):
    status: Literal["preparing", "completed"]
    staff_id: str | None = None


class ProposalBody(Body):
    mode: Literal["pre_open", "in_service", "post_close"]
    objective: Literal["balanced", "preserve_drinks", "reduce_delay"] = "balanced"
    trigger_event_id: str | None = Field(default=None, max_length=100)
    parent_run_id: str | None = Field(default=None, max_length=100)
    service_date: str | None = None
    constraints: Constraints = Field(default_factory=Constraints)
    locale: Literal['ja-JP', 'ko-KR'] = 'ja-JP'
    answers: dict[str, str] = Field(default_factory=dict, max_length=5)


class ApprovalBody(Body):
    candidate_id: str
    expected_revision: Annotated[int, Field(strict=True, ge=1)]


class ModeReview(Body):
    action: Literal["release"]


class ClockBody(Body):
    mode: Literal["manual", "realtime"]


class Advance(Body):
    minutes: Annotated[int, Field(strict=True, ge=1, le=120)]


SalesCounts = dict[str, Annotated[int, Field(strict=True, ge=0, le=1000)]]


class HistoryWindow(Body):
    start_time: str = Field(pattern=r'^\d{2}:\d{2}$')
    counts: SalesCounts

    @field_validator('start_time')
    @classmethod
    def valid_time(cls, value):
        from datetime import time as clock_time
        clock_time.fromisoformat(value)
        return value


class HistoryPatch(Body):
    counts: SalesCounts
    restricted: bool
    windows: list[HistoryWindow] | None = Field(default=None, max_length=48)

    @field_validator('windows')
    @classmethod
    def valid_windows(cls, value):
        seen = set()
        for window in value or []:
            if window.start_time in seen:
                raise ValueError('Unique start_time/counts required')
            seen.add(window.start_time)
        return value


class FirebaseExchange(Body):
    id_token: str = Field(min_length=1, max_length=10000)


def fingerprint(command, body):
    return hashlib.sha256(json.dumps([command, body], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def create_app(db_path: str | Path | None = None, *, repository=None, identities=None) -> FastAPI:
    from .settings import agent_timeout_seconds, load_env
    load_env()
    production = os.getenv("SHIFTPATCH_ENV") == "production" or bool(os.getenv("K_SERVICE"))
    app = FastAPI(title="ShiftPatch API", version="0.2.0")
    path = db_path or os.getenv("SHIFTPATCH_DB_PATH", str(Path(__file__).resolve().parents[2] / "data" / "shiftpatch.db"))
    storage = os.getenv("SHIFTPATCH_STORAGE", "sqlite")
    if storage not in {"sqlite", "firestore"}:
        raise RuntimeError("SHIFTPATCH_STORAGE must be sqlite or firestore.")
    if production and db_path is None and repository is None and storage != "firestore":
        raise RuntimeError("Public deployments require SHIFTPATCH_STORAGE=firestore for durable sample data.")
    if repository is None:
        if db_path is None and storage == "firestore":
            from .cloud_repository import create_repositories
            repository, identities = create_repositories(os.getenv("GOOGLE_CLOUD_PROJECT"))
        else:
            repository = SQLiteRepository(path)
    identities = identities or SQLiteIdentityRepository(path)
    sessions = SessionService(repository, identities)
    app.state.repository, app.state.identities, app.state.sessions = repository, identities, sessions
    public_origin = os.getenv("SHIFTPATCH_PUBLIC_ORIGIN", "").rstrip("/")
    if production and not public_origin:
        raise RuntimeError("SHIFTPATCH_PUBLIC_ORIGIN is required for public deployment.")
    if public_origin:
        parsed_origin = urlparse(public_origin)
        if not parsed_origin.hostname or parsed_origin.path or parsed_origin.query or parsed_origin.fragment or parsed_origin.username or parsed_origin.password or parsed_origin.scheme not in {"http", "https"}:
            raise RuntimeError("SHIFTPATCH_PUBLIC_ORIGIN must be a single origin, for example https://sample.example.com.")
        if production and parsed_origin.scheme != "https":
            raise RuntimeError("Public deployments require an HTTPS origin.")
    origins = [public_origin] if public_origin else ["http://localhost:5173", "http://127.0.0.1:5173"]
    extra_origins = os.getenv("SHIFTPATCH_ALLOWED_ORIGINS", "")
    origins += [value.strip().rstrip("/") for value in extra_origins.split(",") if value.strip()]
    configured_hosts = os.getenv("SHIFTPATCH_ALLOWED_HOSTS", "")
    hosts = [value.strip() for value in configured_hosts.split(",") if value.strip()]
    hosts = hosts or ([urlparse(public_origin).hostname] if production else ["localhost", "127.0.0.1", "[::1]", "testserver"])
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "Idempotency-Key"])

    @app.middleware("http")
    async def origin_guard(request: Request, call_next):
        origin = request.headers.get("origin")
        # A configured public origin is authoritative. Local CLI/test clients can
        # omit Origin, while public cookie mutations must come from the browser.
        same_origin = str(request.base_url).rstrip("/")
        allowed = origin in origins or (not public_origin and origin == same_origin)
        mutating = request.method in {"POST", "PATCH", "DELETE", "PUT"}
        if (origin and not allowed) or (production and mutating and not origin and request.headers.get("sec-fetch-site") != "same-origin") or request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": {"code": "invalid_origin", "message": "この画面から操作してください。"}}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse({"detail": {"code": exc.code, "message": exc.message}}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        fields = [".".join(str(part) for part in err["loc"][1:]) for err in exc.errors()]
        return JSONResponse({"detail": {"code": "invalid_input", "message": "入力内容を確認してください: " + ", ".join(fields)}}, status_code=422)

    def store_repository(store_id: str, request: Request):
        scoped = sessions.require_store(request, store_id)
        from .clock import tick
        if scoped.read().get('clock', {}).get('mode') == 'realtime':
            def update_clock(state):
                if tick(state):
                    detect(state, 'realtime-clock')
            scoped.transact(update_clock)
        return scoped

    def present(scoped_repository, request: Request):
        state = scoped_repository.read()
        value = ops.overview(state, os.getenv("SHIFTPATCH_AI_MODE", "calculation_only"))
        entries = [r for r in state.get('agent_runs', {}).values() if r['status'] == 'running']
        active = max(entries, key=lambda r: r.get('started_at', ''), default=None)
        if active and parse_time(active['expires_at']) <= datetime.now(timezone.utc):
            active = {**active, 'status': 'interrupted'}
        value['active_run'] = {k: v for k, v in active.items() if k not in {'key', 'digest'}} if active else None
        value["config"].update(storage=repository.storage_name, auth=request.state.session["user"]["kind"], sample_store=True)
        return value

    @app.get("/api/session")
    def session_info(request: Request):
        return sessions.payload(sessions.current(request), request)

    @app.post("/api/session/guest")
    def guest_session(request: Request, response: Response):
        return sessions.guest(request, response)

    @app.post("/api/session/firebase")
    def firebase_session(request: Request, response: Response, body: FirebaseExchange):
        return sessions.firebase(request, response, body.id_token)

    @app.delete("/api/session")
    def logout(request: Request, response: Response):
        sessions.logout(request, response)
        return {"authenticated": False}

    def check_reserved_key(state, key):
        # Proposal generation runs outside the write transaction. Its key remains
        # reserved in agent_runs until the final result is persisted.
        if any(run["key"] == key for run in state.get("agent_runs", {}).values()):
            raise DomainError("idempotency_conflict", "このキーは別の計算に使用されています。新しい操作には別のキーを使用してください。")

    def mutate(scoped_repository, key, command, body, operation):
        digest = fingerprint(command, body)

        def execute(state):
            prior = state["idempotency"].get(key)
            if prior:
                if prior["digest"] != digest:
                    raise DomainError("idempotency_conflict", "同じリクエストキーを別の操作に使用できません。")
                return prior["result"]
            check_reserved_key(state, key)
            result = operation(state)
            detect(state, command)
            state["idempotency"][key] = {"digest": digest, "result": result}
            return result

        return scoped_repository.transact(execute)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "environment": "production" if production else "local", "storage": repository.storage_name, "agent_mode": os.getenv("SHIFTPATCH_AI_MODE", "calculation_only")}

    @app.get("/api/stores/{store_id}/overview")
    def overview(store_id: str, request: Request, scoped_repository=Depends(store_repository)):
        return present(scoped_repository, request)

    @app.patch("/api/stores/{store_id}/staff/{staff_id}")
    def staff_patch(store_id: str, staff_id: str, body: StaffPatch, idempotency_key: IdempotencyKey, request: Request, scoped_repository=Depends(store_repository)):
        values = body.model_dump(exclude_none=True)
        mutate(scoped_repository, idempotency_key, f"staff/{staff_id}", values, lambda s: ops.update_staff(s, staff_id, values))
        return present(scoped_repository, request)

    @app.patch("/api/stores/{store_id}/next-day-staff/{staff_id}")
    def tomorrow_patch(store_id: str, staff_id: str, body: StaffPatch, idempotency_key: IdempotencyKey, request: Request, scoped_repository=Depends(store_repository)):
        values = body.model_dump(exclude_none=True)
        mutate(scoped_repository, idempotency_key, f"next-day-staff/{staff_id}", values, lambda s: ops.update_staff(s, staff_id, values, next_day=True))
        return present(scoped_repository, request)

    @app.post("/api/stores/{store_id}/inventory-adjustments")
    def adjust(store_id: str, body: Adjustment, idempotency_key: IdempotencyKey, request: Request, scoped_repository=Depends(store_repository)):
        mutate(scoped_repository, idempotency_key, "inventory-adjustments", body.model_dump(), lambda s: ops.adjust_inventory(s, body.ingredient_id, body.on_hand))
        return present(scoped_repository, request)

    @app.post("/api/stores/{store_id}/deliveries/{delivery_id}/receive")
    def receive(store_id: str, delivery_id: str, body: Receipt, idempotency_key: IdempotencyKey, request: Request, scoped_repository=Depends(store_repository)):
        mutate(scoped_repository, idempotency_key, f"receive/{delivery_id}", body.model_dump(), lambda s: ops.receive_delivery(s, delivery_id, body.quantity, body.expires_at))
        return present(scoped_repository, request)

    @app.post('/api/stores/{store_id}/inventory-lots')
    def add_lot(store_id: str, body: LotBody, idempotency_key: IdempotencyKey, request: Request, scoped_repository=Depends(store_repository)):
        mutate(scoped_repository, idempotency_key, 'inventory-lots', body.model_dump(), lambda s: ops.add_lot(s, body.ingredient_id, body.quantity, body.expires_at))
        return present(scoped_repository, request)

    @app.post("/api/stores/{store_id}/orders")
    def order(store_id: str, body: OrderBody, idempotency_key: IdempotencyKey, request: Request, scoped_repository=Depends(store_repository)):
        result = mutate(scoped_repository, idempotency_key, "orders", body.model_dump(), lambda s: ops.create_order(s, [item.model_dump() for item in body.items]))
        return {**result, "overview": present(scoped_repository, request)}

    @app.post("/api/stores/{store_id}/orders/{order_id}/tasks/{task_id}/status")
    def task(store_id: str, order_id: str, task_id: str, body: TaskBody, idempotency_key: IdempotencyKey, request: Request, scoped_repository=Depends(store_repository)):
        mutate(scoped_repository, idempotency_key, f"task/{order_id}/{task_id}", body.model_dump(), lambda s: ops.update_task(s, order_id, task_id, body.status, body.staff_id))
        return present(scoped_repository, request)

    @app.post('/api/stores/{store_id}/orders/{order_id}/tasks/{task_id}/reallocate')
    def reallocate(store_id: str, order_id: str, task_id: str, body: Body, idempotency_key: IdempotencyKey, request: Request, scoped_repository=Depends(store_repository)):
        mutate(scoped_repository, idempotency_key, 'reallocate/'+task_id, {}, lambda s: ops.reallocate_task(s, order_id, task_id))
        return present(scoped_repository, request)

    @app.post("/api/stores/{store_id}/proposals")
    async def proposal(store_id: str, body: ProposalBody, idempotency_key: IdempotencyKey, request: Request, scoped_repository=Depends(store_repository)):
        try:
            time_budget = agent_timeout_seconds()
        except ValueError as error:
            raise DomainError('invalid_ai_configuration', str(error), 503) from None
        digest = fingerprint("proposals", body.model_dump())
        run_id = str(uuid4())
        started = datetime.now(timezone.utc)

        def register(state):
            prior = state['idempotency'].get(idempotency_key)
            if prior and prior['digest'] != digest:
                raise DomainError('idempotency_conflict', '同じキーの入力が異なります。')
            if prior:
                cached = ops.get(state['proposals'], prior['result']['proposal_id'], '提案')
                if cached['status'] == 'ready' and not ops.proposal_current(cached, state):
                    cached['status'] = 'stale'
                return {'cached': deepcopy(cached)}
            for old_run in state['agent_runs'].values():
                if old_run['key'] == idempotency_key and old_run['digest'] != digest:
                    raise DomainError('idempotency_conflict', '同じキーの入力が異なります。')
            target = state['store']['service_date']
            if body.mode == 'post_close':
                target = (date.fromisoformat(target) + timedelta(days=1)).isoformat()
            if body.service_date and body.service_date != target:
                raise DomainError('invalid_service_date', 'このモードの対象営業日と一致しません。', 422)
            if body.trigger_event_id:
                incident = next((e for e in state['events'] if e['id'] == body.trigger_event_id and e['type'] == 'replan'), None)
                if body.mode != 'in_service':
                    raise DomainError('invalid_incident', '有効な営業中の事件ではありません。', 422)
                for run in state['agent_runs'].values():
                    if (run.get('trigger_event_id') == body.trigger_event_id or run.get('covered_event_id') == body.trigger_event_id):
                        if run.get('proposal_id'):
                            cached = deepcopy(ops.get(state['proposals'], run['proposal_id'], '提案'))
                            if cached['status'] == 'ready' and not ops.proposal_current(cached, state):
                                cached['status'] = 'stale'
                            return {'cached': cached}
                        if run['status'] == 'running' and parse_time(run['expires_at']) > started:
                            raise DomainError('run_in_progress', 'この事件を分析中です。')
                        raise DomainError('run_interrupted', '事件の実行が中断されました。手動で新しく分析してください。')
                if not incident:
                    raise DomainError('invalid_incident', '有効な事件ではありません。', 422)
            context = body.model_dump()
            context['service_date'] = target
            if body.parent_run_id:
                parent = state['agent_runs'].get(body.parent_run_id)
                if not parent or not parent.get('proposal_id'):
                    raise DomainError('invalid_parent', '前の実行が見つかりません。', 422)
                old = ops.get(state['proposals'], parent['proposal_id'], '提案')
                waiting = old['status'] == 'waiting_input'
                retrying = old['status'] in {'failed', 'stale'} or (old['status'] == 'ready' and not ops.proposal_current(old, state))
                if old['mode'] != body.mode or not (waiting or retrying):
                    raise DomainError('invalid_parent', '確認待ち・失敗・期限切れの同じ分析モードで続けてください。', 422)
                if retrying and body.answers:
                    raise DomainError('invalid_answer', '失敗・期限切れの再分析には回答を指定できません。', 422)
                old_target = old.get('service_date')
                if not old_target:
                    old_day = old.get('snapshot', {}).get('store', {}).get('service_date')
                    if old_day:
                        old_target = (date.fromisoformat(old_day) + timedelta(days=1 if body.mode == 'post_close' else 0)).isoformat()
                if old_target and old_target != target:
                    raise DomainError('invalid_parent', '同じ対象営業日の分析で続けてください。', 422)
                questions = {q['id']: q for q in old.get('questions', []) if isinstance(q, dict)}
                for qid, answer in body.answers.items():
                    q = questions.get(qid)
                    if not q:
                        raise DomainError('invalid_answer', 'この実行の質問IDではありません。', 422)
                    field = q['field']
                    allowed = {'objective': {'balanced', 'preserve_drinks', 'reduce_delay'}, 'allow_menu_pause': {'true', 'false'}, 'review_after_minutes': {'15', '30'}}
                    if field in allowed:
                        if answer not in allowed[field] or (q.get('options') and answer not in q['options']):
                            raise DomainError('invalid_answer', '回答の選択値が不正です。', 422)
                        if field == 'objective':
                            context['objective'] = answer
                        elif field == 'allow_menu_pause':
                            context['constraints']['allow_menu_pause'] = answer == 'true'
                        else:
                            context['constraints']['allowed_review_minutes'] = [int(answer)]
                    else:
                        state_field = 'closes' if field == 'close' else field
                        old_snapshot = old.get('snapshot', {})
                        if state_field in old_snapshot:
                            previous, current = deepcopy(old_snapshot[state_field]), deepcopy(state.get(state_field))
                            if field == 'inventory':
                                from .inventory import ensure
                                # A legacy lot migration is not a manager's
                                # stock correction and must not answer a fact.
                                for rows in (previous, current):
                                    for row in rows:
                                        ensure(row, old_snapshot['business_now'])
                            changed = current != previous
                        else:
                            changed = state['revision'] != old['revision']
                        if answer != 'updated' or not changed:
                            raise DomainError('facts_not_updated', '質問対象の勤務・在庫などを運営画面で先に保存してください。', 422)
                if 'objective' not in body.model_fields_set and not any(questions[qid]['field'] == 'objective' for qid in body.answers):
                    context['objective'] = old.get('objective', 'balanced')
                if 'constraints' not in body.model_fields_set:
                    inherited = deepcopy(old.get('constraints', context['constraints']))
                    inherited.update({k: v for k, v in context['constraints'].items() if any(questions[qid]['field'] == ('review_after_minutes' if k == 'allowed_review_minutes' else k) for qid in body.answers)})
                    context['constraints'] = inherited
                if 'locale' not in body.model_fields_set:
                    context['locale'] = old.get('locale', body.locale)
                context['parent_summary'] = old['summary']
            elif body.answers:
                raise DomainError('invalid_answer', '回答には前の実行IDが必要です。', 422)
            for entry in state["agent_runs"].values():
                if entry["key"] == idempotency_key:
                    if entry["digest"] != digest:
                        raise DomainError("idempotency_conflict", "同じキーの入力が異なります。")
                    if entry["status"] == "running" and parse_time(entry["expires_at"]) > started:
                        raise DomainError("run_in_progress", "同じ計算を実行中です。完了後に再確認してください。")
                    raise DomainError("run_interrupted", "この実行は中断されました。新しい操作として再計算してください。")
            if body.mode == 'in_service' and (state.get('closed') or not ops.open_time(state) <= ops.now(state) < ops.close_time(state)):
                raise DomainError('outside_service', '営業中に実行してください。準備または翌日計画を選択できます。')
            if any(run["status"] == "running" and parse_time(run["expires_at"]) > started for run in state["agent_runs"].values()):
                raise DomainError("run_in_progress", "この店舗の運営案を計算中です。完了後に再実行してください。")
            state['agent_runs'][run_id] = {'id': run_id, 'key': idempotency_key, 'digest': digest, 'status': 'running',
                'phase': 'snapshot', 'trace': [], 'started_at': iso(started), 'revision': state['revision'],
                'parent_run_id': body.parent_run_id, 'trigger_event_id': body.trigger_event_id,
                'expires_at': iso(started+timedelta(seconds=time_budget))}
            if body.mode == 'in_service' and state.get('replan_required'):
                covered = state.get('replan_event') or {}
                state['agent_runs'][run_id]['covered_event_id'] = covered.get('id')
                state['replan_required'] = False
            snapshot = deepcopy(state)
            snapshot.pop("idempotency", None)
            snapshot.pop("agent_runs", None)
            # Historical proposal details aren't needed by operations, and recursive snapshots grow rapidly.
            snapshot['proposals'] = [{k: p[k] for k in ('id', 'type', 'status', 'created_at', 'prep') if k in p} for p in snapshot['proposals']]
            snapshot["snapshot_id"] = str(uuid4())
            snapshot["request_context"] = context
            state["agent_runs"][run_id]["snapshot_id"] = snapshot["snapshot_id"]
            return {"snapshot": snapshot}

        entry = scoped_repository.transact(register)
        if "cached" in entry:
            return entry["cached"]
        snapshot = entry["snapshot"]
        gemini = os.getenv("SHIFTPATCH_AI_MODE", "calculation_only") == "gemini"
        cooldown = max(0, int(os.getenv("SHIFTPATCH_AI_COOLDOWN_SECONDS", "15" if gemini else "0")))
        store_limit = max(1, int(os.getenv("SHIFTPATCH_AI_STORE_DAILY_LIMIT", "10" if gemini else "100")))
        global_limit = max(1, int(os.getenv("SHIFTPATCH_AI_GLOBAL_DAILY_LIMIT", "100" if gemini else "10000")))
        checks = []
        if cooldown:
            checks.append(("ai-cooldown:" + store_id, 1, cooldown))
        checks += [("ai-store:" + store_id, store_limit, 86400), ("ai-global", global_limit, 86400)]
        try:
            for quota_key, limit, window in checks:
                if not identities.consume_quota(quota_key, limit, window, now=time.time()):
                    raise DomainError("rate_limited", "運営案の計算回数が上限に達しました。時間をおいて再実行してください。", 429)
        except Exception:
            def deny(state):
                # A rejected quota check has not started an agent execution. Do
                # not consume the incident or reserve its request key forever.
                denied = state["agent_runs"].pop(run_id, None)
                if denied and denied.get('covered_event_id') and denied['covered_event_id'] == (state.get('replan_event') or {}).get('id'):
                    state['replan_required'] = True
            scoped_repository.transact(deny)
            raise
        from .agent.runner import generate_proposal
        from .agent.dispatcher import BoundaryViolation
        last_progress = [None, 0, 0.0]
        def progress(phase, trace):
            signature = (phase, len(trace))
            if signature == tuple(last_progress[:2]) and time.monotonic() - last_progress[2] < 1:
                return
            def save_progress(state):
                run = state['agent_runs'].get(run_id)
                if not run or run.get('cancel_requested'):
                    raise BoundaryViolation('店長が分析を中断しました。')
                if parse_time(run["expires_at"]) <= datetime.now(timezone.utc):
                    from .agent.dispatcher import BudgetExceeded
                    raise BudgetExceeded("実行期限切れ")
                if run['status'] != 'running':
                    raise BoundaryViolation('実行が中断されました。')
                run.update(phase=phase, trace=deepcopy(trace), updated_at=iso(datetime.now(timezone.utc)))
            scoped_repository.transact(save_progress)
            last_progress[:] = [phase, len(trace), time.monotonic()]
        try:
            async with asyncio.timeout(time_budget - 1):
                import inspect
                kwargs = {'progress': progress} if 'progress' in inspect.signature(generate_proposal).parameters else {}
                result = await generate_proposal(snapshot, body.mode, snapshot['request_context']['objective'], **kwargs)
        except TimeoutError:
            result = {"status": "failed", "error": "budget_exceeded", "summary": "時間切れ", "source": os.getenv("SHIFTPATCH_AI_MODE", "calculation_only"), "candidates": [], "trace": []}
        except Exception:
            logger.exception("Proposal generation failed: %s", run_id)
            result = {"status": "failed", "error": "提案の生成に失敗しました。入力または接続設定を確認してください。", "summary": "生成失敗", "source": os.getenv("SHIFTPATCH_AI_MODE", "calculation_only"), "candidates": [], "trace": []}
        proposal_id = str(uuid4())
        result.update(id=proposal_id, run_id=run_id, parent_run_id=body.parent_run_id, trigger_event_id=body.trigger_event_id, service_date=snapshot['request_context']['service_date'], constraints=snapshot["request_context"]["constraints"], locale=snapshot['request_context']['locale'], mode=body.mode, type={"pre_open": "prep_plan", "in_service": "service_plan", "post_close": "next_day_plan"}[body.mode],
                      revision=snapshot["revision"], created_at=snapshot["business_now"], snapshot_id=snapshot["snapshot_id"])
        result.setdefault("recommended_candidate_id", None)
        result.setdefault("review_at", iso(parse_time(snapshot["business_now"])+timedelta(minutes=30)))
        result.setdefault("status", "ready")
        result["snapshot"] = {k: snapshot[k] for k in ["store", "business_now", "revision", "staff", "next_day_staff", "menus", "inventory", "deliveries", "orders", "mode", "history", "closes"]}

        def finish(state):
            execution = state["agent_runs"].get(run_id)
            if not execution or execution["status"] != "running":
                raise DomainError("run_interrupted", "初期化または中断された実行です。再計算してください。")
            if parse_time(execution["expires_at"]) <= datetime.now(timezone.utc):
                result.update(status="failed", recommended_candidate_id=None,
                              error="budget_exceeded", summary="実行期限切れ")
            if result["status"] == "ready" and (not ops.proposal_current(result, state) or (state.get("clock", {}).get("mode") != "realtime" and state["business_now"] != snapshot["business_now"])):
                result["status"] = "stale"
            if execution.get('cancel_requested'):
                result.update(status='failed', error='interrupted', recommended_candidate_id=None, summary='分析を中断しました。')
            execution['status'] = ('interrupted' if result.get('error') == 'interrupted' else 'timed_out' if result.get('error') == 'budget_exceeded' else 'succeeded' if result['status'] == 'ready' else result['status'])
            if result['status'] == 'failed' and not result.get('trace'):
                result['trace'] = deepcopy(execution.get('trace', []))
            execution.update(phase=result.get('phase', execution.get('phase', 'snapshot')), trace=deepcopy(result.get('trace', [])), finished_at=iso(datetime.now(timezone.utc)))
            execution["proposal_id"] = proposal_id
            state["proposals"].insert(0, deepcopy(result))
            state["idempotency"][idempotency_key] = {"digest": digest, "result": {"proposal_id": proposal_id}}
            return result

        return scoped_repository.transact(finish)

    @app.get("/api/stores/{store_id}/agent-runs/{run_id}")
    def agent_run(store_id: str, run_id: str, request: Request, scoped_repository=Depends(store_repository)):
        entry = scoped_repository.read()["agent_runs"].get(run_id)
        if not entry:
            raise DomainError("not_found", "実行が見つかりません。", 404)
        if entry["status"] == "running" and parse_time(entry["expires_at"]) < datetime.now(timezone.utc):
            entry["status"] = "interrupted"
        return {k: v for k, v in entry.items() if k not in {"key", "digest"}}

    @app.post('/api/stores/{store_id}/agent-runs/{run_id}/cancel')
    def cancel_run(store_id: str, run_id: str, body: Body, idempotency_key: IdempotencyKey, request: Request, scoped_repository=Depends(store_repository)):
        def cancel(state):
            run = state['agent_runs'].get(run_id)
            if not run:
                raise DomainError('not_found', '実行が見つかりません。', 404)
            if run['status'] == 'running':
                run['cancel_requested'] = True
            return {'ok': True}
        return mutate(scoped_repository, idempotency_key, 'cancel/'+run_id, {}, cancel)

    @app.post('/api/stores/{store_id}/proposals/{proposal_id}/dismiss')
    def dismiss(store_id: str, proposal_id: str, body: Body, idempotency_key: IdempotencyKey, request: Request, scoped_repository=Depends(store_repository)):
        mutate(scoped_repository, idempotency_key, 'dismiss/'+proposal_id, {}, lambda s: ops.dismiss(s, proposal_id))
        return present(scoped_repository, request)

    @app.post("/api/stores/{store_id}/proposals/{proposal_id}/approve")
    def approve(store_id: str, proposal_id: str, body: ApprovalBody, idempotency_key: IdempotencyKey, request: Request, scoped_repository=Depends(store_repository)):
        mutate(scoped_repository, idempotency_key, f"approve/{proposal_id}", body.model_dump(), lambda s: ops.approve(s, proposal_id, body.candidate_id, body.expected_revision))
        return present(scoped_repository, request)

    @app.post("/api/stores/{store_id}/proposals/{proposal_id}/acknowledge")
    def acknowledge(store_id: str, proposal_id: str, body: Body, idempotency_key: IdempotencyKey, request: Request, scoped_repository=Depends(store_repository)):
        mutate(scoped_repository, idempotency_key, f"acknowledge/{proposal_id}", {}, lambda s: ops.acknowledge(s, proposal_id))
        return present(scoped_repository, request)

    @app.post("/api/stores/{store_id}/service-modes/current/review")
    def mode_review(store_id: str, body: ModeReview, idempotency_key: IdempotencyKey, request: Request, scoped_repository=Depends(store_repository)):
        mutate(scoped_repository, idempotency_key, "review", body.model_dump(), ops.release_mode)
        return present(scoped_repository, request)

    @app.post("/api/stores/{store_id}/closes")
    def close(store_id: str, body: Body, idempotency_key: IdempotencyKey, request: Request, scoped_repository=Depends(store_repository)):
        result = mutate(scoped_repository, idempotency_key, "close", {}, ops.close_day)
        return {**result, "overview": present(scoped_repository, request)}

    @app.patch("/api/stores/{store_id}/history/{business_date}")
    def history(store_id: str, business_date: str, body: HistoryPatch, idempotency_key: IdempotencyKey, request: Request, scoped_repository=Depends(store_repository)):
        values = body.model_dump()
        mutate(scoped_repository, idempotency_key, f"history/{business_date}", values, lambda s: ops.update_history(s, business_date, body.counts, body.restricted, values['windows']))
        return present(scoped_repository, request)

    @app.post('/api/stores/{store_id}/clock')
    def clock_mode(store_id: str, body: ClockBody, idempotency_key: IdempotencyKey, request: Request, scoped_repository=Depends(store_repository)):
        from .clock import set_mode
        def change(state):
            set_mode(state, body.mode)
            ops.bump(state)
            return {'ok': True}
        mutate(scoped_repository, idempotency_key, 'clock-mode', body.model_dump(), change)
        return present(scoped_repository, request)

    @app.post("/api/stores/{store_id}/demo/advance")
    def advance(store_id: str, body: Advance, idempotency_key: IdempotencyKey, request: Request, scoped_repository=Depends(store_repository)):
        mutate(scoped_repository, idempotency_key, "advance", body.model_dump(), lambda s: ops.advance(s, body.minutes))
        return present(scoped_repository, request)

    @app.post("/api/stores/{store_id}/demo/reset")
    def reset(store_id: str, body: Body, idempotency_key: IdempotencyKey, request: Request, scoped_repository=Depends(store_repository)):
        # Reset is an explicit demo action; retain its key even while deleting previous demo records.
        digest = fingerprint("reset", {})
        def reset_operation(state):
            prior = state["idempotency"].get(idempotency_key)
            if prior:
                if prior["digest"] != digest:
                    raise DomainError("idempotency_conflict", "同じキーの入力が異なります。")
                return
            check_reserved_key(state, idempotency_key)
            revision = state["revision"]+1
            state.clear()
            state.update(sample_state(store_id))
            state["revision"] = revision
            state["idempotency"][idempotency_key] = {"digest": digest, "result": {"ok": True}}
        scoped_repository.transact(reset_operation)
        return present(scoped_repository, request)

    if os.getenv("SHIFTPATCH_WEB_DIST"):
        from .web_host import mount_web
        mount_web(app, os.getenv("SHIFTPATCH_WEB_DIST"))
    return app


app = create_app()
