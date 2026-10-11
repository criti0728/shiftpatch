"""Business mutations. Call these only within a repository transaction."""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta
from math import ceil
from uuid import uuid4
from zoneinfo import ZoneInfo

from .errors import DomainError
from .timeutils import iso, parse_time


def get(items, identifier, label="対象"):
    item = next((value for value in items if value["id"] == identifier), None)
    if item is None:
        raise DomainError("not_found", f"{label}が見つかりません。", 404)
    return item


def event(state, kind, message):
    state["events"].insert(0, {"id": str(uuid4()), "type": kind, "at": state["business_now"], "message": message})
    state["events"] = state["events"][:100]


def bump(state):
    state["revision"] += 1
    for proposal in state["proposals"]:
        if proposal["status"] == "ready":
            proposal["status"] = "stale"


def all_tasks(state):
    return [task for order in state["orders"] for task in order["tasks"]]


def now(state):
    return parse_time(state["business_now"])


def close_time(state):
    from .planning.forecast import service_window
    return service_window(state)[1]


def open_time(state):
    from .planning.forecast import service_window
    return service_window(state)[0]


def active_staff(state, menu_id, at=None):
    at = at or now(state)
    return [p for p in state["staff"] if not p["absent"] and p["skills"].get(menu_id, 0) > 0
            and parse_time(p["shift_start"]) <= at and at + timedelta(seconds=p["skills"][menu_id]) <= parse_time(p["shift_end"])]


def assert_not_preparing(state):
    if any(t["status"] == "preparing" for t in all_tasks(state)):
        raise DomainError("work_in_progress", "調理中の注文を完了してから、在庫確定・締めを行ってください。")


def record_restrictions(state, at=None):
    """Remember censored demand even if stock or staffing is later restored."""
    at = at or now(state)
    if state.get("closed") or not open_time(state) <= at < close_time(state):
        return
    stock = {entry["id"]: entry for entry in state["inventory"]}
    for menu in state["menus"]:
        staffed = any(
            not person["absent"] and person["skills"].get(menu["id"], 0) > 0
            and parse_time(person["shift_start"]) <= at < parse_time(person["shift_end"])
            for person in state["staff"]
        )
        stocked = all(
            stock[key]["on_hand"] - stock[key]["reserved"] >= quantity
            and parse_time(stock[key]["expires_at"]) > at
            for key, quantity in menu["recipe"].items()
        )
        if not staffed or not stocked:
            state["restricted_today"] = True
            reason = f"{menu['name']}を通常販売できない時間帯がありました。"
            reasons = state.setdefault("restriction_reasons", [])
            if reason not in reasons:
                reasons.append(reason)


def overview(state, agent_mode="calculation_only"):
    value = deepcopy(state)
    value.pop("idempotency", None)
    value.pop("agent_runs", None)
    if value["mode"].get("review_at") and now(state) >= parse_time(value["mode"]["review_at"]):
        value["mode"]["status"] = "review_required"
    stock = {i["id"]: i for i in state["inventory"]}
    for menu in value["menus"]:
        eligible = active_staff(state, menu["id"])
        minimum_duration = min((p["skills"][menu["id"]] for p in eligible), default=0)
        potential_finish = now(state) + timedelta(seconds=minimum_duration)
        amounts = [max(0, stock[key]["on_hand"]-stock[key]["reserved"]) // qty
                   if parse_time(stock[key]["expires_at"]) > potential_finish else 0
                   for key, qty in menu["recipe"].items()]
        limit = None if state["mode"]["limits"] is None else max(0, state["mode"]["limits"].get(menu["id"], 0)-state["mode"]["used"].get(menu["id"], 0))
        available = min(amounts, default=0) if eligible and open_time(state) <= now(state) < close_time(state) and not state.get("closed") else 0
        menu["available_units"] = min(available, limit) if limit is not None else available
        menu["limit_remaining"] = limit
    for proposal in value["proposals"]:
        if proposal["status"] == "ready" and (proposal["revision"] != state["revision"] or
            (proposal.get("review_at") and now(state) >= parse_time(proposal["review_at"]))):
            proposal["status"] = "stale"
    completed = [o for o in value["orders"] if o["status"] == "completed"]
    value["metrics"] = {"completed_orders": len(completed), "revenue": sum(o["total"] for o in completed),
                        "waiting_orders": sum(o["status"] != "completed" for o in value["orders"])}
    value["config"] = {"agent_mode": agent_mode, "storage": "sqlite", "auth": "demo"}
    return value


