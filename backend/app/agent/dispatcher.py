"""Allowlisted read/calculation tools over a private copy of a single snapshot."""
from copy import deepcopy
from datetime import date, timedelta
import json
from time import monotonic

from pydantic import ValidationError

from .schemas import allowed_tools


class AgentError(Exception):
    pass


class BoundaryViolation(AgentError):
    pass


class BudgetExceeded(AgentError):
    pass


class Dispatcher:
    def __init__(self, snapshot, mode, trace, *, max_tools=12, max_candidates=5):
        self.snapshot = deepcopy(snapshot)
        self.mode = mode
        self.trace = trace
        self.started = monotonic()
        self.tools = allowed_tools(mode)
        self.results: dict[str, dict] = {}
        self.candidates: list[dict] = []
        self.cache: dict[str, dict] = {}
        self.tool_count = 0
        self.max_tools = max_tools
        self.max_candidates = max_candidates
        self.date = snapshot["store"]["service_date"]
        if mode == "post_close":
            self.date = (date.fromisoformat(self.date) + timedelta(days=1)).isoformat()

    def record(self, name, args, data, origin="model", *, cached=False):
        result_id = f"result-{len(self.results) + 1}"
        result = {"result_id": result_id, "tool": name, "data": deepcopy(data)}
        self.results[result_id] = result
        if isinstance(data, dict) and "metrics" in data:
            summary = json.dumps(data["metrics"], ensure_ascii=False)
        elif isinstance(data, dict) and "error" in data:
            summary = data["error"]
        else:
            summary = f"{name} の固定スナップショット結果を取得しました。"
        self.trace.append({
            "step": len(self.trace) + 1, "origin": origin, "tool": name,
            "args": deepcopy(args), "result_id": result_id, "result_summary": summary,
            "elapsed_ms": round((monotonic() - self.started) * 1000), "cached": cached,
        })
        return result

    def require_result(self, result_id, tool=None):
        result = self.results.get(result_id)
        if result is None or (tool is not None and result["tool"] != tool) or "error" in result["data"]:
            raise ValueError(f"この実行に有効な参照結果がありません: {result_id}")
        return result["data"]

    def check_ids(self, ids, key):
        allowed = {item["id"] for item in self.snapshot[key]}
        if not set(ids) <= allowed:
            raise ValueError(f"スナップショットに存在しないID: {sorted(set(ids) - allowed)}")

    def candidate(self, spec, origin="model"):
        from app.simulation.engine import simulate
        spec = deepcopy(spec)
        if spec["action"] == "replace_limits":
            menu_ids = {menu["id"] for menu in self.snapshot["menus"]}
            if set(spec["limits"]) != menu_ids:
                raise ValueError("全メニューの上限を明示してください。")
            if any(type(value) is not int or value < 0 or value > 100 for value in spec["limits"].values()):
                raise ValueError("メニュー上限は0〜100の整数で指定してください。")
        parent = spec.get("parent_candidate_id")
        if parent and parent not in {item["id"] for item in self.candidates}:
            raise ValueError("修正元の候補がこの実行に存在しません。")
        for result_id in spec.get("based_on_result_ids", []):
            self.require_result(result_id)
        if parent:
            parent_result_ids = {key for key, result in self.results.items()
                                 if result["data"].get("id") == parent}
            if not parent_result_ids.intersection(spec.get("based_on_result_ids", [])):
                raise ValueError("修正候補は修正元の計算結果を参照してください。")
        for candidate in self.candidates:
            if all(candidate[key] == spec[key] for key in ("action", "limits", "review_after_minutes")):
                return candidate
        if len(self.candidates) >= self.max_candidates:
            raise BudgetExceeded("比較候補の上限に達しました。")
        candidate = {"id": f"candidate-{len(self.candidates) + 1}", **spec, "origin": origin}
        # Separate copy keeps accidental calculator mutations away from the snapshot.
        calculated = simulate(deepcopy(self.snapshot), deepcopy(candidate), horizon_minutes=30, seed=42)
        candidate.update(metrics=calculated["metrics"], feasible=calculated["feasible"], notes=calculated["notes"])
        self.candidates.append(candidate)
        return candidate

    def baseline(self):
        candidate = self.candidate({"title": "現在の運営を維持", "action": "keep_current", "limits": {},
                                    "review_after_minutes": 30}, "server")
        return self.record("baseline_simulation", {}, candidate, "server")

    def execute(self, name, args):
        if name not in self.tools:
            self.record(name, {}, {"error": "許可されていないツールです。"}, "model")
            raise BoundaryViolation("許可されていないツールへのアクセスを拒否しました。")
        if not isinstance(args, dict):
            raise BoundaryViolation("ツール引数はobjectである必要があります。")
        # Routing, actor and snapshot identity are never provided by the model.
        forbidden = {"store_id", "snapshot", "snapshot_id", "actor_id", "role", "seed", "sql", "code"}
        if forbidden.intersection(args):
            self.record(name, {}, {"error": "権限・スナップショット引数を指定することはできません。"})
            raise BoundaryViolation("権限・スナップショット引数を拒否しました。")
        key = json.dumps([name, args], sort_keys=True, ensure_ascii=False)
        if len(key) > 12_000:
            raise BoundaryViolation("ツール引数のサイズ上限を超えました。")
        if key in self.cache:
            original = self.cache[key]
            self.trace.append({"step": len(self.trace) + 1, "origin": "model", "tool": name,
                               "args": deepcopy(args), "result_id": original["result_id"],
                               "result_summary": "同じスナップショットの計算済み結果を再利用しました。",
                               "elapsed_ms": round((monotonic() - self.started) * 1000), "cached": True})
            return deepcopy(original)
        if self.tool_count >= self.max_tools:
            raise BudgetExceeded("ツール実行回数の上限に達しました。")
        self.tool_count += 1
        try:
            parsed = self.tools[name][0].model_validate(args).model_dump()
            data = self._invoke(name, parsed)
            if len(json.dumps(data, ensure_ascii=False)) > 48_000:
                raise ValueError("結果が大きすぎます。対象のフィルターを絞ってください。")
        except (ValidationError, ValueError, KeyError) as exc:
            # Invalid business candidates can be repaired based on the returned error.
            data = {"error": str(exc)[:1500], "recoverable": True}
        result = self.record(name, args, data)
        self.cache[key] = result
        return deepcopy(result)

    def _invoke(self, name, args):
        state = self.snapshot
        if name in {"read_shift", "estimate_sales"}:
            if args["service_date"] is not None and args["service_date"] != self.date:
                raise ValueError("この実行の対象営業日以外は参照できません。")
        staff = state.get("next_day_staff", []) if self.mode == "post_close" else state["staff"]
        if name == "read_shift":
            return {"service_date": self.date, "staff": [
                {key: value for key, value in person.items() if key != "skills"} for person in staff]}
        if name == "read_staff_menu_times":
            if not set(args["staff_ids"]) <= {person["id"] for person in staff}:
                raise ValueError("対象日のスタッフに存在しないIDです。")
            self.check_ids(args["menu_ids"], "menus")
            return {"service_date": self.date, "staff": [
                {"id": person["id"], "name": person["name"], "absent": person["absent"],
                 "skills": {key: value for key, value in person.get("skills", {}).items()
                            if not args["menu_ids"] or key in args["menu_ids"]}}
                for person in staff if not args["staff_ids"] or person["id"] in args["staff_ids"]]}
        if name == "read_inventory":
            self.check_ids(args["ingredient_ids"], "inventory")
            return {"inventory": [entry for entry in state["inventory"]
                                  if not args["ingredient_ids"] or entry["id"] in args["ingredient_ids"]],
                    "deliveries": [delivery for delivery in state["deliveries"]
                                   if not args["ingredient_ids"] or delivery["ingredient_id"] in args["ingredient_ids"]]}
        if name == "read_queue":
            return {"orders": [order for order in state["orders"] if order["status"] in args["statuses"]]}
        if name == "read_current_mode":
            return deepcopy(state["mode"])
        if name == "read_menu_recipes":
            return {"menus": state["menus"]}
        if name == "simulate_service_plan":
            return self.candidate(args["candidate_spec"])
        if name == "estimate_sales":
            from app.planning.forecast import forecast_sales
            forecast, notes = forecast_sales(deepcopy(state), service_date=self.date)
            rows = sorted((item for item in state["history"] if item.get("date", "") < self.date),
                          key=lambda item: item.get("date", ""), reverse=True)[:7]
            return {"service_date": self.date, "forecast": forecast,
                    "sample_count": sum(not item.get("restricted", False) for item in rows),
                    "restricted_sample_count": sum(bool(item.get("restricted", False)) for item in rows),
                    "notes": notes}
        if name == "read_close_record":
            closes = [item for item in state.get("closes", [])
                      if item.get("service_date", item.get("date")) == state["store"]["service_date"]]
            if not closes:
                raise ValueError("当日の確定締め記録がありません。先に締めを確定してください。")
            return deepcopy(max(closes, key=lambda item: item.get("closed_at", "")))
        if name == "compare_plan_actual":
            close = self.require_result(args["close_result_id"], "read_close_record")
            # A fallback recomputed at close is not an earlier forecast.
            actual = close.get("counts", close.get("completed_counts", {}))
            forecast = close.get("forecast", {})
            forecast_source = close.get("forecast_source", "unknown")
            notes = list(close.get("notes", []))
            notes.append("販売制限があった区間の減少を通常需要の低下と断定しません。")
            if forecast_source != "acknowledged_prep_plan":
                notes.append("比較基準は事前に確認した準備案ではありません。過去の予測精度を検証した結果として扱わないでください。")
            return {"close_result_id": args["close_result_id"], "actual": actual,
                    "forecast": forecast,
                    "forecast_source": forecast_source,
                    "forecast_proposal_id": close.get("forecast_proposal_id"),
                    "errors": {key: actual.get(key, 0) - value for key, value in forecast.items()},
                    "restricted": bool(close.get("restricted", False)),
                    "notes": notes}
        if name in {"calculate_prep", "calculate_next_day_prep"}:
            from app.planning.prep import calculate_prep
            sales = self.require_result(args["sales_basis_result_id"], "estimate_sales")
            if name == "calculate_next_day_prep":
                self.require_result(args["comparison_result_id"], "compare_plan_actual")
            prep = calculate_prep(deepcopy(state), service_date=sales["service_date"])
            if prep["forecast"] != sales["forecast"]:
                raise ValueError("販売基準と準備計算の整合性を確認できません。")
            return {"prep": prep, **args}
        raise BoundaryViolation("ツールは実装されていません。")
