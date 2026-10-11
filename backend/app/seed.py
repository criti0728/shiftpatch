"""Small, explicitly synthetic café. The business clock is independent of wall time."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from .timeutils import iso


def seed_state() -> dict:
    start = datetime(2026, 10, 11, 2, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=2)
    menus = [
        {"id": "americano", "name": "アメリカーノ", "category": "drink", "price": 480, "station": "bar", "recipe": {"beans": 18, "cups": 1}, "emoji": "☕"},
        {"id": "latte", "name": "カフェラテ", "category": "drink", "price": 580, "station": "bar", "recipe": {"beans": 18, "milk": 180, "cups": 1}, "emoji": "🥛"},
        {"id": "matcha", "name": "抹茶ラテ", "category": "drink", "price": 620, "station": "bar", "recipe": {"matcha": 5, "milk": 180, "cups": 1}, "emoji": "🍵"},
        {"id": "sandwich", "name": "ハムチーズサンド", "category": "food", "price": 780, "station": "kitchen", "recipe": {"bread": 2, "ham": 40, "cheese": 20}, "emoji": "🥪"},
        {"id": "cheese_toast", "name": "チーズトースト", "category": "food", "price": 650, "station": "kitchen", "recipe": {"bread": 1, "cheese": 30}, "emoji": "🍞"},
        {"id": "cake", "name": "本日のケーキ", "category": "food", "price": 520, "station": "kitchen", "recipe": {"cake": 1}, "emoji": "🍰"},
    ]
    staff = [
        {"id": "aoi", "name": "葵", "role": "バリスタ", "absent": False, "shift_start": iso(start), "shift_end": iso(end), "skills": {"americano": 90, "latte": 120, "matcha": 100, "sandwich": 480, "cheese_toast": 360, "cake": 60}},
        {"id": "ren", "name": "蓮", "role": "バリスタ", "absent": False, "shift_start": iso(start), "shift_end": iso(end), "skills": {"americano": 110, "latte": 150, "matcha": 120, "cake": 80}},
        {"id": "yui", "name": "結衣", "role": "キッチン", "absent": False, "shift_start": iso(start), "shift_end": iso(end), "skills": {"sandwich": 180, "cheese_toast": 150, "cake": 45}},
        {"id": "sora", "name": "空", "role": "午後シフト", "absent": False, "shift_start": iso(end), "shift_end": iso(end + timedelta(hours=3)), "skills": {"americano": 120, "latte": 160, "sandwich": 240, "cheese_toast": 200, "cake": 60}},
    ]
    for person in staff:
        person["available_dates"] = [f"2026-10-{day:02d}" for day in range(1, 32)]
    inventory = [
        {"id": "beans", "name": "コーヒー豆", "unit": "g", "on_hand": 1200, "buffer": 180},
        {"id": "milk", "name": "牛乳", "unit": "mL", "on_hand": 4000, "buffer": 500},
        {"id": "cups", "name": "カップ", "unit": "個", "on_hand": 80, "buffer": 10},
        {"id": "matcha", "name": "抹茶", "unit": "g", "on_hand": 150, "buffer": 20},
        {"id": "bread", "name": "食パン", "unit": "枚", "on_hand": 22, "buffer": 4},
        {"id": "ham", "name": "ハム", "unit": "g", "on_hand": 440, "buffer": 80},
        {"id": "cheese", "name": "チーズ", "unit": "g", "on_hand": 450, "buffer": 60},
        {"id": "cake", "name": "ケーキ", "unit": "個", "on_hand": 10, "buffer": 2},
    ]
    for entry in inventory:
        entry.update(reserved=0, expires_at=iso(start + timedelta(days=3)))
    tomorrow = deepcopy(staff)
    for person in tomorrow:
        person["shift_start"] = iso(datetime.fromisoformat(person["shift_start"].replace("Z", "+00:00")) + timedelta(days=1))
        person["shift_end"] = iso(datetime.fromisoformat(person["shift_end"].replace("Z", "+00:00")) + timedelta(days=1))
    state = {
        "store": {"id": "demo", "name": "喫茶 こもれび", "service_date": "2026-10-11", "open_time": "11:00", "close_time": "13:00", "timezone": "Asia/Tokyo"},
        "business_now": iso(start), "revision": 1, "staff": staff, "next_day_staff": tomorrow,
        "menus": menus, "inventory": inventory,
        "deliveries": [{"id": "delivery-milk", "ingredient_id": "milk", "quantity": 2000, "arrives_at": iso(start + timedelta(minutes=30)), "confirmed": True, "received": False}],
        "orders": [], "mode": {"id": "mode-initial", "limits": None, "used": {}, "starts_at": iso(start), "review_at": None, "status": "active"},
        "proposals": [], "closes": [], "events": [], "idempotency": {}, "agent_runs": {},
        "history": [{"date": (start - timedelta(days=days)).date().isoformat(), "counts": {"americano": 18 + days % 3, "latte": 22 - days % 3, "matcha": 8, "sandwich": 14 + days % 2, "cheese_toast": 10, "cake": 7}, "restricted": False} for days in range(1, 8)],
        "restricted_today": False,
    }
    # Accepted orders are backed by real reservations, identical to the operation invariant.
    for number, ids in enumerate([["latte", "sandwich"], ["americano", "cheese_toast"], ["latte", "sandwich"]], 1):
        items, tasks = [], []
        for index, menu_id in enumerate(ids, 1):
            menu = next(m for m in menus if m["id"] == menu_id)
            items.append({"menu_id": menu_id, "name": menu["name"], "quantity": 1, "unit_price": menu["price"]})
            tasks.append({"id": f"seed-{number}-{index}", "menu_id": menu_id, "status": "accepted", "staff_id": None, "station": menu["station"], "started_at": None, "duration_seconds": None, "allocations": deepcopy(menu["recipe"])})
            for ingredient_id, quantity in menu["recipe"].items():
                next(i for i in inventory if i["id"] == ingredient_id)["reserved"] += quantity
        state["orders"].append({"id": f"order-seed-{number}", "number": number, "created_at": iso(start - timedelta(minutes=4-number)), "status": "accepted", "items": items, "tasks": tasks, "total": sum(i["unit_price"] for i in items)})
    return state
