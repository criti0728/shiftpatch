"""End-to-end contracts for remaining agent workflow, not live model quality."""
import asyncio
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import threading
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.seed import seed_state
from app import operations as ops
from app.inventory import migrate, receipt, reserve, available, take_pool
from app.incidents import detect
from app.planning.forecast import forecast_windows
from app.planning.prep import calculate_prep
from app.simulation.engine import simulate
from app.agent.runner import generate_proposal
from app.agent.model_client import ModelReply, ToolCall
from app.agent.schemas import declarations


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('SHIFTPATCH_AI_MODE', 'calculation_only')
    monkeypatch.setenv('SHIFTPATCH_AI_COOLDOWN_SECONDS', '0')
    return create_app(tmp_path/'workflow.db')


def guest(client):
    return client.post('/api/session/guest').json()['store']['id']


def post(client, store, path, body=None, key=None):
    return client.post('/api/stores/'+store+path, json=body or {}, headers={'Idempotency-Key': key or uuid4().hex})


def test_incident_edges_and_same_event_deduplication(app):
    with TestClient(app) as client:
        store = guest(client)
        response = client.patch('/api/stores/'+store+'/staff/yui', json={'absent': True}, headers={'Idempotency-Key': uuid4().hex})
        state = response.json()
        assert state['replan_required']
        event = state['replan_event']['id']
        a = post(client, store, '/proposals', {'mode': 'in_service', 'trigger_event_id': event}).json()
        b = post(client, store, '/proposals', {'mode': 'in_service', 'trigger_event_id': event}).json()
        assert a['id'] == b['id']
        assert not client.get('/api/stores/'+store+'/overview').json()['replan_required']
        run = client.get('/api/stores/'+store+'/agent-runs/'+a['run_id']).json()
        assert run['status'] == 'succeeded' and run['snapshot_id'] == a['snapshot_id']
        assert 'key' not in run
        # Ordinary continued high queue is one edge, not one event per order.
        state = seed_state()
        for i in range(3):
            state['orders'].append({**deepcopy(state['orders'][0]), 'id': f'edge-{i}'})
        detect(state, 'orders')
        old = state['replan_event']['id']
        detect(state, 'orders')
        assert state['replan_event']['id'] == old
        state['orders'] = []
        detect(state, 'task')
        for i in range(6):
            state['orders'].append({'id': str(i), 'status': 'accepted', 'created_at': state['business_now']})
        detect(state, 'orders')
        assert state['replan_event']['id'] != old


def test_lot_fifo_expiry_and_protected_existing_reservations():
    state = seed_state()
    stock = migrate(state)
    milk = stock['milk']
    reserved_lot = milk['lots'][0]['id']
    # Old reserved material expires but fresh stock must not replace that promise.
    milk['lots'][0]['expires_at'] = '2026-10-11T02:01:00Z'
    milk['expires_at'] = milk['lots'][0]['expires_at']
    receipt(milk, 1000, '2026-10-15T00:00:00Z', '2026-10-11T02:00:30Z')
    at = datetime.fromisoformat('2026-10-11T02:02:00+00:00')
    assert available(milk, at) == 1000
    assigned = reserve(milk, 180, at)
    assert assigned[0]['lot_id'] != reserved_lot
    assert simulate(state, {'action': 'keep_current'}, include_arrivals=False)['metrics']['existing_at_risk'] >= 1
    # FIFO skips expired stock, prefers earlier receipt among valid lots.
    row = {'id': 'x', 'name': 'x', 'unit': 'g', 'on_hand': 3, 'reserved': 0, 'expires_at': '2030-01-01T00:00:00Z'}
    receipt(row, 7, '2030-02-01T00:00:00Z', '2026-10-11T03:00:00Z')
    allocations = reserve(row, 4, at)
    assert [r['quantity'] for r in allocations] == [3, 1]
    before = deepcopy(state)
    calculate_prep(state)
    assert before == state


def test_fresh_delivery_does_not_inherit_expired_old_lot(app):
    with TestClient(app) as client:
        store = guest(client)
        post(client, store, '/inventory-lots', {'ingredient_id': 'milk', 'quantity': 200, 'expires_at': '2026-10-20T00:00:00Z'})
        state = client.get('/api/stores/'+store+'/overview').json()
        milk = next(i for i in state['inventory'] if i['id'] == 'milk')
        assert len(milk['lots']) == 2 and milk['on_hand'] == 4200
        p = post(client, store, '/proposals', {'mode': 'pre_open'}).json()
        assert post(client, store, '/proposals/'+p['id']+'/dismiss').json()['proposals'][0]['status'] == 'dismissed'


