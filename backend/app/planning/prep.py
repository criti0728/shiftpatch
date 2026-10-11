"""Ingredient planning using observable demand and the target day's staffing."""
from collections import Counter
from copy import deepcopy
from datetime import timedelta

from ..simulation.engine import simulate
from ..timeutils import iso, parse_time
from .forecast import forecast_sales, service_window


def calculate_prep(state: dict, service_date: str | None = None) -> dict:
    current_date = state["store"]["service_date"]
    target = service_date or current_date
    start, end = service_window(state, target)
    forecast, notes = forecast_sales(state, target)
    notes.append("在庫は対象日の閉店まで有効な未予約分、入荷は開店までに到着する確定分で計算しています。")
    if target == current_date:
        people = deepcopy(state["staff"])
    else:
        people = deepcopy(state.get("next_day_staff", []))
        if not people:
            notes.append("対象日の確定シフトがありません。提供可能量を0としているため、シフトを登録して再計算してください。")

    # Determine the feasible mix with enough ingredients; purchase quantities must
    # not promise more service than the staff + shared stations can deliver.
    planned_state = deepcopy(state)
    planned_state["store"]["service_date"] = target
    planned_state["business_now"] = iso(start)
    planned_state["staff"] = people
    for person in people:
        person["shift_start"] = iso(max(start, parse_time(person["shift_start"])))
        person["shift_end"] = iso(min(end, parse_time(person["shift_end"])))
    planned_state["orders"] = []
    planned_state["mode"] = {"limits": None, "used": {}}
    for entry in planned_state["inventory"]:
        entry.update(on_hand=10**9, reserved=10**9, expires_at=iso(end + timedelta(days=1)))
    menus = {menu["id"]: menu for menu in state["menus"]}
    # Fair interleaving instead of exhausting all capacity on the first menu.
    for count in range(max(forecast.values(), default=0)):
        for menu_id, quantity in forecast.items():
            if count >= quantity:
                continue
            menu = menus[menu_id]
            task_id = f"prep-{menu_id}-{count}"
            planned_state["orders"].append({
                "id": task_id, "created_at": iso(start), "status": "accepted",
                "items": [{"menu_id": menu_id, "quantity": 1, "unit_price": menu["price"]}],
                "tasks": [{"id": task_id, "menu_id": menu_id, "status": "accepted", "station": menu["station"],
                           "allocations": deepcopy(menu["recipe"])}],
            })
    # The engine respects list order for equal timestamps; IDs make interleaving
    # explicit and stable even if the engine sorts input records.
    for index, order in enumerate(planned_state["orders"]):
        order["id"] = f"prep-{index:05d}"
    capacity = simulate(planned_state, {"action": "keep_current"},
                        horizon_minutes=(end - start).total_seconds() / 60, include_arrivals=False)
    scheduled = Counter(row["menu_id"] for row in capacity["schedule"] if parse_time(row["finish_at"]) <= end)
    planned = {menu_id: scheduled[menu_id] for menu_id in forecast}
    for menu_id, count in planned.items():
        if count < forecast[menu_id]:
            notes.append(f"{menus[menu_id]['name']}：需要予測{forecast[menu_id]}点のうち、確定シフト・作業台の条件では{count}点を準備できます。")
    needed = Counter()
    for menu_id, count in planned.items():
        for ingredient_id, amount in menus[menu_id]["recipe"].items():
            needed[ingredient_id] += count * amount
    items = []
    for entry in state["inventory"]:
        available = max(0, entry["on_hand"] - entry.get("reserved", 0)) if parse_time(entry["expires_at"]) > end else 0
        incoming = sum(
            delivery["quantity"] for delivery in state.get("deliveries", [])
            if delivery["ingredient_id"] == entry["id"] and delivery.get("confirmed") and not delivery.get("received")
            and parse_time(delivery["arrives_at"]) <= start
            and (not delivery.get("expires_at") or parse_time(delivery["expires_at"]) > end)
        )
        buffer = max(0, entry.get("buffer", 0))
        items.append({"ingredient_id": entry["id"], "name": entry["name"], "unit": entry["unit"],
                      "needed": needed[entry["id"]], "available": available, "incoming": incoming,
                      "buffer": buffer, "to_order": max(0, needed[entry["id"]] + buffer - available - incoming)})
    return {"service_date": target, "forecast": forecast, "planned": planned, "items": items, "notes": notes}
