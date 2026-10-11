"""Detect meaningful incident edges; the open manager screen requests analysis."""
import os
from uuid import uuid4
from .timeutils import parse_time
from .inventory import available


def detect(state, command):
    if state.get('closed'):
        state['replan_required'] = False
        state['replan_event'] = None
        return
    previous = state.setdefault('incident_edges', {})
    from .planning.forecast import service_window
    start, end = service_window(state)
    at = parse_time(state['business_now'])
    if not start <= at < end:
        return
    queue = [o for o in state['orders'] if o['status'] != 'completed']
    threshold = max(1, int(os.getenv('SHIFTPATCH_QUEUE_THRESHOLD', '5')))
    wait_threshold = max(1, int(os.getenv('SHIFTPATCH_WAIT_THRESHOLD_MINUTES', '10')))
    oldest = max(((at - parse_time(o['created_at'])).total_seconds() / 60 for o in queue), default=0)
    edges = {
        'queue': len(queue) >= threshold or oldest >= wait_threshold,
        'review': bool(state['mode'].get('review_at') and at >= parse_time(state['mode']['review_at'])),
        'low_stock': any(available(i, at) <= i.get('buffer', 0) for i in state['inventory']),
        'shortfall': any(i['on_hand'] < i['reserved'] for i in state['inventory']),
    }
    working = sorted(p['id'] for p in state['staff'] if not p.get('absent') and parse_time(p['shift_start']) <= at < parse_time(p['shift_end']))
    reasons = []
    if 'working' in previous and previous['working'] != working and command in {'advance', 'realtime-clock'}:
        reasons.append('勤務中のスタッフが変わりました')
    previous['working'] = working
    if command.startswith('staff/'):
        reasons.append('勤務・欠勤条件が変わりました')
    if command == 'inventory-adjustments':
        reasons.append('実棚が変わりました')
    if command.startswith('inventory-lots'):
        reasons.append('材料ロットが変わりました')
    if command.startswith('receive/'):
        delivery = next(d for d in state['deliveries'] if d['id'] == command.split('/')[1])
        if delivery.get('actual_quantity') != delivery['quantity']:
            reasons.append('予定と実際の入荷量が異なります')
    labels = {'low_stock': '使用可能な材料がバッファ以下になりました', 'queue': '注文数または経過待ち時間がしきい値を超えました',
              'review': '販売設定の見直し時刻になりました', 'shortfall': '予約材料が実在庫を超えています'}
    for key, value in edges.items():
        if value and not previous.get(key):
            reasons.append(labels[key])
    # Expired lots are identified individually, so one expiry does not hide the next.
    expired = sorted(l['id'] for i in state['inventory'] for l in i.get('lots', [])
                     if l['quantity'] > 0 and parse_time(l['expires_at']) <= at)
    if set(expired) - set(previous.get('expired_lots', [])):
        reasons.append('材料ロットの有効期限が過ぎました')
    previous.update(edges, expired_lots=expired)
    if not reasons:
        return
    incident = {'id': f'event-{uuid4().hex}', 'type': 'replan', 'at': state['business_now'],
                'message': '・'.join(reasons), 'revision': state['revision']}
    state['events'].insert(0, incident)
    state['events'] = state['events'][:100]
    state.update(replan_required=True, replan_event=incident)