def test_observed_time_profile_excludes_restricted_days():
    state = seed_state()
    for index, row in enumerate(state['history']):
        row['windows'] = [{'start_time': '11:00', 'counts': row['counts']}, {'start_time': '11:30', 'counts': {}}]
        if index == 0:
            row['restricted'] = True
            row['windows'][0]['counts'] = {key: 999 for key in row['counts']}
    windows, notes = forecast_windows(state)
    assert all(w['counts']['latte'] == 0 for w in windows[1:])
    assert windows[0]['counts']['latte'] < 999
    assert windows[0]['basis']['latte'] == 'observed_intervals'
    assert any('除外' in note for note in notes)


def test_waiting_input_requires_saved_facts_then_links_new_run(app):
    with TestClient(app) as client:
        store = guest(client)
        repo = app.state.repository.for_store(store)
        repo.transact(lambda state: state.update(history=[]))
        parent = post(client, store, '/proposals', {'mode': 'pre_open'}).json()
        question = parent['questions'][0]
        body = {'mode': 'pre_open', 'parent_run_id': parent['run_id'], 'answers': {question['id']: 'updated'}}
        assert post(client, store, '/proposals', body).status_code == 422
        def restore(state):
            state['history'] = seed_state()['history']
            state['revision'] += 1
        repo.transact(restore)
        child = post(client, store, '/proposals', body).json()
        assert child['parent_run_id'] == parent['run_id'] and child['status'] == 'ready'
        assert child['snapshot_id'] != parent['snapshot_id']
        assert post(client, store, '/proposals', {**body, 'answers': {'unknown': 'updated'}}).status_code == 422


def test_progress_visible_during_execution_and_cancel_terminates(app, monkeypatch):
    entered = threading.Event()
    async def slow(snapshot, mode, objective, *, progress=None):
        try:
            progress('investigate', [{'step': 1, 'origin': 'model', 'tool': 'read_shift', 'args': {}, 'result_summary': 'test'}])
            entered.set()
            for _ in range(100):
                await asyncio.sleep(0.01)
                progress('investigate', [])
            return {'status': 'ready', 'source': 'calculation_only', 'candidates': [], 'trace': []}
        except Exception:
            return {'status': 'failed', 'error': 'interrupted', 'source': 'calculation_only', 'candidates': [], 'trace': []}
    monkeypatch.setattr('app.agent.runner.generate_proposal', slow)
    with TestClient(app) as client, ThreadPoolExecutor() as pool:
        store = guest(client)
        future = pool.submit(post, client, store, '/proposals', {'mode': 'in_service'})
        assert entered.wait(2)
        running = client.get('/api/stores/'+store+'/overview').json()['active_run']
        assert running['phase'] == 'investigate' and running['status'] == 'running'
        assert post(client, store, '/agent-runs/'+running['id']+'/cancel').status_code == 200
        result = future.result(timeout=3).json()
        assert result['status'] == 'failed' and result['recommended_candidate_id'] is None
        assert client.get('/api/stores/'+store+'/agent-runs/'+running['id']).json()['status'] == 'interrupted'


def test_actual_runner_progress_and_four_tool_limit(monkeypatch):
    monkeypatch.setenv('SHIFTPATCH_AI_MODE', 'gemini')
    phases = []
    class TooMany:
        async def generate(self, *args):
            return ModelReply(calls=[ToolCall('read_queue', {}) for _ in range(5)])
    result = asyncio.run(generate_proposal(seed_state(), 'in_service', 'balanced', transport=TooMany(), progress=lambda p, t: phases.append(p)))
    assert result['status'] == 'failed' and result['tool_calls'] == 1
    assert 'calculate' in phases and 'investigate' in phases


def test_realtime_clock_advances_only_with_real_elapsed_time():
    from app.clock import tick
    state = seed_state()
    anchor = datetime(2026, 10, 11, tzinfo=timezone.utc)
    state['clock'] = {'mode': 'realtime', 'wall_anchor': anchor.isoformat(), 'business_anchor': state['business_now']}
    assert tick(state, anchor+timedelta(minutes=2))
    assert state['business_now'] == '2026-10-11T02:02:00Z'
    assert not tick(state, anchor+timedelta(minutes=1))


