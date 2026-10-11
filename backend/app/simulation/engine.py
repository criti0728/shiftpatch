"""Deterministic staff + station scheduling against real, reserved inventory.

Each menu unit is one uninterrupted task. Future demand is generated once over
the complete business day, so all candidates (and shorter windows) observe the
same arrivals. This is a scenario calculation, not a claim of forecast accuracy.
"""
from collections import defaultdict
from datetime import datetime, timedelta
from random import Random

from ..planning.forecast import forecast_sales, service_window
from ..timeutils import iso, parse_time


ENGINE_VERSION = "1.0.0"


def _first_slot(start: datetime, seconds: int, intervals: list[tuple[datetime, datetime]]) -> datetime:
    cursor = start
    for busy_start, busy_end in sorted(intervals):
        if busy_end <= cursor:
            continue
        if cursor + timedelta(seconds=seconds) <= busy_start:
            break
        cursor = max(cursor, busy_end)
    return cursor


def _arrival_scenario(state: dict, seed: int) -> list[dict]:
    start, end = service_window(state)
    counts, _ = forecast_sales(state)
    rng = Random(seed)
    duration = (end - start).total_seconds()
    arrivals = []
    for menu_id in sorted(counts):
        for index in range(counts[menu_id]):
            # Stratification avoids random empty intervals but preserves varied order.
            arrived = start + timedelta(seconds=(index + rng.random()) * duration / counts[menu_id])
            arrivals.append({"id": f"arrival-{menu_id}-{index}", "menu_id": menu_id, "arrived": arrived})
    return sorted(arrivals, key=lambda row: (row["arrived"], row["id"]))


