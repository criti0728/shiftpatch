from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

BASE = "/api/stores/demo"


class SampleClient(TestClient):
    store_id = None

    def request(self, method, url, *args, **kwargs):
        if self.store_id and isinstance(url, str):
            url = url.replace(BASE, "/api/stores/" + self.store_id, 1)
        return super().request(method, url, *args, **kwargs)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIFTPATCH_AI_MODE", "calculation_only")
    app = create_app(tmp_path / "test.db")
    with SampleClient(app) as value:
        value.store_id = value.post("/api/session/guest").json()["store"]["id"]
        # Direct setup helpers target the same owned sample as API requests.
        app.state.repository = app.state.repository.for_store(value.store_id)
        yield value


def post(client, path, body=None, key=None):
    return client.post(BASE+path, json=body or {}, headers={"Idempotency-Key": key or str(uuid4())})


def snapshot(client):
    return client.get(BASE+"/overview").json()


def add_limit_proposal(client, limits=None):
    def add(state):
        candidate = {"id": "candidate-test", "title": "テスト販売制限", "action": "replace_limits", "review_after_minutes": 30,
                     "limits": limits or {m["id"]: 0 for m in state["menus"]}, "metrics": {}, "feasible": True, "notes": []}
        proposal = {"id": "proposal-test", "mode": "in_service", "type": "service_plan", "status": "ready", "revision": state["revision"],
                    "source": "calculation_only", "created_at": state["business_now"], "review_at": "2026-10-11T02:30:00Z", "candidates": [candidate]}
        state["proposals"].append(proposal)
        return proposal
    return client.app.state.repository.transact(add)


def test_order_idempotency_and_atomic_reservation(client):
    before = snapshot(client)
    key = str(uuid4())
    body = {"items": [{"menu_id": "latte", "quantity": 2}]}
    first = post(client, "/orders", body, key)
    assert first.status_code == 200, first.text
    second = post(client, "/orders", body, key)
    assert first.json()["order"]["id"] == second.json()["order"]["id"]
    after = snapshot(client)
    assert len(after["orders"]) == len(before["orders"])+1
    before_milk = next(i for i in before["inventory"] if i["id"] == "milk")
    after_milk = next(i for i in after["inventory"] if i["id"] == "milk")
    assert after_milk["reserved"] == before_milk["reserved"]+360
    assert after_milk["on_hand"] == before_milk["on_hand"]
    conflict = post(client, "/orders", {"items": [{"menu_id": "latte", "quantity": 1}]}, key)
    assert conflict.status_code == 409


def test_two_concurrent_orders_cannot_reserve_last_unit(client):
    def last_unit(state):
        next(i for i in state["inventory"] if i["id"] == "cake")["on_hand"] = 1
    client.app.state.repository.transact(last_unit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: post(client, "/orders", {"items": [{"menu_id": "cake", "quantity": 1}]}), range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]
    stock = next(i for i in snapshot(client)["inventory"] if i["id"] == "cake")
    assert stock["reserved"] == stock["on_hand"] == 1


def test_limit_is_enforced_and_does_not_expire_automatically(client):
    before = snapshot(client)
    proposal = add_limit_proposal(client)
    body = {"candidate_id": "candidate-test", "expected_revision": proposal["revision"]}
    key = str(uuid4())
    assert post(client, "/proposals/proposal-test/approve", body, key).status_code == 200
    mode_id = snapshot(client)["mode"]["id"]
    assert post(client, "/proposals/proposal-test/approve", body, key).status_code == 200
    assert snapshot(client)["mode"]["id"] == mode_id
    assert post(client, "/orders", {"items": [{"menu_id": "latte", "quantity": 1}]}).status_code == 409
    assert post(client, "/demo/advance", {"minutes": 31}).status_code == 200
    after = snapshot(client)
    assert after["mode"]["status"] == "review_required"
    assert after["proposals"][0]["status"] == "applied"
    assert after["orders"] == before["orders"]
    assert post(client, "/orders", {"items": [{"menu_id": "latte", "quantity": 1}]}).status_code == 409