def test_developer_api_endpoint_configuration_uses_server_key_only(monkeypatch):
    from google import genai
    from app.agent.model_client import GeminiTransport
    captured = {}
    monkeypatch.setattr(genai, 'Client', lambda **values: captured.update(values) or object())
    monkeypatch.setenv('SHIFTPATCH_GEMINI_PROVIDER', 'developer')
    monkeypatch.setenv('SHIFTPATCH_GEMINI_MODEL', 'configured-flash')
    monkeypatch.setenv('GEMINI_API_KEY', 'test-secret')
    monkeypatch.setenv('SHIFTPATCH_GEMINI_ENDPOINT', 'https://gateway.example.com')
    GeminiTransport()
    assert captured['api_key'] == 'test-secret'
    assert captured['http_options'].base_url == 'https://gateway.example.com'
    assert captured['http_options'].retry_options.attempts == 1
    assert 'project' not in captured
    monkeypatch.setenv('SHIFTPATCH_GEMINI_ENDPOINT', 'https://gateway.example.com?key=test-secret')
    with pytest.raises(ValueError) as error:
        GeminiTransport()
    assert 'test-secret' not in str(error.value)


def test_constraints_and_quantitative_prose_cannot_bypass_validation():
    from app.agent.dispatcher import Dispatcher
    from app.agent.result_validator import validate_final
    state = seed_state()
    dispatcher = Dispatcher(state, 'in_service', [], constraints={'allowed_review_minutes': [15], 'allow_menu_pause': False})
    read = dispatcher.execute('read_inventory', {})
    spec = {'title': 'pause', 'limits': {m['id']: 0 for m in state['menus']}, 'review_after_minutes': 15, 'based_on_result_ids': [read['result_id']]}
    invalid = dispatcher.execute('simulate_service_plan', {'candidate_spec': spec})
    assert invalid['status'] == 'invalid_candidate' and not dispatcher.candidates
    spec['limits'] = {m['id']: 2 for m in state['menus']}
    spec['review_after_minutes'] = 30
    assert dispatcher.execute('simulate_service_plan', {'candidate_spec': spec})['status'] == 'invalid_candidate'
    with pytest.raises(ValueError, match='数量'):
        validate_final({'outcome': 'needs_input', 'summary': '売上は99999円です。', 'questions': [{'id': 'input', 'text': '入力してください', 'field': 'inventory', 'options': []}]}, dispatcher)


def test_reserved_lot_reallocation_is_explicit_and_atomic(app):
    with TestClient(app) as client:
        store = guest(client)
        repo = app.state.repository.for_store(store)
        def expire(state):
            stock = migrate(state)
            milk = stock['milk']
            milk['lots'][0]['expires_at'] = state['business_now']
            milk['expires_at'] = state['business_now']
        repo.transact(expire)
        before = repo.read()
        task = before['orders'][0]['tasks'][0]
        old = task['lot_allocations']['milk'][0]['lot_id']
        path = '/orders/'+before['orders'][0]['id']+'/tasks/'+task['id']+'/reallocate'
        assert post(client, store, path).status_code == 409
        assert repo.read() == before  # failed release+reserve is rolled back
        post(client, store, '/inventory-lots', {'ingredient_id': 'milk', 'quantity': 500, 'expires_at': '2026-10-15T00:00:00Z'})
        assert repo.read()['orders'][0]['tasks'][0]['lot_allocations']['milk'][0]['lot_id'] == old
        assert post(client, store, path).status_code == 200
        updated = repo.read()['orders'][0]['tasks'][0]
        assert updated['lot_allocations']['milk'][0]['lot_id'] != old


def test_same_revision_time_boundary_stales_proposal_and_rejects_approval(app):
    with TestClient(app) as client:
        store = guest(client)
        repo = app.state.repository.for_store(store)
        def near_end(state):
            state['staff'][2]['shift_end'] = '2026-10-11T02:01:00Z'
            state['clock'] = {'mode': 'realtime', 'wall_anchor': datetime.now(timezone.utc).isoformat(), 'business_anchor': state['business_now']}
        repo.transact(near_end)
        proposal = post(client, store, '/proposals', {'mode': 'in_service'}).json()
        repo.transact(lambda state: state.update(business_now='2026-10-11T02:02:00Z'))
        overview = client.get('/api/stores/'+store+'/overview').json()
        assert overview['revision'] == proposal['revision']
        assert overview['proposals'][0]['status'] == 'stale'
        response = post(client, store, '/proposals/'+proposal['id']+'/approve', {'candidate_id': proposal['candidates'][0]['id'], 'expected_revision': proposal['revision']})
        assert response.status_code == 409


