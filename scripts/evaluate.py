#!/usr/bin/env python3
"""Reproducible synthetic service-policy comparisons, using only the stdlib.

Run: python3 scripts/evaluate.py --output test-results/evaluation

This evaluates the deterministic calculator with fixed policies. It does not
call Gemini, validate AI quality, or establish a real revenue improvement.
Every run uses a fresh synthetic seed, never the running application's database.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import csv
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import io
import json
from pathlib import Path
from statistics import mean
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.seed import seed_state  # noqa: E402
from app.simulation.engine import ENGINE_VERSION, simulate  # noqa: E402
from app.timeutils import iso, parse_time  # noqa: E402


DEFAULT_SEEDS = (11, 22, 33, 44, 55)
PREP_FACTORS = (0.7, 1.0, 1.3)
METRICS = (
    "completed_units", "completed_orders", "rejected_units", "pending_units",
    "max_wait_minutes", "revenue", "existing_at_risk",
)
DISCLAIMER = (
    "合成データと固定条件での計算比較です。Geminiは呼び出していません。"
    "AIの判断品質・実店舗の予測精度・売上改善効果を証明する評価ではありません。"
)
CHECK_COUNT = 0


def require(condition: bool, message: str) -> None:
    """Always enforce invariants, including if Python is invoked with -O."""
    global CHECK_COUNT
    CHECK_COUNT += 1
    if not condition:
        raise AssertionError(message)


def digest(value) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def find(items, identifier):
    return next(item for item in items if item["id"] == identifier)


def scenarios() -> list[dict]:
    descriptions = [
        ("normal", "通常営業", "初期の3注文・6作業と通常の需要基準。"),
        ("kitchen_absent", "キッチン担当欠勤", "結衣が欠勤。残るスタッフの作業時間を使用。"),
        ("single_staff", "スタッフ1名", "葵のみ勤務。バーとキッチンを同時に担当できない条件。"),
        ("shift_ends_early", "早いシフト終了", "午前スタッフ3名の勤務は12分後に終了。午後スタッフは元の開始時刻。"),
        ("milk_low", "牛乳残り1杯分", "既存予約に加え180mLのみ。未入荷の予定は現在在庫に含めない。"),
        ("bread_low", "パン残り2枚", "既存予約に加え2枚のみ。複数メニューが在庫を共有。"),
        ("reservation_shortfall", "予約済み材料不足", "パンの実棚が既存予約を2枚下回る条件。既存注文への影響を表示。"),
        ("milk_expiring", "牛乳が8分後に期限", "完了予定時点で期限が過ぎる牛乳は使用不可。"),
        ("delivery_not_received", "牛乳の入荷待ち", "未予約の牛乳は0。5分後に確定入荷予定があるが受領前。"),
        ("demand_surge", "注文増加", "通常販売日のメニュー別数量を2倍にした合成需要。"),
        ("pinned_preparing", "調理中の作業あり", "葵がバーでラテを調理中。担当・開始時刻・120秒の記録は固定。"),
        ("review_overdue", "販売制限の見直し超過", "各メニューの上限2点を消費済み。見直し時刻を過ぎても上限は解除しない。"),
    ]
    result = []
    for identifier, title, description in descriptions:
        state = seed_state()
        now = parse_time(state["business_now"])
        if identifier == "kitchen_absent":
            find(state["staff"], "yui")["absent"] = True
        elif identifier == "single_staff":
            for person in state["staff"]:
                person["absent"] = person["id"] != "aoi"
        elif identifier == "shift_ends_early":
            for person in state["staff"]:
                if person["id"] != "sora":
                    person["shift_end"] = iso(now + timedelta(minutes=12))
        elif identifier in {"milk_low", "bread_low", "reservation_shortfall", "delivery_not_received"}:
            ingredient = find(state["inventory"], "bread" if "bread" in identifier or identifier == "reservation_shortfall" else "milk")
            extra = {"milk_low": 180, "bread_low": 2, "reservation_shortfall": -2, "delivery_not_received": 0}[identifier]
            ingredient["on_hand"] = ingredient["reserved"] + extra
            if identifier == "delivery_not_received":
                state["deliveries"][0]["arrives_at"] = iso(now + timedelta(minutes=5))
        elif identifier == "milk_expiring":
            find(state["inventory"], "milk")["expires_at"] = iso(now + timedelta(minutes=8))
        elif identifier == "demand_surge":
            for row in state["history"]:
                row["counts"] = {key: count * 2 for key, count in row["counts"].items()}
        elif identifier == "pinned_preparing":
            order = state["orders"][0]
            order["status"] = "preparing"
            order["tasks"][0].update(status="preparing", staff_id="aoi", started_at=state["business_now"], duration_seconds=120)
        elif identifier == "review_overdue":
            state["mode"].update(limits={menu["id"]: 2 for menu in state["menus"]},
                                 used={menu["id"]: 2 for menu in state["menus"]},
                                 review_at=iso(now - timedelta(minutes=1)), status="review_required")
        result.append({"id": identifier, "title": title, "description": description, "state": state})
    return result


def policies(state: dict) -> list[dict]:
    return [
        {"id": "current", "title": "現在設定を維持", "action": "keep_current", "limits": {}, "review_after_minutes": 30},
        {"id": "food_limited", "title": "フード少量受付", "action": "replace_limits",
         "limits": {menu["id"]: 20 if menu["category"] == "drink" else 2 for menu in state["menus"]}, "review_after_minutes": 30},
        {"id": "all_limited", "title": "全メニュー少量受付", "action": "replace_limits",
         "limits": {menu["id"]: 5 if menu["category"] == "drink" else 1 for menu in state["menus"]}, "review_after_minutes": 30},
    ]


def scaled_state(source: dict, factor: float) -> dict:
    state = deepcopy(source)
    for person in state["staff"]:
        person["skills"] = {key: max(1, int(seconds * factor + 0.5)) for key, seconds in person["skills"].items()}
    # Already preparing work is an observed commitment: never change its recorded
    # duration, assigned staff or start time during sensitivity comparisons.
    return state


def validate_result(state: dict, candidate: dict, result: dict, scenario_id: str, horizon: int) -> None:
    """Check resource, inventory, commitment and metric invariants independently."""
    schedule = result["schedule"]
    metrics = result["metrics"]
    menus = {menu["id"]: menu for menu in state["menus"]}
    staff = {person["id"]: person for person in state["staff"]}
    inventory = {entry["id"]: entry for entry in state["inventory"]}
    tasks = {task["id"]: task for order in state["orders"] for task in order["tasks"] if task["status"] != "completed"}
    timeline = defaultdict(list)
    existing_stock, new_stock, new_menu_counts = Counter(), Counter(), Counter()
    scheduled_ids = set()
    end = parse_time(state["business_now"]) + timedelta(minutes=horizon)
    scheduled_complete = 0
    for row in schedule:
        start, finish = parse_time(row["start_at"]), parse_time(row["finish_at"])
        person = staff[row["staff_id"]]
        require(row["task_id"] not in scheduled_ids, "A task was scheduled twice")
        scheduled_ids.add(row["task_id"])
        require(not person["absent"] and person["skills"].get(row["menu_id"], 0) > 0, "Ineligible staff assignment")
        require(parse_time(person["shift_start"]) <= start < finish <= parse_time(person["shift_end"]), "Task outside staff shift")
        task = tasks.get(row["task_id"])
        require(row["existing"] == (task is not None), "Existing task identity changed")
        if task and task["status"] == "preparing":
            require(row["staff_id"] == task["staff_id"] and row["start_at"] == task["started_at"], "Pinned assignment changed")
            duration = task["duration_seconds"]
        else:
            duration = person["skills"][row["menu_id"]]
            require(start >= parse_time(state["business_now"]), "A new assignment starts in the past")
        require((finish - start).total_seconds() == duration, "Task duration differs from its recorded assumptions")
        timeline[("staff", row["staff_id"])].append((start, finish))
        timeline[("station", row["station"])].append((start, finish))
        require(row["station"] == (task["station"] if task else menus[row["menu_id"]]["station"]), "Wrong task station")
        recipe = task["allocations"] if task else menus[row["menu_id"]]["recipe"]
        quantities = existing_stock if task else new_stock
        for key, amount in recipe.items():
            require(finish < parse_time(inventory[key]["expires_at"]), "Expired inventory was scheduled")
            quantities[key] += amount
        if task is None:
            new_menu_counts[row["menu_id"]] += 1
        scheduled_complete += finish <= end
    for intervals in timeline.values():
        intervals.sort()
        for previous, current in zip(intervals, intervals[1:]):
            require(previous[1] <= current[0], "Staff or station assigned overlapping tasks")
    for key, entry in inventory.items():
        require(existing_stock[key] <= max(0, min(entry["on_hand"], entry["reserved"])), "Existing reservations overdrawn")
        require(new_stock[key] <= max(0, entry["on_hand"] - entry["reserved"]), "New work consumed reserved or unreceived stock")
    limits = state["mode"]["limits"] if candidate["action"] == "keep_current" else candidate["limits"]
    used = state["mode"]["used"] if candidate["action"] == "keep_current" else {}
    if limits is not None:
        for key, count in new_menu_counts.items():
            require(count <= max(0, limits.get(key, 0) - used.get(key, 0)), "Sales limit exceeded or reset without replacement")
    for key in METRICS:
        require(metrics[key] >= 0, f"Negative metric: {key}")
    require(metrics["completed_units"] == scheduled_complete, "Completed unit accounting mismatch")
    missing_existing = set(tasks) - scheduled_ids
    require(metrics["existing_at_risk"] == len(missing_existing), "Missing existing work was hidden")
    require(result["feasible"] == (not missing_existing), "Infeasible existing work was declared feasible")
    require(metrics["completed_units"] + metrics["pending_units"] == len(schedule) + len(missing_existing), "Accepted unit accounting mismatch")
    if scenario_id == "reservation_shortfall":
        require(metrics["existing_at_risk"] > 0, "Reserved-stock shortfall was not surfaced")
    if scenario_id == "delivery_not_received":
        require(new_menu_counts["latte"] + new_menu_counts["matcha"] == 0, "Future delivery became usable without receipt")
    if scenario_id == "review_overdue" and candidate["id"] == "current":
        require(sum(new_menu_counts.values()) == 0, "Review deadline silently reopened exhausted limits")


def summarize(rows: list[dict]) -> list[dict]:
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["scenario_id"], row["policy_id"], row["prep_factor"])].append(row)
    return [
        {"scenario_id": scenario, "policy_id": policy, "prep_factor": factor,
         "runs": len(group), "feasible_runs": sum(row["feasible"] for row in group),
         "mean": {key: round(mean(row["metrics"][key] for row in group), 3) for key in METRICS},
         "min": {key: min(row["metrics"][key] for row in group) for key in METRICS},
         "max": {key: max(row["metrics"][key] for row in group) for key in METRICS}}
        for (scenario, policy, factor), group in grouped.items()
    ]


def run_evaluation(seeds: tuple[int, ...], horizon: int) -> dict:
    global CHECK_COUNT
    CHECK_COUNT = 0
    cases = scenarios()
    rows, repeat_count = [], 0
    normal_seed_signatures = set()
    for case in cases:
        for factor in PREP_FACTORS:
            state = scaled_state(case["state"], factor)
            input_digest = digest(state)
            for seed in seeds:
                observed_total = None
                baseline_metrics = None
                for policy in policies(state):
                    policy_before = deepcopy(policy)
                    result = simulate(state, policy, horizon_minutes=horizon, seed=seed)
                    require(digest(state) == input_digest and policy == policy_before, "Simulation mutated its input")
                    validate_result(state, policy, result, case["id"], horizon)
                    total = sum(result["metrics"][key] for key in ("completed_units", "pending_units", "rejected_units"))
                    if policy["id"] == "current":
                        # Repeat an entire baseline execution for every paired trial.
                        require(result == simulate(state, policy, horizon_minutes=horizon, seed=seed), "Identical inputs were not deterministic")
                        repeat_count += 1
                        baseline_metrics = result["metrics"]
                        observed_total = total
                    else:
                        require(total == observed_total, "Policies were compared against different demand quantities")
                    schedule_digest = digest(result["schedule"])
                    if case["id"] == "normal" and factor == 1.0 and policy["id"] == "current":
                        normal_seed_signatures.add(schedule_digest)
                    rows.append({
                        "scenario_id": case["id"], "policy_id": policy["id"], "prep_factor": factor,
                        "seed": seed, "horizon_minutes": horizon, "input_sha256": input_digest,
                        "schedule_sha256": schedule_digest, "feasible": result["feasible"],
                        "metrics": result["metrics"],
                        "delta_from_current": {key: round(result["metrics"][key] - baseline_metrics[key], 3) for key in METRICS},
                        "notes": result["notes"], "schedule": result["schedule"],
                    })
    require(len(normal_seed_signatures) > 1, "Changing the seed had no observable effect on normal-service schedules")
    require(len(rows) == 12 * len(seeds) * 3 * 3, "Incomplete evaluation matrix")
    sources = ["backend/app/seed.py", "backend/app/planning/forecast.py", "backend/app/simulation/engine.py", "scripts/evaluate.py"]
    return {
        "schema_version": 1, "engine_version": ENGINE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(), "disclaimer": DISCLAIMER,
        "configuration": {"seeds": list(seeds), "horizon_minutes": horizon, "prep_factors": list(PREP_FACTORS),
                          "policy_count": 3, "scenario_count": 12, "stored_runs": len(rows),
                          "repeat_checks": repeat_count, "database_accessed": False, "gemini_called": False},
        "checks": {"passed": True, "invariant_checks": CHECK_COUNT,
                   "seed_has_observable_effect": True, "normal_unique_schedule_count": len(normal_seed_signatures)},
        "source_sha256": {path: sha256((ROOT / path).read_bytes()).hexdigest() for path in sources},
        "scenarios": cases, "policies": policies(cases[0]["state"]),
        "summary": summarize(rows), "results": rows,
    }


def markdown_report(evaluation: dict) -> str:
    config = evaluation["configuration"]
    lines = [
        "# ShiftPatch ローカル計算評価", "", DISCLAIMER, "",
        f"12状況 × 5シード × 3方針 × 3準備時間条件 = **{config['stored_runs']}件**。",
        f"各試行の計算区間は{config['horizon_minutes']}分。シード: {', '.join(map(str, config['seeds']))}。",
        "現在設定、フード少量受付、全メニュー少量受付はコードで固定した比較案です。AI提案ではありません。", "",
        f"不変条件チェック {evaluation['checks']['invariant_checks']:,}件、同一入力再実行 {config['repeat_checks']}件を通過。",
        f"通常条件では5シードから{evaluation['checks']['normal_unique_schedule_count']}種類の予定が生成され、シードの影響も確認しています。", "",
        "準備時間は0.7倍・1.0倍・1.3倍で比較。調理中の確定担当・開始時刻・記録済み所要時間は変更していません。",
        "待ち時間は未完了分の経過時間も含み、売上相当額は注文全体の提供完了時に計上します。", "",
        "## 通常の準備時間での比較", "",
        "数値は5シードの算術平均です。単純な勝敗・順位にはせず、完了・拒否・未完了を併記します。", "",
        "| 状況 | 方針 | 完了点 | 拒否点 | 未完了点 | 最大待ち分 | 売上相当円 | 既存リスク点 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    titles = {case["id"]: case["title"] for case in evaluation["scenarios"]}
    policy_titles = {policy["id"]: policy["title"] for policy in evaluation["policies"]}
    for summary in evaluation["summary"]:
        if summary["prep_factor"] != 1.0:
            continue
        values = summary["mean"]
        cells = [titles[summary["scenario_id"]], policy_titles[summary["policy_id"]]]
        cells.extend(f"{values[key]:.1f}" for key in ("completed_units", "rejected_units", "pending_units", "max_wait_minutes", "revenue", "existing_at_risk"))
        lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "", "## 検証範囲と出力", "",
        "- スタッフ・作業台の重複、勤務外作業、期限切れ材料、既存予約の横取り、未受領入荷の使用を検査。",
        "- 見直し時刻後の自動制限解除、既存注文の欠落、入力変更、候補間の需要総数不一致を検査。",
        "- `results.json`: 全入力状況、方針、実行別予定、集計、ソースのハッシュ。",
        "- `results.csv`: 全540件の指標と、同一状況・シード・時間条件の現在設定との差分。",
        "- ±30%条件の個別結果と最小・最大・平均はJSON/CSVを参照。誤差分布や信頼区間ではありません。", "",
        "実店舗データ、Geminiのツール選択、候補修正、説明の正確さ、承認操作の評価は別途必要です。",
        "このCLIは独立した `seed_state()` を使い、起動中のアプリ・SQLite・Google Cloudに接続しません。", "",
    ]
    return "\n".join(lines)


def write_outputs(evaluation: dict, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    columns = ["scenario_id", "policy_id", "prep_factor", "seed", "horizon_minutes", "feasible", *METRICS,
               *("delta_" + key for key in METRICS), "input_sha256", "schedule_sha256"]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    for row in evaluation["results"]:
        flat = {key: row[key] for key in columns if key in row}
        flat.update(row["metrics"])
        flat.update({"delta_" + key: value for key, value in row["delta_from_current"].items()})
        writer.writerow(flat)
    payloads = {
        "results.json": json.dumps(evaluation, ensure_ascii=False, indent=2) + "\n",
        "results.csv": buffer.getvalue(),
        "summary.md": markdown_report(evaluation),
    }
    for name, contents in payloads.items():
        temporary = output / (name + ".tmp")
        temporary.write_text(contents, encoding="utf-8")
        temporary.replace(output / name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=ROOT / "test-results" / "evaluation", help="Output directory for JSON, CSV and Markdown")
    parser.add_argument("--seeds", type=int, nargs=5, default=DEFAULT_SEEDS, metavar="SEED", help="Five distinct arrival seeds (default: 11 22 33 44 55)")
    parser.add_argument("--horizon-minutes", type=int, default=30, help="Common evaluation horizon, 1–120 minutes (default: 30)")
    args = parser.parse_args()
    if len(set(args.seeds)) != 5:
        parser.error("--seeds must contain five distinct values")
    if not 1 <= args.horizon_minutes <= 120:
        parser.error("--horizon-minutes must be between 1 and 120")
    evaluation = run_evaluation(tuple(args.seeds), args.horizon_minutes)
    write_outputs(evaluation, args.output.resolve())
    print(f"PASS: {len(evaluation['results'])} synthetic comparisons; {evaluation['checks']['invariant_checks']} invariant checks; {evaluation['configuration']['repeat_checks']} repeat checks.")
    print(DISCLAIMER)
    print(f"Results: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