def simulate(
    state: dict, candidate: dict, horizon_minutes: int = 30,
    seed: int = 42, include_arrivals: bool = True,
) -> dict:
    """Project service without mutating state, reserving staff and stations together.

    ``existing_at_risk`` counts existing units that cannot be validly assigned.
    Scheduled work beyond the horizon remains pending, with elapsed waiting time.
    New arrivals that cannot be safely fulfilled are explicitly rejected.
    """
    if not isinstance(horizon_minutes, (int, float)) or not 0 < horizon_minutes <= 1440:
        raise ValueError("horizon_minutes must be within (0, 1440]")
    now = parse_time(state["business_now"])
    horizon = now + timedelta(minutes=horizon_minutes)
    menus = {menu["id"]: menu for menu in state["menus"]}
    staff = {person["id"]: person for person in state["staff"]}
    inventory = {entry["id"]: entry for entry in state["inventory"]}
    mode = state.get("mode", {})
    action = candidate.get("action", "keep_current")
    if action not in {"keep_current", "replace_limits"}:
        raise ValueError("unsupported candidate action")
    raw_limits = mode.get("limits") if action == "keep_current" else candidate.get("limits", {})
    limits = raw_limits or {}
    if any(key not in menus or type(value) is not int or value < 0 for key, value in limits.items()):
        raise ValueError("limits must be non-negative integer quantities for existing menus")
    used = dict(mode.get("used", {})) if action == "keep_current" else {}
    notes = []
    if action == "keep_current" and mode.get("review_at") and parse_time(mode["review_at"]) <= now and limits:
        notes.append("販売制限の見直し時刻を過ぎています。解除の承認までは既存の上限と使用量を維持します。")
    if include_arrivals:
        _, forecast_notes = forecast_sales(state)
        notes.extend(forecast_notes)

    # Reserved stock and free stock are separate pools: existing jobs never pay twice.
    reserved_stock = {key: max(0, min(entry["on_hand"], entry.get("reserved", 0))) for key, entry in inventory.items()}
    free_stock = {key: max(0, entry["on_hand"] - entry.get("reserved", 0)) for key, entry in inventory.items()}
    for key, entry in inventory.items():
        if entry.get("reserved", 0) > entry["on_hand"]:
            notes.append(f"{entry['name']}：実在庫が予約量を下回っています。影響する作業の確認が必要です。")
    calendars: dict[str, list] = defaultdict(list)
    schedule, jobs = [], []
    rejected = 0

    def assignment(menu_id: str, earliest: datetime, station: str | None = None):
        options = []
        menu = menus.get(menu_id)
        if menu is None:
            return None
        for person in staff.values():
            seconds = person.get("skills", {}).get(menu_id)
            if person.get("absent") or not isinstance(seconds, (int, float)) or seconds <= 0:
                continue
            shift_start, shift_end = parse_time(person["shift_start"]), parse_time(person["shift_end"])
            start = _first_slot(max(now, earliest, shift_start), seconds,
                                calendars[f"staff:{person['id']}"] + calendars[f"station:{station or menu['station']}"])
            finish = start + timedelta(seconds=seconds)
            if finish <= shift_end:
                options.append((finish, start, person["id"]))
        if not options:
            return None
        finish, start, staff_id = min(options)
        return staff_id, start, finish

    def usable(recipe: dict, pool: dict, finish: datetime) -> bool:
        return all(
            key in inventory and quantity <= pool.get(key, 0)
            and parse_time(inventory[key]["expires_at"]) > finish
            for key, quantity in recipe.items()
        )

    def reserve_slot(job: dict, staff_id: str, start: datetime, finish: datetime):
        station = job["station"]
        calendars[f"staff:{staff_id}"].append((start, finish))
        calendars[f"station:{station}"].append((start, finish))
        job["finish"] = finish
        schedule.append({
            "task_id": job["id"], "menu_id": job["menu_id"], "staff_id": staff_id,
            "station": station, "start_at": iso(start), "finish_at": iso(finish),
            "existing": job["existing"],
        })

    unfinished_orders = [order for order in state.get("orders", []) if order.get("status") != "completed"]
    # Stable sorting preserves acceptance order when the demo clock has not moved.
    for order in sorted(unfinished_orders, key=lambda item: item["created_at"]):
        prices = {item["menu_id"]: item.get("unit_price", menus.get(item["menu_id"], {}).get("price", 0)) for item in order["items"]}
        for task in order.get("tasks", []):
            if task["status"] == "completed":
                continue
            menu = menus.get(task["menu_id"], {})
            jobs.append({
                **task, "order_id": order["id"], "existing": True,
                "arrived": parse_time(order["created_at"]), "price": prices.get(task["menu_id"], 0),
                "station": task.get("station") or menu.get("station", "unknown"),
                "recipe": task.get("allocations", menu.get("recipe", {})),
                "finish": None, "at_risk": False,
            })

    # Pinned work takes precedence even over older accepted orders.
    for job in [entry for entry in jobs if entry["status"] == "preparing"]:
        person = staff.get(job.get("staff_id"))
        if not person or not job.get("started_at") or not job.get("duration_seconds"):
            job["at_risk"] = True
            notes.append(f"既存作業 {job['id']}：担当者・開始時刻が記録されていないため、割り当てを検証できません。")
            continue
        start = parse_time(job["started_at"])
        finish = start + timedelta(seconds=job["duration_seconds"])
        conflicts = calendars[f"staff:{person['id']}"] + calendars[f"station:{job['station']}"]
        valid = (
            not person.get("absent") and start <= now and finish > now
            and start >= parse_time(person["shift_start"]) and finish <= parse_time(person["shift_end"])
            and not any(start < right and finish > left for left, right in conflicts)
            and usable(job["recipe"], reserved_stock, finish)
        )
        if not valid:
            job["at_risk"] = True
            notes.append(f"既存作業 {job['id']}：調理中の担当・終了予定時刻・在庫を確認してください。")
            # An overdue or inconsistent in-progress task cannot silently free resources.
            calendars[f"staff:{person['id']}"].append((now, max(horizon, finish)))
            calendars[f"station:{job['station']}"].append((now, max(horizon, finish)))
            continue
        reserve_slot(job, person["id"], start, finish)
        for key, quantity in job["recipe"].items():
            reserved_stock[key] -= quantity

    for job in [entry for entry in jobs if entry["status"] != "preparing"]:
        found = assignment(job["menu_id"], job["arrived"], job["station"])
        if found is None or not usable(job["recipe"], reserved_stock, found[2]):
            job["at_risk"] = True
            notes.append(f"既存作業 {job['id']}：勤務・作業台、または使用時点の予約済み在庫が不足しています。")
            continue
        reserve_slot(job, *found)
        for key, quantity in job["recipe"].items():
            reserved_stock[key] -= quantity

    if include_arrivals:
        _, service_end = service_window(state)
        for arrival in _arrival_scenario(state, seed):
            if not now <= arrival["arrived"] < horizon:
                continue
            menu_id = arrival["menu_id"]
            menu = menus[menu_id]
            if raw_limits is not None and used.get(menu_id, 0) >= limits.get(menu_id, 0):
                rejected += 1
                continue
            found = assignment(menu_id, arrival["arrived"])
            if found is None or found[2] > service_end or not usable(menu["recipe"], free_stock, found[2]):
                rejected += 1
                continue
            job = {**arrival, "order_id": arrival["id"], "existing": False, "price": menu["price"],
                   "station": menu["station"], "recipe": menu["recipe"], "finish": None, "at_risk": False}
            reserve_slot(job, *found)
            jobs.append(job)
            used[menu_id] = used.get(menu_id, 0) + 1
            for key, quantity in menu["recipe"].items():
                free_stock[key] -= quantity

    completed = [job for job in jobs if job["finish"] is not None and job["finish"] <= horizon]
    pending = [job for job in jobs if job["finish"] is None or job["finish"] > horizon]
    by_order = defaultdict(list)
    for job in jobs:
        by_order[job["order_id"]].append(job)
    completed_order_ids = {
        order_id for order_id, group in by_order.items()
        if all(job["finish"] is not None and job["finish"] <= horizon for job in group)
    }
    # Revenue is recognized on delivery of the entire order, matching actual close
    # records. An already prepared line in a partially completed order is included
    # when its final outstanding task finishes, at the recorded order-time price.
    revenue = sum(
        order.get("total", sum(item.get("unit_price", 0) * item["quantity"] for item in order["items"]))
        for order in unfinished_orders if order["id"] in completed_order_ids
    )
    revenue += sum(job["price"] for job in jobs if not job["existing"] and job["order_id"] in completed_order_ids)
    waits = [max(0, ((job["finish"] if job["finish"] is not None and job["finish"] <= horizon else horizon) - job["arrived"]).total_seconds() / 60) for job in jobs]
    at_risk = sum(job["at_risk"] for job in jobs if job["existing"])
    if pending:
        notes.append(f"区間終了時に{len(pending)}点が未完了です。経過待ち時間を最大待ち時間に含めています。")
    return {
        "metrics": {"completed_units": len(completed), "completed_orders": len(completed_order_ids),
                    "rejected_units": rejected, "pending_units": len(pending),
                    "max_wait_minutes": round(max(waits, default=0), 1),
                    "revenue": revenue, "existing_at_risk": at_risk},
        "feasible": at_risk == 0, "notes": list(dict.fromkeys(notes)),
        "schedule": sorted(schedule, key=lambda row: (row["start_at"], row["task_id"])),
    }
