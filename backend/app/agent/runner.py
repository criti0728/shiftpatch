"""Bounded observe → choose tool → observe loop, with an honest offline path."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
import logging
import os
from time import monotonic
from uuid import uuid4

from app.timeutils import iso, parse_time
from app.simulation.engine import ENGINE_VERSION
from .dispatcher import AgentError, BoundaryViolation, BudgetExceeded, Dispatcher
from .model_client import GeminiTransport, ModelReply, ModelTransport
from .prompts import PROMPT_VERSION, system_prompt
from .result_validator import validate_final
from .schemas import declarations

logger = logging.getLogger(__name__)
MAX_MODEL_CALLS = 6
MAX_TOOLS = 12
MAX_CANDIDATES = 5
MAX_REQUESTED_TOOLS = 24
MAX_TOOLS_PER_REPLY = 4
TOTAL_SECONDS = 40
SAVE_RESERVE_SECONDS = 5
MAX_MODEL_INPUT_CHARACTERS = 120_000
MAX_REPORTED_TOTAL_TOKENS = 60_000


def _missing_inputs(snapshot, mode):
    if mode == "post_close":
        if not any(close.get("service_date", close.get("date")) == snapshot["store"]["service_date"]
                   for close in snapshot.get("closes", [])):
            return ["本日の営業を締めて、実際の販売数・在庫を確定してください。"]
        if not snapshot.get("next_day_staff"):
            return ["翌営業日の確定シフトとメニュー別の作業時間を登録してください。"]
    staff = snapshot.get("next_day_staff", []) if mode == "post_close" else snapshot.get("staff", [])
    if not staff:
        return ["対象営業日の確定シフトを登録してください。"]
    missing = [person["name"] for person in staff
               if not person.get("absent") and not person.get("skills")]
    if missing:
        return ["メニュー別の作業時間を登録してください: " + "、".join(missing)]
    if not snapshot.get("history"):
        return ["販売予測の基準となる過去の販売数を登録してください。"]
    return []


def _initial_context(snapshot, mode, objective, baseline, target_date):
    # Only a summary is injected: the model chooses which detailed data to inspect.
    people = snapshot.get("next_day_staff", []) if mode == "post_close" else snapshot["staff"]
    return {
        "mode": mode, "objective": objective, "service_date": snapshot["store"]["service_date"],
        "target_service_date": target_date,
        "as_of": snapshot["business_now"], "revision": snapshot["revision"],
        "snapshot_scope": "server-authorized single store; read-only",
        "menu_ids": [menu["id"] for menu in snapshot["menus"]],
        "summary": {"absent_staff_count": sum(bool(s["absent"]) for s in people),
                    "waiting_orders": sum(o["status"] != "completed" for o in snapshot["orders"]),
                    "inventory_mismatch_count": sum(i["on_hand"] < i["reserved"] for i in snapshot["inventory"])},
        "recent_events": snapshot.get("events", [])[:3],
        "request_context": snapshot.get('request_context', {}),
        "baseline": baseline,
    }


def _calculation_only(dispatcher, proposal, objective):
    """Explicit server examples, never presented as model actions or recommendations."""
    from app.planning.prep import calculate_prep
    if dispatcher.mode == "in_service":
        dispatcher.baseline()
        dispatcher.tool_count += 1
        menus = dispatcher.snapshot["menus"]
        alternatives = [
            ("フードを少量ずつ受け付ける", {m["id"]: 20 if m["category"] == "drink" else 2 for m in menus}),
            ("全メニューを少量ずつ受け付ける", {m["id"]: 5 if m["category"] == "drink" else 1 for m in menus}),
        ]
        for title, limits in alternatives:
            spec = {"title": title, "action": "replace_limits", "limits": limits, "review_after_minutes": 30}
            candidate = dispatcher.candidate(spec, "server")
            dispatcher.record("rule_based_simulation", spec, candidate, "server")
            dispatcher.tool_count += 1
        proposal["summary"] = "AI未接続のため、サーバーの固定比較案を同じ条件で計算しました。指標を比較して選択してください。AIによる推薦はありません。"
    else:
        prep = calculate_prep(deepcopy(dispatcher.snapshot), service_date=dispatcher.date)
        proposal["prep"] = prep
        dispatcher.record("calculate_prep" if dispatcher.mode == "pre_open" else "calculate_next_day_prep",
                          {"service_date": dispatcher.date}, {"prep": prep}, "server")
        dispatcher.tool_count += 1
        proposal["summary"] = "AI未接続のため、登録された勤務・実在庫・過去の販売記録から準備量を計算しました。実際の発注は行っていません。"
    proposal["candidates"] = dispatcher.candidates


def _reply_content(reply):
    if reply.content is not None:
        return reply.content
    # Used by test transports; the SDK path always retains the original Content.
    if reply.calls:
        return {"role": "model", "parts": [{"function_call": {"name": call.name, "args": call.args,
                                                               **({"id": call.id} if call.id else {})}}
                                            for call in reply.calls]}
    return {"role": "model", "parts": [{"text": json.dumps(reply.final, ensure_ascii=False)}]}


def _input_size(messages, system, specs):
    """Bound serialized input size without guessing an exact tokenizer count."""
    def serializable(value):
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json", exclude_none=True)
        raise TypeError("Unsupported model input")
    return len(json.dumps({"system": system, "tools": specs, "messages": messages},
                          ensure_ascii=False, default=serializable))


def _record_usage(proposal, usage):
    if not usage:
        return
    totals = proposal.setdefault("token_usage", {})
    for key, count in usage.items():
        if type(count) is int and count >= 0:
            totals[key] = totals.get(key, 0) + count


def structured_questions(questions):
    from .schemas import Question
    result = []
    for index, q in enumerate(questions):
        if isinstance(q, Question):
            result.append(q.model_dump())
        elif isinstance(q, dict):
            result.append(q)
        else:
            field = 'close' if '締め' in q else 'next_day_staff' if '翌' in q else 'history' if '販売数' in q else 'staff'
            result.append({'id': f'input-{index+1}', 'text': q, 'field': field, 'options': []})
    return result


async def _run_model(dispatcher, proposal, objective, transport, started):
    baseline = dispatcher.baseline() if dispatcher.mode == "in_service" else None
    if baseline:
        dispatcher.tool_count += 1
    messages = [{"role": "user", "parts": [{"text": json.dumps(
        _initial_context(dispatcher.snapshot, dispatcher.mode, objective, baseline, dispatcher.date), ensure_ascii=False)}]}]
    system = system_prompt(dispatcher.mode)
    specs = declarations(dispatcher.mode)
    repairs = 0
    requested_tools = 0
    for index in range(MAX_MODEL_CALLS):
        remaining = TOTAL_SECONDS - SAVE_RESERVE_SECONDS - (monotonic() - started)
        if remaining <= 0:
            raise BudgetExceeded("分析の制限時間に達しました。再実行してください。")
        if _input_size(messages, system, specs) > MAX_MODEL_INPUT_CHARACTERS:
            raise BudgetExceeded("モデルに渡すデータ量の上限に達しました。対象を絞って再実行してください。")
        if proposal.get("token_usage", {}).get("total_token_count", 0) >= MAX_REPORTED_TOTAL_TOKENS:
            raise BudgetExceeded("モデルの累積トークン利用量が上限に達しました。")
        proposal['phase'] = 'explain' if len(dispatcher.candidates) >= 3 or any(r['tool'].startswith('calculate_') for r in dispatcher.results.values()) else 'investigate'
        if dispatcher.progress:
            dispatcher.progress(proposal['phase'], dispatcher.trace)
        proposal["model_calls"] = index + 1
        try:
            pending = asyncio.create_task(transport.generate(messages, system, specs))
            try:
                async with asyncio.timeout(remaining):
                    while not pending.done():
                        await asyncio.wait({pending}, timeout=0.5)
                        if dispatcher.progress:
                            dispatcher.progress(proposal['phase'], dispatcher.trace)
                    reply = await pending
            finally:
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
        except TimeoutError as exc:
            raise BudgetExceeded("モデル応答が制限時間に達しました。再実行してください。") from exc
        except (ValueError, ConnectionError, OSError) as exc:
            _record_usage(proposal, getattr(transport, "last_usage", {}))
            if repairs >= 1:
                raise AgentError("Gemini応答の取得・解析に失敗しました。") from exc
            repairs += 1
            dispatcher.record("model_retry", {}, {"reason": "応答の取得・解析に失敗したため1回再試行します。"}, "server")
            continue
        if not isinstance(reply, ModelReply):
            raise AgentError("モデル境界から不正な応答を受信しました。")
        _record_usage(proposal, reply.usage)
        messages.append(_reply_content(reply))
        if reply.calls:
            requested_tools += len(reply.calls)
            proposal["requested_tool_calls"] = requested_tools
            if len(reply.calls) > MAX_TOOLS_PER_REPLY or requested_tools > MAX_REQUESTED_TOOLS or reply.final is not None:
                raise BoundaryViolation("1応答のツール呼出し数または応答形式が不正です。")
            responses = []
            for call in reply.calls:
                if monotonic() - started >= TOTAL_SECONDS - SAVE_RESERVE_SECONDS:
                    raise BudgetExceeded("分析の制限時間に達しました。")
                if dispatcher.progress:
                    dispatcher.progress('calculate' if call.name in {'simulate_service_plan', 'calculate_prep', 'calculate_next_day_prep', 'estimate_sales', 'compare_plan_actual'} else 'investigate', dispatcher.trace)
                result = dispatcher.execute(call.name, call.args)
                result['call_id'] = call.id or result['call_id']
                dispatcher.trace[-1]["model_call"] = index + 1
                dispatcher.trace[-1]["call_id"] = result["call_id"]
                function_response = {"name": call.name, "response": result}
                if call.id:
                    function_response["id"] = call.id
                responses.append({"function_response": function_response})
            messages.append({"role": "user", "parts": responses})
            continue
        try:
            proposal['phase'] = 'validate'
            if dispatcher.progress:
                dispatcher.progress('validate', dispatcher.trace)
            answer = validate_final(reply.final, dispatcher)
        except (ValueError, TypeError) as exc:
            if repairs >= 1:
                raise AgentError("最終結果の参照または形式を検証できませんでした。") from exc
            repairs += 1
            error = dispatcher.record("validate_final", {}, {"error": str(exc)[:1200]}, "server")
            messages.append({"role": "user", "parts": [{"text": json.dumps({
                "validation_error": error, "instruction": "Correct the JSON or obtain missing tool results. One correction remains; do not invent IDs."
            }, ensure_ascii=False)}]})
            continue
        proposal.update(
            status="waiting_input" if answer.outcome == "needs_input" else "ready",
            recommended_candidate_id=answer.recommended_candidate_id,
            summary=answer.summary, questions=structured_questions(answer.questions),
            outcome=answer.outcome, evidence_refs=answer.evidence_refs,
        )
        for note in answer.candidate_notes:
            candidate = next(c for c in dispatcher.candidates if c["id"] == note.candidate_id)
            candidate["explanation"] = note.summary
            candidate["tradeoff"] = note.tradeoff
            candidate["evidence_refs"] = note.evidence_refs
        if dispatcher.mode != "in_service" and answer.outcome == "ready":
            proposal["prep"] = deepcopy(dispatcher.require_result(answer.plan_result_id)["prep"])
        return
    raise BudgetExceeded("モデル呼び出しの上限に達しました。検証済みの最終提案は得られませんでした。")


async def generate_proposal(snapshot: dict, mode: str, objective: str, *,
                            transport: ModelTransport | None = None, progress=None) -> dict:
    """Generate without writes. Caller owns authorization, storage and revision checks.

    An injected transport exercises the Gemini contract in tests only. Runtime mode
    still comes from SHIFTPATCH_AI_MODE so test doubles cannot silently replace AI.
    """
    if mode not in {"pre_open", "in_service", "post_close"}:
        raise ValueError("未知の提案モードです。")
    if objective not in {"balanced", "preserve_drinks", "reduce_delay"}:
        raise ValueError("未知の目標です。")
    source = os.environ.get("SHIFTPATCH_AI_MODE", "calculation_only")
    if source not in {"calculation_only", "gemini"}:
        raise ValueError("SHIFTPATCH_AI_MODE は calculation_only または gemini です。")
    started = monotonic()
    proposal = {
        "id": f"proposal-{uuid4().hex}", "mode": mode,
        "type": {"pre_open": "prep_plan", "in_service": "service_plan", "post_close": "next_day_plan"}[mode],
        "status": "ready", "revision": snapshot["revision"], "source": source,
        "created_at": snapshot["business_now"],
        "review_at": iso(parse_time(snapshot["business_now"]) + timedelta(minutes=30)),
        "recommended_candidate_id": None, "summary": "", "candidates": [], "trace": [],
        "objective": objective, "prompt_version": PROMPT_VERSION, "model_calls": 0,
        "snapshot_id": snapshot.get("snapshot_id") or f"snapshot-{uuid4().hex}",
        "scenario_seed": 42, "engine_version": ENGINE_VERSION,
        "token_usage": {}, "phase": "snapshot",
    }
    questions = _missing_inputs(snapshot, mode)
    if questions:
        proposal.update(status="waiting_input", questions=structured_questions(questions), summary="不足している運営データを確認してください。")
        return proposal
    private_snapshot = deepcopy(snapshot)
    private_snapshot["snapshot_id"] = proposal["snapshot_id"]
    dispatcher = Dispatcher(private_snapshot, mode, proposal["trace"], max_tools=MAX_TOOLS, max_candidates=MAX_CANDIDATES, progress=progress, constraints=snapshot.get("request_context", {}).get("constraints"))
    own_transport = None
    try:
        async with asyncio.timeout(TOTAL_SECONDS):
            if source == "calculation_only":
                _calculation_only(dispatcher, proposal, objective)
            else:
                if transport is None:
                    own_transport = GeminiTransport()
                    transport = own_transport
                proposal["model_id"] = getattr(transport, "model", "injected-test-transport")
                await _run_model(dispatcher, proposal, objective, transport, started)
            proposal['phase'] = 'validate'
            if dispatcher.progress:
                dispatcher.progress('validate', dispatcher.trace)
            if monotonic() - started >= TOTAL_SECONDS:
                raise BudgetExceeded("分析の制限時間に達しました。")
    except (TimeoutError, BudgetExceeded) as exc:
        proposal.update(status="failed", error="budget_exceeded", recommended_candidate_id=None,
                        summary=str(exc) or "分析がタイムアウトしました。再実行してください。")
    except (AgentError, ValueError, ImportError) as exc:
        proposal.update(status="failed", error="agent_failed", recommended_candidate_id=None, summary=str(exc))
    except Exception:
        # Authentication/provider errors should not leak credentials or turn into a fake success.
        logger.error("Agent execution failed (provider details suppressed)")
        proposal.update(status="failed", recommended_candidate_id=None,
                        error="model_unavailable" if source == "gemini" else "calculation_failed",
                        summary="Geminiへの接続に失敗しました。認証・モデル・プロジェクト設定を確認してください。"
                        if source == "gemini" else "計算に失敗しました。登録データを確認して再実行してください。")
    finally:
        proposal["candidates"] = dispatcher.candidates
        proposal["results"] = deepcopy(dispatcher.results)
        proposal["tool_calls"] = dispatcher.tool_count
        proposal["elapsed_ms"] = round((monotonic() - started) * 1000)
        if own_transport is not None:
            try:
                await asyncio.wait_for(own_transport.aclose(), timeout=1)
            except Exception:
                logger.warning("Gemini client cleanup did not complete")
    return proposal
