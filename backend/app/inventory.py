"""Lot inventory, immutable reservation ownership and FIFO consumption.

Legacy single-row inventory is migrated in place. Scalar totals remain a projection
for old clients; mutations always synchronize them from lots.
"""
from copy import deepcopy
from uuid import uuid4
from .errors import DomainError
from .timeutils import parse_time


def sync(entry):
    entry['on_hand'] = sum(lot['quantity'] for lot in entry['lots'])
    entry['reserved'] = sum(lot['reserved'] for lot in entry['lots'])
    live = [lot['expires_at'] for lot in entry['lots'] if lot['quantity'] > 0]
    if live:
        entry['expires_at'] = min(live)


def ensure(entry, at=None):
    # Also permits old maintenance/import code to change aggregate rows before
    # migration. API lot mutations never take this legacy path.
    lots = entry.get('lots')
    if lots is None or (sum(l['quantity'] for l in lots) != entry['on_hand'] or
                        sum(l['reserved'] for l in lots) != entry.get('reserved', 0)):
        entry['lots'] = [{'id': f"legacy-{entry['id']}", 'quantity': entry['on_hand'],
                          'reserved': entry.get('reserved', 0), 'expires_at': entry['expires_at'],
                          'received_at': at or '1970-01-01T00:00:00Z'}]
    elif len(lots) == 1 and lots[0]['expires_at'] != entry['expires_at']:
        lots[0]['expires_at'] = entry['expires_at']
    return entry['lots']


def available(entry, at):
    return sum(max(0, lot['quantity'] - lot['reserved']) for lot in ensure(entry)
               if parse_time(lot['expires_at']) > at)


def reserve(entry, quantity, at):
    lots = sorted(ensure(entry), key=lambda l: (l['received_at'], l['id']))
    if available(entry, at) < quantity:
        raise DomainError('insufficient_inventory', f"{entry['name']}の使用可能ロットが不足しています。")
    allocations = []
    for lot in lots:
        if parse_time(lot['expires_at']) <= at:
            continue
        take = min(quantity, max(0, lot['quantity'] - lot['reserved']))
        if take:
            lot['reserved'] += take
            allocations.append({'lot_id': lot['id'], 'quantity': take})
            quantity -= take
        if not quantity:
            break
    sync(entry)
    return allocations


def migrate(state):
    stock = {entry['id']: entry for entry in state['inventory']}
    for entry in stock.values():
        ensure(entry, state['business_now'])
    # Bind accepted legacy commitments to reserved lots without double reserving.
    remaining = {key: {l['id']: l['reserved'] for l in entry['lots']} for key, entry in stock.items()}
    for order in state.get('orders', []):
        for task in order['tasks']:
            if task['status'] == 'completed' or task.get('reservation_pending'):
                continue
            if task.get('lot_allocations'):
                for key, rows in task['lot_allocations'].items():
                    for row in rows:
                        if row['lot_id'] in remaining[key]:
                            remaining[key][row['lot_id']] -= row['quantity']
                continue
            allocations = {}
            for key, count in task['allocations'].items():
                rows = []
                for lot in sorted(stock[key]['lots'], key=lambda l: (l['received_at'], l['id'])):
                    take = min(count, max(0, remaining[key][lot['id']]))
                    if take:
                        rows.append({'lot_id': lot['id'], 'quantity': take})
                        remaining[key][lot['id']] -= take
                        count -= take
                allocations[key] = rows
            task['lot_allocations'] = allocations
    return stock


def validate_task(stock, task, finish):
    for key, quantity in task['allocations'].items():
        rows = task.get('lot_allocations', {}).get(key, [])
        if sum(row['quantity'] for row in rows) != quantity:
            raise DomainError('reservation_shortfall', '確保したロットが不足しています。実棚を確認してください。')
        lots = {l['id']: l for l in ensure(stock[key])}
        for row in rows:
            lot = lots.get(row['lot_id'])
            if not lot or lot['quantity'] < row['quantity'] or lot['reserved'] < row['quantity']:
                raise DomainError('reservation_shortfall', '確保したロットが不足しています。')
            if parse_time(lot['expires_at']) <= finish:
                raise DomainError('expired_inventory', '確保した材料の有効期限が提供予定時刻より前です。')


def consume(stock, task, finish):
    validate_task(stock, task, finish)
    for key, rows in task['lot_allocations'].items():
        lots = {l['id']: l for l in stock[key]['lots']}
        for row in rows:
            lots[row['lot_id']]['quantity'] -= row['quantity']
            lots[row['lot_id']]['reserved'] -= row['quantity']
        sync(stock[key])


def receipt(entry, quantity, expires_at, received_at):
    ensure(entry, received_at)
    entry['lots'].append({'id': f'lot-{uuid4().hex}', 'quantity': quantity, 'reserved': 0,
                          'expires_at': expires_at, 'received_at': received_at})
    sync(entry)


def adjust(entry, on_hand, at):
    lots = ensure(entry, at)
    difference = on_hand - entry['on_hand']
    if difference > 0:
        receipt(entry, difference, entry['expires_at'], at)
    elif difference < 0:
        missing = -difference
        # Reduce free quantities first; reservation shortfalls stay visible rather
        # than being silently deleted. Their owner tasks cannot start/complete.
        for reserved_pass in (False, True):
            for lot in reversed(sorted(lots, key=lambda l: (l['received_at'], l['id']))):
                can_remove = lot['quantity'] if reserved_pass else max(0, lot['quantity'] - lot['reserved'])
                take = min(missing, can_remove)
                lot['quantity'] -= take
                missing -= take
                if not missing:
                    break
            if not missing:
                break
        sync(entry)


def simulation_pools(state):
    private = deepcopy(state)
    stock = migrate(private)
    reserved, free = {}, {}
    for key, entry in stock.items():
        reserved[key] = [{**l, 'remaining': max(0, min(l['quantity'], l['reserved']))} for l in entry['lots']]
        free[key] = [{**l, 'remaining': max(0, l['quantity'] - l['reserved'])} for l in entry['lots']]
    return private, reserved, free


def take_pool(pool, recipe, finish, *, allocations=None, commit=False):
    planned = []
    for key, quantity in recipe.items():
        lots = sorted(pool.get(key, []), key=lambda l: (l['received_at'], l['id']))
        if allocations is not None:
            rows = allocations.get(key, [])
            if sum(r['quantity'] for r in rows) != quantity:
                return False
            by_id = {l['id']: l for l in lots}
            for row in rows:
                lot = by_id.get(row['lot_id'])
                if not lot or lot['remaining'] < row['quantity'] or parse_time(lot['expires_at']) <= finish:
                    return False
                planned.append((lot, row['quantity']))
        else:
            for lot in lots:
                if parse_time(lot['expires_at']) <= finish:
                    continue
                take = min(quantity, lot['remaining'])
                planned.append((lot, take))
                quantity -= take
                if not quantity:
                    break
            if quantity:
                return False
    if commit:
        for lot, take in planned:
            lot['remaining'] -= take
    return True