def update_staff(state, staff_id, changes, next_day=False):
    person = get(state["next_day_staff"] if next_day else state["staff"], staff_id, "スタッフ")
    if not next_day and any(t["status"] == "preparing" and t["staff_id"] == staff_id for t in all_tasks(state)):
        raise DomainError("staff_busy", "担当中の作業を完了してから勤務条件を変更してください。")
    candidate = {**person, **changes}
    start, end = parse_time(candidate["shift_start"]), parse_time(candidate["shift_end"])
    if start >= end or end-start > timedelta(hours=16):
        raise DomainError("invalid_shift", "勤務終了は開始後、16時間以内にしてください。", 422)
    menu_ids = {m["id"] for m in state["menus"]}
    if set(candidate["skills"]) - menu_ids:
        raise DomainError("invalid_menu", "登録されていないメニューです。", 422)
    candidate["skills"] = {key: value for key, value in candidate["skills"].items() if value > 0}
    if start.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat() not in candidate.get("available_dates", []):
        candidate["availability_warning"] = "勤務可能日以外に配置されています。"
    else:
        candidate.pop("availability_warning", None)
    person.clear()
    person.update(candidate)
    if not next_day:
        record_restrictions(state)
    bump(state)
    event(state, "staff", f"{person['name']}の{'翌日' if next_day else '本日'}勤務を更新しました。")
    return {"ok": True}


def adjust_inventory(state, ingredient_id, on_hand):
    assert_not_preparing(state)
    ingredient = get(state["inventory"], ingredient_id, "材料")
    difference = on_hand - ingredient["on_hand"]
    ingredient["on_hand"] = on_hand
    record_restrictions(state)
    bump(state)
    event(state, "inventory", f"{ingredient['name']} 実棚 {on_hand}{ingredient['unit']}（差分 {difference:+d}）。")
    return {"ok": True}


def receive_delivery(state, delivery_id, quantity):
    delivery = get(state["deliveries"], delivery_id, "入荷予定")
    if delivery["received"]:
        raise DomainError("already_received", "この入荷はすでに記録されています。")
    record_restrictions(state)
    ingredient = get(state["inventory"], delivery["ingredient_id"], "材料")
    ingredient["on_hand"] += quantity
    delivery.update(received=True, actual_quantity=quantity, received_at=state["business_now"])
    bump(state)
    event(state, "delivery", f"{ingredient['name']} {quantity}{ingredient['unit']}を入荷（予定 {delivery['quantity']}）。")
    return {"ok": True}


