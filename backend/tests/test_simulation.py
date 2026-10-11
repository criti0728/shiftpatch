from copy import deepcopy
from datetime import timedelta

from app.seed import seed_state
from app.simulation.engine import simulate
from app.timeutils import iso, parse_time


KEEP = {"action": "keep_current"}


def empty_state():
    state = seed_state()
    state["orders"] = []
    for item in state["inventory"]:
        item["reserved"] = 0
    return state


def test_same_scenario_is_deterministic_pure_and_resource_safe():
    state = seed_state()
    before = deepcopy(state)
    result = simulate(state, KEEP, horizon_minutes=120)
    assert result == simulate(state, KEEP, horizon_minutes=120)
    assert state == before
    assert result["metrics"]["completed_units"] > 6
    people = {person["id"]: person for person in state["staff"]}
    for index, row in enumerate(result["schedule"]):
        person = people[row["staff_id"]]
        assert parse_time(person["shift_start"]) <= parse_time(row["start_at"])
        assert parse_time(row["finish_at"]) <= parse_time(person["shift_end"])
        for other in result["schedule"][index + 1:]:
            if row["staff_id"] == other["staff_id"] or row["station"] == other["station"]:
                assert row["finish_at"] <= other["start_at"] or other["finish_at"] <= row["start_at"]
    # Same demand, radically different policies: all modeled units are accounted for.
    limited = simulate(state, {"action": "replace_limits", "limits": {menu["id"]: 0 for menu in state["menus"]}}, horizon_minutes=120)
    total = lambda data: sum(data["metrics"][key] for key in ("completed_units", "pending_units", "rejected_units"))
    assert total(result) == total(limited)
    assert limited["metrics"]["completed_units"] == 6  # Existing orders remain accepted.


def test_preparing_assignment_and_remaining_time_are_pinned():
    state = seed_state()
    state["business_now"] = iso(parse_time(state["business_now"]) + timedelta(seconds=30))
    task = state["orders"][0]["tasks"][0]
    task.update(status="preparing", staff_id="aoi", started_at=state["staff"][0]["shift_start"], duration_seconds=120)
    state["orders"][0]["status"] = "preparing"
    result = simulate(state, KEEP, include_arrivals=False)
    pinned = next(row for row in result["schedule"] if row["task_id"] == task["id"])
    assert pinned["staff_id"] == "aoi"
    assert pinned["start_at"] == task["started_at"]
    assert parse_time(pinned["finish_at"]) - parse_time(task["started_at"]) == timedelta(seconds=120)
    assert result["metrics"]["existing_at_risk"] == 0
    for row in result["schedule"]:
        if row != pinned and (row["staff_id"] == "aoi" or row["station"] == "bar"):
            assert row["start_at"] >= pinned["finish_at"]


def test_existing_reservations_are_not_subtracted_twice_and_use_order_price():
    state = seed_state()
    for item in state["inventory"]:
        item["on_hand"] = item["reserved"]
    for menu in state["menus"]:
        menu["price"] = 1
    result = simulate(state, KEEP, include_arrivals=False)
    assert result["metrics"]["completed_units"] == 6
    assert result["metrics"]["revenue"] == sum(order["total"] for order in state["orders"])
    assert result["feasible"]
    # Real shortages cannot be hidden by previously recorded reservations.
    next(item for item in state["inventory"] if item["id"] == "bread")["on_hand"] = 0
    result = simulate(state, KEEP, include_arrivals=False)
    assert result["metrics"]["existing_at_risk"] == 3
    assert not result["feasible"]


def test_expiry_at_finish_and_unreceived_deliveries_do_not_fund_new_orders():
    state = empty_state()
    for item in state["inventory"]:
        item["on_hand"] = 0
    state["deliveries"] = [{"id": f"incoming-{item['id']}", "ingredient_id": item["id"], "quantity": 100000,
                            "arrives_at": state["business_now"], "confirmed": True, "received": False}
                           for item in state["inventory"]]
    result = simulate(state, KEEP)
    assert result["metrics"]["completed_units"] == 0
    assert result["metrics"]["rejected_units"] > 0
    state = seed_state()
    for item in state["inventory"]:
        item["expires_at"] = iso(parse_time(state["business_now"]) + timedelta(seconds=30))
    result = simulate(state, KEEP, include_arrivals=False)
    assert result["metrics"]["existing_at_risk"] == 6
    assert result["schedule"] == []


def test_review_time_does_not_reset_used_limits():
    state = empty_state()
    state["mode"].update(limits={menu["id"]: 2 for menu in state["menus"]},
                         used={menu["id"]: 2 for menu in state["menus"]}, review_at=state["business_now"])
    result = simulate(state, KEEP)
    assert result["metrics"]["completed_units"] == 0
    assert result["metrics"]["rejected_units"] > 0
    assert any("見直し時刻" in note for note in result["notes"])
    fresh = simulate(state, {"action": "replace_limits", "limits": {menu["id"]: 2 for menu in state["menus"]}})
    assert fresh["metrics"]["completed_units"] > 0


def test_missing_staff_and_horizon_backlog_are_explicit():
    state = seed_state()
    result = simulate(state, KEEP, horizon_minutes=1, include_arrivals=False)
    assert result["metrics"]["pending_units"] == 6
    assert result["metrics"]["max_wait_minutes"] == 4
    assert result["metrics"]["completed_orders"] == 0
    for person in state["staff"]:
        person["absent"] = True
    result = simulate(state, KEEP, include_arrivals=False)
    assert result["metrics"]["pending_units"] == 6
    assert result["metrics"]["existing_at_risk"] == 6
    assert not result["feasible"]
    # UUID lexical order must not let a newly accepted order jump an older one
    # when both arrived at the same paused demo-clock timestamp.
    state = seed_state()
    state["orders"] = [deepcopy(state["orders"][0]), deepcopy(state["orders"][0])]
    for index, order in enumerate(state["orders"]):
        order.update(id=["z-old", "a-new"][index], created_at=state["business_now"])
        order["tasks"] = [order["tasks"][0]]
        order["tasks"][0]["id"] = f"task-{index}"
    result = simulate(state, KEEP, include_arrivals=False)
    assert result["schedule"][0]["task_id"] == "task-0"


def test_revenue_is_recognized_only_after_the_complete_order_is_delivered():
    state = seed_state()
    state["orders"] = [state["orders"][0]]  # Latte: 120 seconds; sandwich: 180.
    result = simulate(state, KEEP, horizon_minutes=2, include_arrivals=False)
    assert result["metrics"]["completed_units"] == 1
    assert result["metrics"]["completed_orders"] == 0
    assert result["metrics"]["revenue"] == 0
    # One item is already prepared, but none of the order's revenue was delivered.
    task = state["orders"][0]["tasks"][0]
    task["status"] = "completed"
    for entry in state["inventory"]:
        amount = task["allocations"].get(entry["id"], 0)
        entry["reserved"] -= amount
        entry["on_hand"] -= amount
    for menu in state["menus"]:
        menu["price"] = 1
    result = simulate(state, KEEP, horizon_minutes=3, include_arrivals=False)
    assert result["metrics"]["completed_units"] == 1
    assert result["metrics"]["completed_orders"] == 1
    assert result["metrics"]["revenue"] == 580 + 780