def test_event_dedup_never_bypasses_key_body_conflicts(app):
    with TestClient(app) as client:
        store = guest(client)
        client.patch('/api/stores/'+store+'/staff/yui', json={'absent': True}, headers={'Idempotency-Key': uuid4().hex})
        event = client.get('/api/stores/'+store+'/overview').json()['replan_event']['id']
        key = uuid4().hex
        body = {'mode': 'in_service', 'trigger_event_id': event}
        first = post(client, store, '/proposals', body, key).json()
        conflict = post(client, store, '/proposals', {**body, 'objective': 'reduce_delay'}, key)
        assert conflict.status_code == 409
        # Semantic event identity survives bounded operational event-log retention.
        app.state.repository.for_store(store).transact(lambda s: s.update(events=[]))
        assert post(client, store, '/proposals', body).json()['id'] == first['id']


def test_manual_analysis_covers_pending_event_without_second_auto_run(app):
    with TestClient(app) as client:
        store = guest(client)
        client.patch('/api/stores/'+store+'/staff/yui', json={'absent': True}, headers={'Idempotency-Key': uuid4().hex})
        event = client.get('/api/stores/'+store+'/overview').json()['replan_event']['id']
        manual = post(client, store, '/proposals', {'mode': 'in_service'}).json()
        assert not client.get('/api/stores/'+store+'/overview').json()['replan_required']
        assert post(client, store, '/proposals', {'mode': 'in_service', 'trigger_event_id': event}).json()['id'] == manual['id']


def test_new_order_reserves_a_lot_valid_at_scheduled_finish(app):
    with TestClient(app) as client:
        store = guest(client)
        repo = app.state.repository.for_store(store)
        def stock(state):
            state['orders'] = []
            for entry in state['inventory']:
                entry['reserved'] = 0
            milk = migrate(state)['milk']
            milk['lots'][0]['expires_at'] = '2026-10-11T02:01:00Z'
            milk['expires_at'] = milk['lots'][0]['expires_at']
            receipt(milk, 500, '2026-10-12T00:00:00Z', state['business_now'])
        repo.transact(stock)
        result = post(client, store, '/orders', {'items': [{'menu_id': 'latte', 'quantity': 1}]})
        assert result.status_code == 200
        task = result.json()['order']['tasks'][0]
        assert task['lot_allocations']['milk'][0]['lot_id'] != 'legacy-milk'
        assert 'reservation_pending' not in task


def test_sales_tool_can_select_a_valid_time_window_without_changing_day_basis():
    from app.agent.dispatcher import Dispatcher
    dispatcher = Dispatcher(seed_state(), 'pre_open', [])
    selected = dispatcher.execute('estimate_sales', {'start_time': '11:00', 'end_time': '11:30'})
    assert selected['status'] == 'ok' and len(selected['data']['windows']) == 1
    prep = dispatcher.execute('calculate_prep', {'sales_basis_result_id': selected['result_id']})
    assert prep['data']['prep']['forecast'] == selected['data']['forecast']
    assert dispatcher.execute('estimate_sales', {'start_time': '10:00'})['status'] == 'error'


def test_realtime_tick_keeps_restriction_history_across_expiry():
    from app.clock import tick
    state = seed_state()
    milk = next(i for i in state['inventory'] if i['id'] == 'milk')
    milk['expires_at'] = '2026-10-11T02:01:00Z'
    anchor = datetime(2026, 10, 11, tzinfo=timezone.utc)
    state['clock'] = {'mode': 'realtime', 'wall_anchor': anchor.isoformat(), 'business_anchor': state['business_now']}
    tick(state, anchor+timedelta(hours=3))
    assert state['restricted_today']
    assert state['restriction_reasons']


def test_delayed_approval_does_not_extend_the_original_candidate_window(monkeypatch):
    monkeypatch.setenv('SHIFTPATCH_AI_MODE', 'calculation_only')
    state = seed_state()
    proposal = asyncio.run(generate_proposal(state, 'in_service', 'balanced'))
    state['proposals'].append(proposal)
    state['business_now'] = '2026-10-11T02:05:00Z'
    candidate = proposal['candidates'][1]
    ops.approve(state, proposal['id'], candidate['id'], state['revision'])
    assert state['mode']['starts_at'] == '2026-10-11T02:05:00Z'
    assert state['mode']['review_at'] == '2026-10-11T02:30:00Z'