def test_changed_input_prevents_approval(client):
    proposal = add_limit_proposal(client)
    response = client.patch(BASE+"/staff/yui", json={"absent": True}, headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 200
    response = post(client, "/proposals/proposal-test/approve", {"candidate_id": "candidate-test", "expected_revision": proposal["revision"]})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "stale_proposal"
    assert snapshot(client)["mode"]["limits"] is None


def test_task_completion_consumes_once_and_shared_resources_are_exclusive(client):
    before = snapshot(client)
    order = before["orders"][0]
    task = order["tasks"][0]
    path = f"/orders/{order['id']}/tasks/{task['id']}/status"
    assert post(client, path, {"status": "completed"}).status_code == 409
    assert post(client, path, {"status": "preparing", "staff_id": "aoi"}).status_code == 200
    other = before["orders"][1]
    busy = post(client, f"/orders/{other['id']}/tasks/{other['tasks'][0]['id']}/status", {"status": "preparing", "staff_id": "ren"})
    assert busy.status_code == 409  # same bar station
    assert post(client, path, {"status": "completed"}).status_code == 409
    assert post(client, "/inventory-adjustments", {"ingredient_id": "milk", "on_hand": 3000}).status_code == 409
    post(client, "/demo/advance", {"minutes": 3})
    assert post(client, path, {"status": "completed"}).status_code == 200
    assert post(client, path, {"status": "completed"}).status_code == 200
    after = snapshot(client)
    a = next(i for i in after["inventory"] if i["id"] == "milk")
    b = next(i for i in before["inventory"] if i["id"] == "milk")
    assert a["on_hand"] == b["on_hand"]-180
    assert a["reserved"] == b["reserved"]-180


def test_inventory_shortfall_preserves_orders_and_blocks_start(client):
    before = snapshot(client)
    assert post(client, "/inventory-adjustments", {"ingredient_id": "bread", "on_hand": 0}).status_code == 200
    order = before["orders"][0]
    sandwich = order["tasks"][1]
    response = post(client, f"/orders/{order['id']}/tasks/{sandwich['id']}/status", {"status": "preparing"})
    assert response.status_code == 409
    assert len(snapshot(client)["orders"]) == len(before["orders"])


def test_future_delivery_does_not_create_usable_inventory(client):
    post(client, "/inventory-adjustments", {"ingredient_id": "milk", "on_hand": 360})
    assert post(client, "/orders", {"items": [{"menu_id": "latte", "quantity": 1}]}).status_code == 409
    before = snapshot(client)
    assert post(client, "/deliveries/delivery-milk/receive", {"quantity": 1000}).status_code == 200
    assert post(client, "/orders", {"items": [{"menu_id": "latte", "quantity": 1}]}).status_code == 200
    assert post(client, "/deliveries/delivery-milk/receive", {"quantity": 1000}).status_code == 409
    assert next(i for i in snapshot(client)["inventory"] if i["id"] == "milk")["on_hand"] == 1360


def test_invalid_input_and_missing_store(client):
    assert post(client, "/orders", {"items": [{"menu_id": "latte", "quantity": -1}]}).status_code == 422
    assert post(client, "/orders", {"items": [{"menu_id": "latte", "quantity": True}]}).status_code == 422
    assert client.get("/api/stores/other/overview").status_code == 404
    assert client.post(BASE+"/orders", json={"items": [{"menu_id": "latte", "quantity": 1}]}).status_code == 422


def test_close_is_snapshot_and_reservations_are_preserved(client):
    before = snapshot(client)
    key = str(uuid4())
    first = post(client, "/closes", key=key)
    assert first.status_code == 200, first.text
    assert first.json()["close"]["restricted"] is True  # shortened business interval
    assert first.json()["close"]["pending_orders"] == 3
    assert post(client, "/closes", key=key).json()["close"]["id"] == first.json()["close"]["id"]
    assert snapshot(client)["inventory"] == before["inventory"]
    assert post(client, "/orders", {"items": [{"menu_id": "latte", "quantity": 1}]}).status_code == 409


def test_local_calculation_proposals_are_not_presented_as_ai(client):
    result = post(client, "/proposals", {"mode": "in_service", "objective": "preserve_drinks"})
    assert result.status_code == 200, result.text
    proposal = result.json()
    assert proposal["source"] == "calculation_only"
    assert proposal["recommended_candidate_id"] is None
    assert len(proposal["candidates"]) >= 2
    assert all(step["origin"] == "server" for step in proposal["trace"])
    assert proposal["revision"] == snapshot(client)["revision"]  # generation does not invalidate itself


def test_reset_keeps_revision_monotonic_and_survives_restart(client, tmp_path):
    before = snapshot(client)
    post(client, "/demo/reset")
    after = snapshot(client)
    assert after["revision"] > before["revision"]
    from app.repository import SQLiteRepository
    assert SQLiteRepository(client.app.state.repository.path).for_store(client.store_id).read()["revision"] == after["revision"]


def test_inflight_proposal_key_cannot_be_reused_by_a_write_or_reset(client):
    from app.main import fingerprint, ProposalBody
    from app.timeutils import iso
    key = str(uuid4())
    body = {"mode": "in_service", "objective": "balanced"}

    def register(state):
        state["agent_runs"]["pending-test"] = {
            "id": "pending-test", "key": key, "digest": fingerprint("proposals", ProposalBody.model_validate(body).model_dump()),
            "status": "running", "expires_at": iso(datetime.now(timezone.utc) + timedelta(seconds=45)),
        }

    client.app.state.repository.transact(register)
    before = snapshot(client)
    order = post(client, "/orders", {"items": [{"menu_id": "cake", "quantity": 1}]}, key)
    assert order.status_code == 409
    assert order.json()["detail"]["code"] == "idempotency_conflict"
    assert post(client, "/demo/reset", key=key).status_code == 409
    assert post(client, "/proposals", body, key).json()["detail"]["code"] == "run_in_progress"
    assert snapshot(client) == before


def test_expired_prep_cannot_be_acknowledged_even_with_matching_revision(client):
    def expired(state):
        state["proposals"].append({"id": "expired-prep", "type": "prep_plan", "status": "ready",
                                   "revision": state["revision"], "review_at": state["business_now"]})
    client.app.state.repository.transact(expired)
    response = post(client, "/proposals/expired-prep/acknowledge")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "expired_proposal"


def test_failed_capacity_check_rolls_back_order_and_all_reservations(client):
    from app.timeutils import iso, parse_time
    def expires_before_ready(state):
        cake = next(item for item in state["inventory"] if item["id"] == "cake")
        cake["expires_at"] = iso(parse_time(state["business_now"]) + timedelta(seconds=10))
    client.app.state.repository.transact(expires_before_ready)
    before = snapshot(client)
    response = post(client, "/orders", {"items": [{"menu_id": "cake", "quantity": 1}]})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "capacity_exceeded"
    assert snapshot(client) == before


def test_close_compares_saved_acknowledged_plan_not_recomputed_history(client):
    proposal = post(client, "/proposals", {"mode": "pre_open", "objective": "balanced"}).json()
    assert post(client, f"/proposals/{proposal['id']}/acknowledge").status_code == 200
    before_forecast = deepcopy(proposal["prep"]["forecast"])
    history = snapshot(client)["history"][0]
    revised = {menu["id"]: 1000 for menu in snapshot(client)["menus"]}
    response = client.patch(BASE + f"/history/{history['date']}", json={"counts": revised, "restricted": False},
                            headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 200
    close = post(client, "/closes").json()["close"]
    assert close["forecast"] == before_forecast
    assert close["forecast_proposal_id"] == proposal["id"]
    assert close["forecast_source"] == "acknowledged_prep_plan"


def test_restocking_does_not_erase_a_stockout_from_demand_history(client):
    def remove_queue(state):
        state["orders"] = []
        for entry in state["inventory"]:
            entry["reserved"] = 0
    client.app.state.repository.transact(remove_queue)
    assert post(client, "/inventory-adjustments", {"ingredient_id": "cake", "on_hand": 0}).status_code == 200
    assert post(client, "/inventory-adjustments", {"ingredient_id": "cake", "on_hand": 10}).status_code == 200
    assert post(client, "/demo/advance", {"minutes": 120}).status_code == 200
    result = post(client, "/closes").json()
    assert result["close"]["restricted"] is True
    assert result["close"]["pending_orders"] == 0
    assert any("通常販売できない" in note for note in result["close"]["notes"])
    assert result["overview"]["history"][0]["restricted"] is True


def test_existing_commitments_can_be_fulfilled_after_close_without_changing_close_snapshot(client):
    before = snapshot(client)
    order = before["orders"][0]
    task = order["tasks"][0]
    path = f"/orders/{order['id']}/tasks/{task['id']}/status"
    assert post(client, "/demo/advance", {"minutes": 120}).status_code == 200
    close = post(client, "/closes").json()["close"]
    assert close["restricted"] is True  # Pending units cannot lower tomorrow's demand.
    assert post(client, path, {"status": "preparing", "staff_id": "sora"}).status_code == 200
    assert post(client, "/demo/advance", {"minutes": 3}).status_code == 200
    assert post(client, path, {"status": "completed"}).status_code == 200
    after = snapshot(client)
    assert after["closes"][0] == close
    assert post(client, "/orders", {"items": [{"menu_id": "latte", "quantity": 1}]}).status_code == 409


def test_cached_proposal_is_refreshed_if_its_review_time_has_passed(client):
    key = str(uuid4())
    body = {"mode": "pre_open", "objective": "balanced"}
    first = post(client, "/proposals", body, key).json()
    def expire_without_revising_input(state):
        proposal = next(item for item in state["proposals"] if item["id"] == first["id"])
        proposal["review_at"] = state["business_now"]
    client.app.state.repository.transact(expire_without_revising_input)
    cached = post(client, "/proposals", body, key).json()
    assert cached["id"] == first["id"]
    assert cached["status"] == "stale"


def test_run_expired_before_final_save_cannot_become_ready(client, monkeypatch):
    from app.timeutils import iso
    async def expire_and_finish(snapshot, mode, objective):
        def expire(state):
            for run in state["agent_runs"].values():
                run["expires_at"] = iso(datetime.now(timezone.utc) - timedelta(seconds=1))
        client.app.state.repository.transact(expire)
        return {"status": "ready", "source": "calculation_only", "summary": "test", "candidates": [], "trace": []}
    monkeypatch.setattr("app.agent.runner.generate_proposal", expire_and_finish)
    response = post(client, "/proposals", {"mode": "pre_open", "objective": "balanced"})
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "failed"
    assert result["error"] == "budget_exceeded"
    run = client.get(BASE + f"/agent-runs/{result['run_id']}").json()
    assert run["status"] == "timed_out"


@pytest.mark.parametrize('window', [
    {'start_time': 1100, 'counts': {}},
    {'start_time': '11:00:00', 'counts': {}},
    {'start_time': '25:00', 'counts': {}},
    {'start_time': '11:00', 'counts': {'latte': True}},
    {'start_time': '11:00', 'counts': {}, 'unexpected': 1},
])
def test_malformed_history_windows_return_validation_errors(client, window):
    before = snapshot(client)
    day = before['history'][0]
    response = client.patch(BASE + '/history/' + day['date'],
                            json={'counts': day['counts'], 'restricted': False, 'windows': [window]},
                            headers={'Idempotency-Key': uuid4().hex})
    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'invalid_input'
    assert snapshot(client) == before


@pytest.mark.parametrize('start', ['10:30', '11:15', '13:00'])
def test_history_windows_must_match_service_intervals(client, start):
    before = snapshot(client)
    day = before['history'][0]
    response = client.patch(BASE + '/history/' + day['date'],
                            json={'counts': day['counts'], 'restricted': False,
                                  'windows': [{'start_time': start, 'counts': day['counts']}]},
                            headers={'Idempotency-Key': uuid4().hex})
    assert response.status_code == 422
    assert response.json()['detail']['code'] == 'invalid_windows'
    assert snapshot(client) == before


def test_history_windows_are_saved_and_used_in_time_profiles(client):
    state = snapshot(client)
    day = state['history'][0]
    windows = [{'start_time': '11:00', 'counts': day['counts']}]
    response = client.patch(BASE + '/history/' + day['date'],
                            json={'counts': day['counts'], 'restricted': False, 'windows': windows},
                            headers={'Idempotency-Key': uuid4().hex})
    assert response.status_code == 200
    assert response.json()['history'][0]['windows'] == windows


def test_quota_rejection_does_not_consume_incident_or_request_key(client, monkeypatch):
    response = client.patch(BASE + '/staff/yui', json={'absent': True},
                            headers={'Idempotency-Key': uuid4().hex})
    event = response.json()['replan_event']['id']
    body = {'mode': 'in_service', 'trigger_event_id': event}
    key = uuid4().hex
    monkeypatch.setattr(client.app.state.identities, 'consume_quota', lambda *args, **kwargs: False)
    assert post(client, '/proposals', body, key).status_code == 429
    assert snapshot(client)['replan_required'] is True
    assert snapshot(client)['active_run'] is None
    monkeypatch.setattr(client.app.state.identities, 'consume_quota', lambda *args, **kwargs: True)
    result = post(client, '/proposals', body, key)
    assert result.status_code == 200, result.text
    assert result.json()['status'] == 'ready'
    assert snapshot(client)['replan_required'] is False


def test_generation_timeout_preserves_completed_tool_trace(client, monkeypatch):
    trace = [{'step': 1, 'origin': 'server', 'tool': 'read_queue', 'args': {},
              'result_summary': 'Completed queue inspection'}]
    async def timeout(snapshot, mode, objective, *, progress=None):
        progress('investigate', trace)
        raise TimeoutError()
    monkeypatch.setattr('app.agent.runner.generate_proposal', timeout)
    result = post(client, '/proposals', {'mode': 'pre_open'}).json()
    assert result['status'] == 'failed'
    assert result['error'] == 'budget_exceeded'
    assert result['trace'] == trace
    run = client.get(BASE + '/agent-runs/' + result['run_id']).json()
    assert run['status'] == 'timed_out'
    assert run['trace'] == trace


def test_continuation_preserves_context_and_applies_preference_answers(client, monkeypatch):
    contexts = []
    async def ask_preferences(state, mode, objective):
        contexts.append(deepcopy(state['request_context']))
        result = {'source': 'calculation_only', 'summary': 'Choose a preference',
                  'candidates': [], 'trace': [], 'objective': objective}
        if not state['request_context'].get('parent_run_id'):
            return {**result, 'status': 'waiting_input', 'questions': [
                {'id': 'priority', 'text': 'Priority?', 'field': 'objective', 'options': ['balanced', 'reduce_delay']},
                {'id': 'review', 'text': 'Review?', 'field': 'review_after_minutes', 'options': ['15', '30']},
            ]}
        return {**result, 'status': 'ready'}
    monkeypatch.setattr('app.agent.runner.generate_proposal', ask_preferences)
    first = post(client, '/proposals', {'mode': 'in_service', 'locale': 'ko-KR',
                 'constraints': {'allowed_review_minutes': [30], 'allow_menu_pause': False}}).json()
    child = post(client, '/proposals', {'mode': 'in_service', 'parent_run_id': first['run_id'],
                 'answers': {'priority': 'reduce_delay', 'review': '15'}}).json()
    assert child['status'] == 'ready'
    assert child['parent_run_id'] == first['run_id']
    assert child['service_date'] == first['service_date'] == snapshot(client)['store']['service_date']
    assert child['objective'] == 'reduce_delay'
    assert child['locale'] == contexts[-1]['locale'] == 'ko-KR'
    assert child['constraints'] == {'allowed_review_minutes': [15], 'allow_menu_pause': False}
    assert contexts[-1]['parent_summary'] == first['summary']
    assert contexts[-1]['answers'] == {'priority': 'reduce_delay', 'review': '15'}
    assert child['snapshot_id'] != first['snapshot_id']


def test_fact_continuation_requires_the_questioned_data_to_be_saved(client, monkeypatch):
    async def ask_stock(state, mode, objective):
        return {'status': 'ready' if state['request_context'].get('parent_run_id') else 'waiting_input',
                'source': 'calculation_only', 'summary': 'Confirm stock', 'candidates': [], 'trace': [],
                'questions': [{'id': 'stock', 'text': 'Stock?', 'field': 'inventory', 'options': []}]}
    monkeypatch.setattr('app.agent.runner.generate_proposal', ask_stock)
    first = post(client, '/proposals', {'mode': 'pre_open'}).json()
    body = {'mode': 'pre_open', 'parent_run_id': first['run_id'], 'answers': {'stock': 'updated'}}
    assert post(client, '/proposals', body).json()['detail']['code'] == 'facts_not_updated'
    assert client.patch(BASE + '/staff/yui', json={'absent': True},
                        headers={'Idempotency-Key': uuid4().hex}).status_code == 200
    # An unrelated staff edit also migrates legacy lot rows; that is not a stock correction.
    assert post(client, '/proposals', body).json()['detail']['code'] == 'facts_not_updated'
    assert post(client, '/inventory-adjustments', {'ingredient_id': 'milk', 'on_hand': 4500}).status_code == 200
    child = post(client, '/proposals', body)
    assert child.status_code == 200, child.text
    assert child.json()['status'] == 'ready'


def test_continuation_rejects_a_different_service_day_or_dismissed_parent(client, monkeypatch):
    async def ask(state, mode, objective):
        return {'status': 'waiting_input', 'source': 'calculation_only', 'summary': 'Priority',
                'candidates': [], 'trace': [],
                'questions': [{'id': 'priority', 'text': 'Priority?', 'field': 'objective', 'options': ['balanced']}]}
    monkeypatch.setattr('app.agent.runner.generate_proposal', ask)
    first = post(client, '/proposals', {'mode': 'pre_open'}).json()
    body = {'mode': 'pre_open', 'parent_run_id': first['run_id'], 'answers': {'priority': 'balanced'}}
    def previous_day(state):
        next(p for p in state['proposals'] if p['id'] == first['id'])['service_date'] = '2026-10-10'
    client.app.state.repository.transact(previous_day)
    assert post(client, '/proposals', body).json()['detail']['code'] == 'invalid_parent'
    assert post(client, '/proposals/' + first['id'] + '/dismiss').status_code == 200
    assert post(client, '/proposals', body).json()['detail']['code'] == 'invalid_parent'


def test_completed_continuation_replays_after_parent_dismissal_and_service_date_change(client, monkeypatch):
    async def ask(state, mode, objective):
        return {'status': 'ready' if state['request_context'].get('parent_run_id') else 'waiting_input',
                'source': 'calculation_only', 'summary': 'Priority', 'candidates': [], 'trace': [],
                'questions': [{'id': 'priority', 'text': 'Priority?', 'field': 'objective', 'options': ['balanced']}]}
    monkeypatch.setattr('app.agent.runner.generate_proposal', ask)
    parent = post(client, '/proposals', {'mode': 'pre_open'}).json()
    key = uuid4().hex
    body = {'mode': 'pre_open', 'parent_run_id': parent['run_id'], 'service_date': parent['service_date'],
            'answers': {'priority': 'balanced'}}
    child = post(client, '/proposals', body, key).json()
    assert post(client, '/proposals/' + parent['id'] + '/dismiss').status_code == 200
    assert post(client, '/proposals', body, key).json()['id'] == child['id']
    def new_day(state):
        state['store']['service_date'] = '2026-10-12'
        state['revision'] += 1
    client.app.state.repository.transact(new_day)
    response = post(client, '/proposals', body, key)
    assert response.status_code == 200, response.text
    assert response.json()['id'] == child['id']
    assert response.json()['status'] == 'stale'
    conflict = post(client, '/proposals', {**body, 'answers': {}}, key)
    assert conflict.status_code == 409
    assert conflict.json()['detail']['code'] == 'idempotency_conflict'


@pytest.mark.parametrize('status', ['failed', 'stale', 'expired_ready'])
def test_parent_retry_preserves_preferences_and_rejects_answers(client, monkeypatch, status):
    async def generated(state, mode, objective):
        return {'status': 'ready', 'source': 'calculation_only', 'summary': 'Retry context',
                'candidates': [], 'trace': [], 'objective': objective}
    monkeypatch.setattr('app.agent.runner.generate_proposal', generated)
    first = post(client, '/proposals', {'mode': 'pre_open', 'objective': 'reduce_delay', 'locale': 'ko-KR',
                 'constraints': {'allowed_review_minutes': [15], 'allow_menu_pause': False}}).json()
    def failed(state):
        parent = next(p for p in state['proposals'] if p['id'] == first['id'])
        if status == 'expired_ready':
            parent['review_at'] = state['business_now']
        else:
            parent['status'] = status
    client.app.state.repository.transact(failed)
    body = {'mode': 'pre_open', 'parent_run_id': first['run_id']}
    invalid = post(client, '/proposals', {**body, 'answers': {'priority': 'balanced'}})
    assert invalid.status_code == 422
    assert invalid.json()['detail']['code'] == 'invalid_answer'
    child = post(client, '/proposals', body)
    assert child.status_code == 200, child.text
    result = child.json()
    assert result['parent_run_id'] == first['run_id']
    assert result['status'] == 'ready'
    assert result['objective'] == 'reduce_delay'
    assert result['locale'] == 'ko-KR'
    assert result['constraints'] == first['constraints']


@pytest.mark.parametrize('status', ['ready', 'applied', 'acknowledged', 'dismissed'])
def test_current_or_used_parent_cannot_be_retried(client, monkeypatch, status):
    async def generated(state, mode, objective):
        return {'status': 'ready', 'source': 'calculation_only', 'summary': 'Current plan',
                'candidates': [], 'trace': []}
    monkeypatch.setattr('app.agent.runner.generate_proposal', generated)
    first = post(client, '/proposals', {'mode': 'pre_open'}).json()
    def used(state):
        next(p for p in state['proposals'] if p['id'] == first['id'])['status'] = status
    client.app.state.repository.transact(used)
    result = post(client, '/proposals', {'mode': 'pre_open', 'parent_run_id': first['run_id']})
    assert result.status_code == 422
    assert result.json()['detail']['code'] == 'invalid_parent'
