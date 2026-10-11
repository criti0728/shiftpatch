"""Small, explainable forecasts; restricted sales are censored observations."""
from datetime import date, datetime, time, timedelta
from math import floor
from zoneinfo import ZoneInfo


DEMO_DEFAULTS = {
    "americano": 18, "latte": 21, "matcha": 8,
    "sandwich": 14, "cheese_toast": 10, "cake": 7,
}


def service_window(state: dict, service_date: str | None = None) -> tuple[datetime, datetime]:
    store = state["store"]
    day = date.fromisoformat(service_date or store["service_date"])
    tz = ZoneInfo(store.get("timezone", "Asia/Tokyo"))
    start = datetime.combine(day, time.fromisoformat(store.get("open_time", "11:00")), tz)
    end = datetime.combine(day, time.fromisoformat(store.get("close_time", "13:00")), tz)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def forecast_sales(state: dict, service_date: str | None = None) -> tuple[dict[str, int], list[str]]:
    """Use up to seven prior unrestricted days, with an explicit sparse-data fallback."""
    target = service_date or state["store"]["service_date"]
    rows = sorted(
        (row for row in state.get("history", []) if row.get("date", "") < target),
        key=lambda row: row.get("date", ""), reverse=True,
    )[:7]
    usable = [row for row in rows if not row.get("restricted", False)]
    notes = []
    if len(usable) < len(rows):
        notes.append(f"販売制限・品切れがあった{len(rows) - len(usable)}日分は需要の基準から除外しました。")
    defaults = state.get("store", {}).get("default_forecast", DEMO_DEFAULTS)
    if len(usable) < 3:
        notes.append("通常販売の標本が3日未満です。店長の基準値（未設定時は合成デモの基準値）を使用します。")
    result = {}
    for menu in state["menus"]:
        menu_id = menu["id"]
        samples = [row["counts"][menu_id] for row in usable if menu_id in row.get("counts", {})]
        if len(samples) >= 3:
            result[menu_id] = max(0, floor(sum(samples) / len(samples) + 0.5))
        else:
            result[menu_id] = max(0, int(defaults.get(menu_id, 0)))
    return result, notes


def forecast_windows(state: dict, service_date: str | None = None) -> tuple[list[dict], list[str]]:
    """30-minute demand with observed per-menu profiles; exact daily totals.

    Missing interval samples use an explicitly labelled uniform profile. Restricted
    days are never used as evidence of low demand.
    """
    from ..timeutils import iso
    target = service_date or state['store']['service_date']
    start, end = service_window(state, target)
    totals, notes = forecast_sales(state, target)
    rows = sorted((r for r in state.get('history', []) if r.get('date', '') < target),
                  key=lambda r: r['date'], reverse=True)[:7]
    usable = [r for r in rows if not r.get('restricted') and r.get('windows')]
    bounds = []
    left = start
    while left < end:
        right = min(end, left + timedelta(minutes=30))
        bounds.append((left, right))
        left = right
    windows = [{'start_at': iso(l), 'end_at': iso(r), 'counts': {},
                'basis': {}, 'sample_count': len(usable)} for l, r in bounds]
    for menu_id, total in totals.items():
        weights = []
        observed = len(usable) >= 3
        for left, right in bounds:
            bucket = left.strftime('%H:%M')
            samples = [next((w.get('counts', {}).get(menu_id, 0) for w in row['windows']
                             if w['start_time'] == bucket), 0) for row in usable]
            weights.append(sum(samples) / len(samples) if observed else (right-left).total_seconds())
        if sum(weights) <= 0:
            weights = [(r-l).total_seconds() for l, r in bounds]
            observed = False
        raw = [total * w / sum(weights) for w in weights]
        amounts = [floor(v) for v in raw]
        for index in sorted(range(len(raw)), key=lambda i: (-(raw[i]-amounts[i]), i))[:total-sum(amounts)]:
            amounts[index] += 1
        for window, count in zip(windows, amounts):
            window['counts'][menu_id] = count
            window['basis'][menu_id] = 'observed_intervals' if observed else 'uniform_fallback'
    if any('uniform_fallback' in w['basis'].values() for w in windows):
        notes.append('時間帯別の通常販売標本が不足する項目は均等配分です。実測の時間帯予測ではありません。')
    return windows, notes
