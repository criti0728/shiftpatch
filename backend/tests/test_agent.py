"""Contract tests with scripted transports; these are not live Gemini evaluation."""
import asyncio
from copy import deepcopy
import json

import pytest

from app.agent import runner
from app.agent.dispatcher import BudgetExceeded, Dispatcher
from app.agent.model_client import ModelReply, ToolCall
from app.agent.schemas import declarations
from app.seed import seed_state


def generate(snapshot=None, mode="in_service", transport=None):
    return asyncio.run(runner.generate_proposal(snapshot or seed_state(), mode, "preserve_drinks", transport=transport))


class Replies:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.messages = []

    async def generate(self, messages, system_instruction, declarations):
        self.messages.append(deepcopy(messages))
        return self.replies.pop(0)


def response_result(messages):
    return messages[-1]["parts"][0]["function_response"]["response"]


class AdaptiveScript:
    """A test double chooses different tools and revises after real engine output."""
    def __init__(self):
        self.turn = 0
        self.observed = []

    async def generate(self, messages, system_instruction, declarations):
        self.turn += 1
        if self.turn == 1:
            context = json.loads(messages[0]["parts"][0]["text"])
            self.menu_ids = context["menu_ids"]
            self.absent = context["summary"]["absent_staff_count"] > 0
            return ModelReply(calls=[ToolCall(
                "read_staff_menu_times" if self.absent else "read_inventory",
                {"menu_ids": ["sandwich"]} if self.absent else {"ingredient_ids": ["bread"]},
            )])
        observation = response_result(messages)
        self.observed.append(observation)
        if self.turn == 2:
            if self.absent:
                active_times = [person["skills"]["sandwich"] for person in observation["data"]["staff"]
                                if not person["absent"] and "sandwich" in person["skills"]]
                food_limit = 1 if min(active_times) > 200 else 3
            else:
                bread = observation["data"]["inventory"][0]
                food_limit = max(1, min(3, (bread["on_hand"] - bread["reserved"]) // 2))
            return ModelReply(calls=[ToolCall("simulate_service_plan", {"candidate_spec": {
                "title": "観測条件に合わせた初回候補",
                "limits": {menu_id: food_limit if menu_id == "sandwich" else 5 for menu_id in self.menu_ids},
                "based_on_result_ids": [observation["result_id"]],
            }})])
        if self.turn == 3:
            candidate = observation["data"]
            # The chosen edit uses actual simulated pending work, not a fixed returned candidate.
            limits = deepcopy(candidate["limits"])
            limits["sandwich"] = 0 if candidate["metrics"]["pending_units"] else max(0, limits["sandwich"] - 1)
            return ModelReply(calls=[ToolCall("simulate_service_plan", {"candidate_spec": {
                "title": "計算結果を受けて修正した候補", "limits": limits,
                "parent_candidate_id": candidate["id"],
                "based_on_result_ids": [observation["result_id"]],
            }})])
        candidate = observation["data"]
        return ModelReply(final={
            "outcome": "ready", "summary": "既存注文を維持しながらフード受付を調整します。",
            "recommended_candidate_id": candidate["id"],
            "evidence_refs": [observation["result_id"]],
            "candidate_notes": [{"candidate_id": candidate["id"], "summary": "計算結果から受付量を見直しました。",
                                 "evidence_refs": [observation["result_id"]],
                                 "tradeoff": "フードの販売機会が減ります。"}],
        })


def test_calculation_only_has_real_metrics_without_fake_ai(monkeypatch):
    monkeypatch.setenv("SHIFTPATCH_AI_MODE", "calculation_only")
    state = seed_state()
    original = deepcopy(state)
    proposal = generate(state)
    assert proposal["status"] == "ready"
    assert proposal["source"] == "calculation_only"
    assert proposal["recommended_candidate_id"] is None
    assert proposal["model_calls"] == 0
    assert len(proposal["candidates"]) == 3
    assert all(event["origin"] == "server" for event in proposal["trace"])
    assert proposal["candidates"][1]["metrics"]["rejected_units"] > proposal["candidates"][0]["metrics"]["rejected_units"]
    assert state == original


def test_gemini_missing_configuration_is_explicit_failure(monkeypatch):
    monkeypatch.setenv("SHIFTPATCH_AI_MODE", "gemini")
    for name in ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "SHIFTPATCH_GEMINI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    proposal = generate()
    assert proposal["status"] == "failed"
    assert proposal["source"] == "gemini"
    assert "GOOGLE_CLOUD_PROJECT" in proposal["summary"]
    assert not proposal["candidates"]
    assert proposal["recommended_candidate_id"] is None


def test_model_observes_then_revises_and_preserves_snapshot(monkeypatch):
    monkeypatch.setenv("SHIFTPATCH_AI_MODE", "gemini")
    state = seed_state()
    state["staff"][2]["absent"] = True
    before = deepcopy(state)
    transport = AdaptiveScript()
    result = generate(state, transport=transport)
    assert result["status"] == "ready", result
    assert result["model_calls"] == 4
    assert result["recommended_candidate_id"] == "candidate-3"
    assert result["candidates"][2]["parent_candidate_id"] == "candidate-2"
    assert result["candidates"][2]["limits"] != result["candidates"][1]["limits"]
    assert result["trace"][1]["tool"] == "read_staff_menu_times"
    assert result["trace"][0]["origin"] == "server"
    assert all(item["origin"] == "model" for item in result["trace"][1:])
    assert result["trace"][3]["args"]["candidate_spec"]["based_on_result_ids"] == [result["trace"][2]["result_id"]]
    assert state == before


def test_scripted_scenarios_select_different_investigations(monkeypatch):
    monkeypatch.setenv("SHIFTPATCH_AI_MODE", "gemini")
    inventory_case = generate(transport=AdaptiveScript())
    absent_state = seed_state()
    absent_state["staff"][2]["absent"] = True
    staff_case = generate(absent_state, transport=AdaptiveScript())
    assert inventory_case["status"] == staff_case["status"] == "ready"
    assert inventory_case["trace"][1]["tool"] == "read_inventory"
    assert staff_case["trace"][1]["tool"] == "read_staff_menu_times"
    assert inventory_case["candidates"][1]["limits"] != staff_case["candidates"][1]["limits"]


@pytest.mark.parametrize("tool,args", [
    ("apply_service_mode", {"candidate_id": "candidate-1"}),
    ("read_inventory", {"store_id": "another-store"}),
])
def test_model_cannot_mutate_or_select_another_store(monkeypatch, tool, args):
    monkeypatch.setenv("SHIFTPATCH_AI_MODE", "gemini")
    state = seed_state()
    before = deepcopy(state)
    proposal = generate(state, transport=Replies(ModelReply(calls=[ToolCall(tool, args)])))
    assert proposal["status"] == "failed"
    assert proposal["recommended_candidate_id"] is None
    assert state == before


def test_unknown_candidate_rejected_after_single_repair(monkeypatch):
    monkeypatch.setenv("SHIFTPATCH_AI_MODE", "gemini")
    bad = ModelReply(final={"outcome": "ready", "summary": "架空の候補です。",
                            "recommended_candidate_id": "candidate-does-not-exist",
                            "evidence_refs": ["result-1"]})
    proposal = generate(transport=Replies(bad, bad))
    assert proposal["status"] == "failed"
    assert proposal["recommended_candidate_id"] is None
    assert proposal["model_calls"] == 2


def test_model_call_budget_terminates_even_when_tools_cached(monkeypatch):
    monkeypatch.setenv("SHIFTPATCH_AI_MODE", "gemini")
    repeated = ModelReply(calls=[ToolCall("read_queue", {})])
    proposal = generate(transport=Replies(*[repeated] * 6))
    assert proposal["status"] == "failed"
    assert proposal["error"] == "budget_exceeded"
    assert proposal["model_calls"] == 6
    assert proposal["tool_calls"] == 2  # baseline plus one noncached read
    assert proposal["trace"][-1]["cached"] is True


def test_execution_budget_and_candidate_validation():
    dispatcher = Dispatcher(seed_state(), "in_service", [], max_tools=1)
    first = dispatcher.execute("read_inventory", {"ingredient_ids": ["milk"]})
    assert "error" not in first["data"]
    with pytest.raises(BudgetExceeded):
        dispatcher.execute("read_queue", {})
    other = Dispatcher(seed_state(), "in_service", [])
    bad = other.execute("simulate_service_plan", {"candidate_spec": {
        "title": "不足した候補", "limits": {"latte": 2}, "based_on_result_ids": ["invented"],
    }})
    assert "error" in bad["data"]
    assert not other.candidates


def test_model_timeout_stops_without_recommendation(monkeypatch):
    monkeypatch.setenv("SHIFTPATCH_AI_MODE", "gemini")
    monkeypatch.setattr(runner, "TOTAL_SECONDS", 0.03)
    monkeypatch.setattr(runner, "SAVE_RESERVE_SECONDS", 0)

    class Slow:
        async def generate(self, *args):
            await asyncio.sleep(1)

    proposal = generate(transport=Slow())
    assert proposal["status"] == "failed"
    assert proposal["error"] == "budget_exceeded"
    assert proposal["recommended_candidate_id"] is None
    assert proposal["elapsed_ms"] < 500


def test_pre_open_and_close_modes_are_plans_not_sale_changes(monkeypatch):
    monkeypatch.setenv("SHIFTPATCH_AI_MODE", "calculation_only")
    result = generate(mode="pre_open")
    assert result["type"] == "prep_plan"
    assert result["prep"]["service_date"] == "2026-10-11"
    assert result["candidates"] == []
    waiting = generate(mode="post_close")
    assert waiting["status"] == "waiting_input"
    state = seed_state()
    state["closes"] = [{"id": "close", "service_date": "2026-10-11", "counts": {}, "forecast": {}}]
    next_day = generate(state, mode="post_close")
    assert next_day["status"] == "ready"
    assert next_day["prep"]["service_date"] == "2026-10-12"
    assert next_day["type"] == "next_day_plan"
    assert not next_day["candidates"]


def test_gemini_pre_open_assembles_only_referenced_server_prep(monkeypatch):
    monkeypatch.setenv("SHIFTPATCH_AI_MODE", "gemini")
    transport = Replies(
        ModelReply(calls=[ToolCall("estimate_sales", {})]),
        ModelReply(calls=[ToolCall("calculate_prep", {"sales_basis_result_id": "result-1"})]),
        ModelReply(final={
            "outcome": "ready", "summary": "在庫と勤務に基づく準備計画です。",
            "evidence_refs": ["result-1", "result-2"],
            "plan_result_id": "result-2", "sales_basis_result_id": "result-1",
        }),
    )
    proposal = generate(mode="pre_open", transport=transport)
    assert proposal["status"] == "ready", proposal
    assert proposal["prep"]["forecast"] == proposal["results"]["result-1"]["data"]["forecast"]
    assert proposal["recommended_candidate_id"] is None


def test_no_mutation_tool_is_exposed_in_any_mode():
    for mode in ("pre_open", "in_service", "post_close"):
        names = {item["name"] for item in declarations(mode)}
        assert not any(word in name for name in names for word in ("approve", "apply", "cancel", "order"))
        if mode != "in_service":
            assert "simulate_service_plan" not in names


def test_sdk_boundary_preserves_content_and_disables_auto_execution():
    """Use real SDK types but fake HTTP; not a live Gemini credentials check."""
    from types import SimpleNamespace
    from google.genai import types
    from app.agent.model_client import GeminiTransport

    raw_content = types.Content(role="model", parts=[
        types.Part(function_call=types.FunctionCall(name="read_queue", args={}, id="call-real-id"),
                   thought_signature=b"opaque-model-signature"),
    ])
    captured = {}

    class FakeModels:
        async def generate_content(self, **kwargs):
            captured.update(kwargs)
            return types.GenerateContentResponse(candidates=[types.Candidate(content=raw_content)])

    transport = GeminiTransport.__new__(GeminiTransport)
    transport.model = "configured-model"
    transport.client = SimpleNamespace(aio=SimpleNamespace(models=FakeModels()))
    previous = types.Content(role="model", parts=[types.Part(
        function_call=types.FunctionCall(name="read_inventory", args={}, id="previous"),
        thought_signature=b"keep-this",
    )])
    messages = [previous, {"role": "user", "parts": [{
        "function_response": {"name": "read_inventory", "id": "previous", "response": {"data": {}}},
    }]}]
    reply = asyncio.run(transport.generate(messages, "system", declarations("in_service")))
    assert reply.content is raw_content
    assert reply.calls[0].id == "call-real-id"
    assert captured["contents"][0] is previous
    assert captured["contents"][1].parts[0].function_response.id == "previous"
    assert captured["config"].automatic_function_calling.disable is True
    candidate_declaration = next(item for item in declarations("in_service") if item["name"] == "simulate_service_plan")
    candidate_properties = candidate_declaration["parameters_json_schema"]["properties"]["candidate_spec"]["properties"]
    assert "title" in candidate_properties


def test_reported_token_budget_stops_further_model_calls(monkeypatch):
    monkeypatch.setenv("SHIFTPATCH_AI_MODE", "gemini")
    reply = ModelReply(calls=[ToolCall("read_queue", {})],
                       usage={"total_token_count": runner.MAX_REPORTED_TOTAL_TOKENS + 1})
    proposal = generate(transport=Replies(reply))
    assert proposal["status"] == "failed"
    assert proposal["error"] == "budget_exceeded"
    assert proposal["model_calls"] == 1
    assert proposal["token_usage"]["total_token_count"] == runner.MAX_REPORTED_TOTAL_TOKENS + 1


def test_input_size_budget_stops_before_provider_call(monkeypatch):
    monkeypatch.setenv("SHIFTPATCH_AI_MODE", "gemini")
    monkeypatch.setattr(runner, "MAX_MODEL_INPUT_CHARACTERS", 100)
    proposal = generate(transport=Replies())
    assert proposal["status"] == "failed"
    assert proposal["error"] == "budget_exceeded"
    assert proposal["model_calls"] == 0


def test_transient_provider_failure_retries_only_once(monkeypatch):
    monkeypatch.setenv("SHIFTPATCH_AI_MODE", "gemini")

    class Unavailable:
        calls = 0

        async def generate(self, *args):
            self.calls += 1
            raise ConnectionError("Provider unavailable")

    transport = Unavailable()
    proposal = generate(transport=transport)
    assert transport.calls == 2
    assert proposal["status"] == "failed"
    assert proposal["source"] == "gemini"
    assert proposal["recommended_candidate_id"] is None


def test_candidate_note_must_reference_its_own_metrics():
    from app.agent.result_validator import validate_final
    dispatcher = Dispatcher(seed_state(), "in_service", [])
    baseline = dispatcher.baseline()
    inventory = dispatcher.execute("read_inventory", {})
    with pytest.raises(ValueError, match="候補自身"):
        validate_final({
            "outcome": "ready", "summary": "説明", "recommended_candidate_id": "candidate-1",
            "evidence_refs": [baseline["result_id"]],
            "candidate_notes": [{"candidate_id": "candidate-1", "summary": "説明",
                                 "tradeoff": "トレードオフ", "evidence_refs": [inventory["result_id"]]}],
        }, dispatcher)


def test_candidate_budget_deduplicates_then_stops_new_candidates():
    dispatcher = Dispatcher(seed_state(), "in_service", [])
    baseline = dispatcher.baseline()
    read = dispatcher.execute("read_inventory", {})
    for quantity in range(4):
        spec = {"title": f"案 {quantity}", "action": "replace_limits", "review_after_minutes": 30,
                "limits": {menu["id"]: quantity for menu in seed_state()["menus"]},
                "based_on_result_ids": [read["result_id"]]}
        dispatcher.candidate(spec)
    assert len(dispatcher.candidates) == 5
    # Same effective limits reuse the earlier candidate even after reaching the cap.
    assert dispatcher.candidate(spec)["id"] == "candidate-5"
    assert len(dispatcher.candidates) == 5
    spec["limits"]["latte"] += 1
    with pytest.raises(BudgetExceeded):
        dispatcher.candidate(spec)


@pytest.mark.parametrize("source", ["acknowledged_prep_plan", "historical_baseline_at_close"])
def test_close_comparison_preserves_forecast_provenance(source):
    state = seed_state()
    state["closes"] = [{"id": "close", "service_date": state["store"]["service_date"],
                        "closed_at": state["business_now"], "counts": {"latte": 3}, "forecast": {"latte": 5},
                        "forecast_source": source, "forecast_proposal_id": "earlier-plan" if source == "acknowledged_prep_plan" else None,
                        "notes": ["締めで確定した販売数です。"]}]
    dispatcher = Dispatcher(state, "post_close", [])
    close = dispatcher.execute("read_close_record", {})
    comparison = dispatcher.execute("compare_plan_actual", {"close_result_id": close["result_id"]})["data"]
    assert comparison["forecast_source"] == source
    assert comparison["forecast_proposal_id"] == state["closes"][0]["forecast_proposal_id"]
    assert comparison["errors"]["latte"] == -2
    assert state["closes"][0]["notes"][0] in comparison["notes"]
    assert any("過去の予測精度" in note for note in comparison["notes"]) is (source != "acknowledged_prep_plan")