def create_order(state, items):
    if state.get("closed") or now(state) >= close_time(state):
        raise DomainError("closed", "本日の新規注文受付は終了しました。")
    if now(state) < open_time(state):
        raise DomainError("not_open", "まだ開店時刻前です。")
    amounts = Counter()
    for item in items:
        amounts[item["menu_id"]] += item["quantity"]
    if sum(amounts.values()) > 20:
        raise DomainError("order_too_large", "1回の注文は20点までです。", 422)
    menus = {menu["id"]: menu for menu in state["menus"]}
    stock = {ingredient["id"]: ingredient for ingredient in state["inventory"]}
    needed = Counter()
    for menu_id, quantity in amounts.items():
        if menu_id not in menus:
            raise DomainError("invalid_menu", "登録されていないメニューです。", 422)
        menu = menus[menu_id]
        if not active_staff(state, menu_id):
            raise DomainError("no_staff", f"{menu['name']}を作れる勤務中のスタッフがいません。")
        limits = state["mode"]["limits"]
        if limits is not None and state["mode"]["used"].get(menu_id, 0) + quantity > limits.get(menu_id, 0):
            raise DomainError("sales_limit", f"{menu['name']}は販売停止中、または受付上限に達しています。")
        for ingredient_id, usage in menu["recipe"].items():
            needed[ingredient_id] += usage * quantity
    for ingredient_id, amount in needed.items():
        ingredient = stock[ingredient_id]
        if ingredient["on_hand"]-ingredient["reserved"] < amount or parse_time(ingredient["expires_at"]) <= now(state):
            raise DomainError("insufficient_inventory", f"{ingredient['name']}の使用可能在庫が不足しています。")
    identifier = str(uuid4())
    order = {"id": identifier, "number": len(state["orders"])+1, "created_at": state["business_now"], "status": "accepted", "items": [], "tasks": [], "total": 0}
    for menu_id, quantity in amounts.items():
        menu = menus[menu_id]
        order["items"].append({"menu_id": menu_id, "name": menu["name"], "quantity": quantity, "unit_price": menu["price"]})
        order["total"] += quantity * menu["price"]
        for _ in range(quantity):
            order["tasks"].append({"id": str(uuid4()), "menu_id": menu_id, "status": "accepted", "staff_id": None,
                                   "station": menu["station"], "started_at": None, "duration_seconds": None, "allocations": deepcopy(menu["recipe"])})
    for ingredient_id, amount in needed.items():
        stock[ingredient_id]["reserved"] += amount
    state["orders"].append(order)
    # Simulate the queue including the provisional order. A failure rolls back the entire transaction.
    from .simulation.engine import simulate
    simulation = simulate(state, {"action": "keep_current", "limits": {}, "review_after_minutes": 30},
                          horizon_minutes=max(1, ceil((close_time(state)-now(state)).total_seconds()/60)), include_arrivals=False)
    schedule = {row["task_id"]: row for row in simulation["schedule"]}
    for task in order["tasks"]:
        row = schedule.get(task["id"])
        if row is None or parse_time(row["finish_at"]) > close_time(state):
            raise DomainError("capacity_exceeded", "現在の人員・在庫では営業時間内に提供できません。注文内容を減らしてください。")
    for menu_id, quantity in amounts.items():
        state["mode"]["used"][menu_id] = state["mode"]["used"].get(menu_id, 0) + quantity
    record_restrictions(state)
    bump(state)
    event(state, "order", f"注文 #{order['number']:03d} を受け付け、材料を確保しました。")
    return {"order": deepcopy(order)}


def update_task(state, order_id, task_id, status, staff_id=None):
    order = get(state["orders"], order_id, "注文")
    task = get(order["tasks"], task_id, "作業")
    if status == task["status"]:
        return {"ok": True}
    stock = {i["id"]: i for i in state["inventory"]}
    for ingredient_id in task["allocations"]:
        ingredient = stock[ingredient_id]
        if ingredient["on_hand"] < ingredient["reserved"]:
            raise DomainError("reservation_shortfall", f"{ingredient['name']}の実在庫が予約量を下回っています。補充・実棚確認が必要です。")
    if status == "preparing" and task["status"] == "accepted":
        running = [t for t in all_tasks(state) if t["status"] == "preparing"]
        if any(t["station"] == task["station"] for t in running):
            raise DomainError("station_busy", "この作業台は使用中です。担当作業を完了してください。")
        occupied = {t["staff_id"] for t in running}
        available = [p for p in active_staff(state, task["menu_id"]) if p["id"] not in occupied and (not staff_id or p["id"] == staff_id)]
        if not available:
            raise DomainError("staff_busy", "このメニューを担当できる空きスタッフがいません。")
        person = min(available, key=lambda p: p["skills"][task["menu_id"]])
        duration = person["skills"][task["menu_id"]]
        finish = now(state) + timedelta(seconds=duration)
        for ingredient_id in task["allocations"]:
            if parse_time(stock[ingredient_id]["expires_at"]) <= finish:
                raise DomainError("expired_inventory", "提供予定時刻まで使える材料がありません。")
        # Accepted commitments survive closing. New orders have already passed
        # the service-window check; late fulfillment still requires valid shifts.
        task.update(status="preparing", staff_id=person["id"], started_at=state["business_now"], duration_seconds=duration)
    elif status == "completed" and task["status"] == "preparing":
        if now(state) < parse_time(task["started_at"]) + timedelta(seconds=task["duration_seconds"]):
            raise DomainError("task_not_ready", "準備時間がまだ経過していません。デモ時刻を進めてください。")
        for ingredient_id, quantity in task["allocations"].items():
            ingredient = stock[ingredient_id]
            if ingredient["on_hand"] < quantity or ingredient["reserved"] < quantity:
                raise DomainError("reservation_shortfall", "確保済み材料が不足しています。")
            ingredient["on_hand"] -= quantity
            ingredient["reserved"] -= quantity
        task["status"] = "completed"
        task["completed_at"] = state["business_now"]
    else:
        raise DomainError("invalid_transition", "この作業状態には変更できません。")
    statuses = {t["status"] for t in order["tasks"]}
    order["status"] = "completed" if statuses == {"completed"} else "accepted" if statuses == {"accepted"} else "preparing"
    if order["status"] == "completed":
        order["completed_at"] = state["business_now"]
    bump(state)
    event(state, "task", f"注文 #{order['number']:03d} の作業を{'開始' if status == 'preparing' else '完了'}しました。")
    return {"ok": True}


