"""Persisted sample clock: manually advanced, or paced by real elapsed time."""
from datetime import datetime, timezone
from .timeutils import iso, parse_time


def tick(state, wall=None):
    clock = state.get('clock', {})
    if clock.get('mode') != 'realtime':
        return False
    wall = wall or datetime.now(timezone.utc)
    elapsed = max(0, (wall - parse_time(clock['wall_anchor'])).total_seconds())
    from datetime import timedelta
    business = parse_time(clock['business_anchor']) + timedelta(seconds=elapsed)
    # Second resolution prevents a read from rewriting sub-second timestamps.
    business = business.replace(microsecond=0)
    if business <= parse_time(state['business_now']):
        return False
    from .operations import record_restrictions
    from .inventory import ensure
    prior = parse_time(state['business_now'])
    boundaries = [prior, business]
    boundaries += [parse_time(p[k]) for p in state['staff'] for k in ('shift_start', 'shift_end')]
    boundaries += [parse_time(l['expires_at']) for i in state['inventory'] for l in ensure(i)]
    for at in boundaries:
        if prior <= at <= business:
            record_restrictions(state, at)
    state['business_now'] = iso(business)
    return True


def set_mode(state, mode):
    tick(state)
    state['clock'] = {'mode': mode, 'wall_anchor': iso(datetime.now(timezone.utc)),
                      'business_anchor': state['business_now']}
