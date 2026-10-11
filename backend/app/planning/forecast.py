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