def approve(state, proposal_id, candidate_id, expected_revision):
    if state.get("closed") or not open_time(state) <= now(state) < close_time(state):
        raise DomainError("closed", "営業時間外は新しい販売設定を適用できません。")
    proposal = get(state["proposals"], proposal_id, "提案")
    if proposal["type"] != "service_plan":
        raise DomainError("wrong_proposal_type", "準備案から販売設定は変更できません。")
    if proposal["status"] != "ready" or expected_revision != state["revision"] or proposal["revision"] != state["revision"]:
        raise DomainError("stale_proposal", "条件が変わりました。最新の条件で再計算してください。")
    if proposal.get("review_at") and now(state) >= parse_time(proposal["review_at"]):
        raise DomainError("expired_proposal", "提案の有効時間が過ぎました。再計算してください。")
    candidate = get(proposal["candidates"], candidate_id, "候補")
    if not candidate["feasible"]:
        raise DomainError("infeasible_candidate", "この候補は実行できません。条件を確認してください。")
    if candidate["action"] == "replace_limits":
        limits = candidate["limits"]
        if set(limits) != {m["id"] for m in state["menus"]} or any(type(x) is not int or x < 0 for x in limits.values()):
            raise DomainError("invalid_candidate", "候補の販売上限が不正です。")
        state["mode"] = {"id": str(uuid4()), "limits": deepcopy(limits), "used": {}, "starts_at": state["business_now"],
                         "review_at": iso(now(state)+timedelta(minutes=candidate["review_after_minutes"])), "status": "active",
                         "proposal_id": proposal_id, "candidate_id": candidate_id}
        state["restricted_today"] = True
    proposal["status"] = "applied"
    proposal["selected_candidate_id"] = candidate_id
    proposal["applied_at"] = state["business_now"]
    bump(state)
    event(state, "approval", f"「{candidate['title']}」を反映しました。既存注文は保持されます。")
    return {"ok": True}


def acknowledge(state, proposal_id):
    proposal = get(state["proposals"], proposal_id, "提案")
    if proposal["type"] == "service_plan":
        raise DomainError("wrong_proposal_type", "販売案は承認操作で反映してください。")
    if proposal["status"] != "ready" or proposal["revision"] != state["revision"]:
        raise DomainError("stale_proposal", "条件が変わりました。再計算してください。")
    if proposal.get("review_at") and now(state) >= parse_time(proposal["review_at"]):
        raise DomainError("expired_proposal", "提案の有効時間が過ぎました。再計算してください。")
    proposal["status"] = "acknowledged"
    event(state, "acknowledge", "準備案を確認済みとして記録しました。実際の発注は行いません。")
    return {"ok": True}


def release_mode(state):
    state["mode"] = {"id": str(uuid4()), "limits": None, "used": {}, "starts_at": state["business_now"], "review_at": None, "status": "active"}
    bump(state)
    event(state, "mode", "販売制限を解除しました。在庫・人員の検査は引き続き行います。")
    return {"ok": True}


