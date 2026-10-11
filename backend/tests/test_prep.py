from copy import deepcopy
from datetime import timedelta

from app.planning.forecast import forecast_sales
from app.planning.prep import calculate_prep
from app.seed import seed_state
from app.timeutils import iso, parse_time


def test_restricted_history_is_not_evidence_of_low_demand():
    state = seed_state()
    expected, _ = forecast_sales(state)
    state["history"][0]["restricted"] = True
    state["history"][0]["counts"] = {menu["id"]: 0 for menu in state["menus"]}
    actual, notes = forecast_sales(state)
    assert abs(actual["latte"] - expected["latte"]) <= 1
    assert any("除外" in note for note in notes)
    for row in state["history"]:
        row["restricted"] = True
    state["store"]["default_forecast"] = {menu["id"]: 9 for menu in state["menus"]}
    fallback, notes = forecast_sales(state)
    assert set(fallback.values()) == {9}
    assert any("標本" in note for note in notes)


def test_prep_uses_valid_unreserved_stock_and_confirmed_on_time_deliveries():
    state = seed_state()
    tomorrow = "2026-10-12"
    milk = next(item for item in state["inventory"] if item["id"] == "milk")
    milk.update(on_hand=1000, reserved=200, buffer=500)
    state["deliveries"] += [
        {"id": "late", "ingredient_id": "milk", "quantity": 100000, "arrives_at": "2026-10-12T03:00:00Z", "confirmed": True, "received": False},
        {"id": "uncertain", "ingredient_id": "milk", "quantity": 100000, "arrives_at": state["business_now"], "confirmed": False, "received": False},
        {"id": "already", "ingredient_id": "milk", "quantity": 100000, "arrives_at": state["business_now"], "confirmed": True, "received": True},
    ]
    before = deepcopy(state)
    result = calculate_prep(state, tomorrow)
    item = next(entry for entry in result["items"] if entry["ingredient_id"] == "milk")
    assert item["available"] == 800
    assert item["incoming"] == 2000
    assert item["to_order"] == max(0, item["needed"] + 500 - 800 - 2000)
    assert state == before
    milk["expires_at"] = "2026-10-12T02:30:00Z"
    assert next(entry for entry in calculate_prep(state, tomorrow)["items"] if entry["ingredient_id"] == "milk")["available"] == 0


def test_target_day_staff_changes_possible_preparation_not_demand():
    state = seed_state()
    baseline = calculate_prep(state, "2026-10-12")
    for person in state["next_day_staff"]:
        person["absent"] = True
    no_staff = calculate_prep(state, "2026-10-12")
    assert baseline["forecast"] == no_staff["forecast"]
    assert sum(baseline["planned"].values()) > 0
    assert sum(no_staff["planned"].values()) == 0
    assert all(entry["needed"] == 0 for entry in no_staff["items"])
    assert any("準備できます" in note for note in no_staff["notes"])


def test_prep_defaults_to_today_and_does_not_shift_staff_to_an_unregistered_date():
    state = seed_state()
    assert calculate_prep(state)["service_date"] == state["store"]["service_date"]
    result = calculate_prep(state, "2026-10-13")
    assert sum(result["planned"].values()) == 0
    assert sum(result["forecast"].values()) > 0