def advance(state, minutes):
    target = now(state)+timedelta(minutes=minutes)
    if target > close_time(state)+timedelta(hours=1):
        raise DomainError("demo_time_limit", "デモ時刻は閉店1時間後までです。初期化して再実行できます。")
    # Inspect transitions crossed by an accelerated clock, not only the final
    # instant: a temporary shift gap or stock expiry must not disappear at close.
    transitions = [now(state), target]
    transitions.extend(parse_time(person[key]) for person in state["staff"] for key in ("shift_start", "shift_end"))
    transitions.extend(parse_time(entry["expires_at"]) for entry in state["inventory"])
    for at in transitions:
        if now(state) <= at <= target:
            record_restrictions(state, at)
    state["business_now"] = iso(target)
    bump(state)
    event(state, "clock", f"営業時刻を{minutes}分進めました。")
    return {"ok": True}


def update_history(state, date, counts, restricted):
    if set(counts) != {m["id"] for m in state["menus"]}:
        raise DomainError("invalid_counts", "全メニューの販売数を入力してください。", 422)
    entry = next((h for h in state["history"] if h["date"] == date), None)
    if entry is None:
        raise DomainError("not_found", "対象の営業記録がありません。", 404)
    entry.update(counts=counts, restricted=restricted)
    bump(state)
    event(state, "history", f"{date} の販売記録を更新しました。")
    return {"ok": True}


def close_day(state):
    assert_not_preparing(state)
    if state.get("closed"):
        raise DomainError("already_closed", "本日の締めは確定済みです。")
    from .planning.forecast import forecast_sales
    # Compare against an actually acknowledged preparation plan when one exists.
    # A 30-minute service simulation is not a two-hour daily forecast.
    day = state["store"]["service_date"]
    plans = [proposal for proposal in state["proposals"]
             if proposal.get("type") == "prep_plan" and proposal.get("status") == "acknowledged"
             and proposal.get("prep", {}).get("service_date") == day]
    plan = max(plans, key=lambda proposal: proposal["created_at"], default=None)
    forecast = deepcopy(plan["prep"]["forecast"]) if plan else forecast_sales(state)[0]
    completed = [o for o in state["orders"] if o["status"] == "completed"]
    counts = {m["id"]: sum(i["quantity"] for o in completed for i in o["items"] if i["menu_id"] == m["id"]) for m in state["menus"]}
    # A shortened demo interval is censored too: do not compare its sales as a full two-hour demand day.
    pending_orders = sum(o["status"] != "completed" for o in state["orders"])
    record_restrictions(state)
    restricted = state["restricted_today"] or now(state) < close_time(state) or pending_orders > 0
    close = {"id": str(uuid4()), "closed_at": state["business_now"], "service_date": state["store"]["service_date"],
             "completed_orders": len(completed), "revenue": sum(o["total"] for o in completed), "counts": counts,
             "inventory": [{"ingredient_id": i["id"], "name": i["name"], "on_hand": i["on_hand"], "reserved": i["reserved"]} for i in state["inventory"]],
             "restricted": restricted, "forecast": forecast, "variance": {key: counts[key]-forecast.get(key, 0) for key in counts},
             "forecast_source": "acknowledged_prep_plan" if plan else "historical_baseline_at_close",
             "forecast_proposal_id": plan["id"] if plan else None,
             "notes": list(state.get("restriction_reasons", [])), "pending_orders": pending_orders}
    if not plan:
        close["notes"].append("確認済みの当日準備案がないため、締め時点の過去販売基準との比較です。")
    if pending_orders:
        close["notes"].append("未提供注文が残っているため、完了販売数を通常日の需要低下の根拠には使いません。")
    state["closes"].insert(0, close)
    state["history"].insert(0, {"date": close["service_date"], "counts": counts, "restricted": restricted})
    state["closed"] = True
    bump(state)
    event(state, "close", "本日の締めを確定しました。未提供注文の材料は引き続き確保します。")
    return {"close": deepcopy(close)}
