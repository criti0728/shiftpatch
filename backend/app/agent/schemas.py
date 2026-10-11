from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyArgs(StrictModel):
    pass


class DateArgs(StrictModel):
    service_date: str | None = None


class StaffArgs(StrictModel):
    staff_ids: list[str] = Field(default_factory=list, max_length=50)
    menu_ids: list[str] = Field(default_factory=list, max_length=50)


class InventoryArgs(StrictModel):
    ingredient_ids: list[str] = Field(default_factory=list, max_length=100)


class QueueArgs(StrictModel):
    statuses: list[Literal["accepted", "preparing", "completed"]] = Field(
        default_factory=lambda: ["accepted", "preparing"], max_length=3)


class CandidateSpec(StrictModel):
    title: str = Field(min_length=1, max_length=100)
    action: Literal["replace_limits"] = "replace_limits"
    limits: dict[str, StrictInt]
    review_after_minutes: Literal[15, 30] = 30
    parent_candidate_id: str | None = None
    based_on_result_ids: list[str] = Field(min_length=1, max_length=12)


class SimulateArgs(StrictModel):
    candidate_spec: CandidateSpec


class PrepArgs(StrictModel):
    sales_basis_result_id: str


class CompareArgs(StrictModel):
    close_result_id: str


class NextDayArgs(StrictModel):
    sales_basis_result_id: str
    comparison_result_id: str


class CandidateNote(StrictModel):
    candidate_id: str
    summary: str = Field(min_length=1, max_length=500)
    evidence_refs: list[str] = Field(min_length=1, max_length=12)
    tradeoff: str = Field(min_length=1, max_length=500)


class FinalAnswer(StrictModel):
    outcome: Literal["ready", "needs_input", "no_feasible_plan"]
    summary: str = Field(min_length=1, max_length=1000)
    recommended_candidate_id: str | None = None
    candidate_notes: list[CandidateNote] = Field(default_factory=list, max_length=5)
    evidence_refs: list[str] = Field(default_factory=list, max_length=12)
    questions: list[str] = Field(default_factory=list, max_length=5)
    plan_result_id: str | None = None
    sales_basis_result_id: str | None = None
    comparison_result_id: str | None = None


TOOL_SCHEMAS = {
    "read_shift": (DateArgs, "Read confirmed shifts/absence for the target service date."),
    "read_staff_menu_times": (StaffArgs, "Read menu preparation times for selected staff and menus."),
    "read_inventory": (InventoryArgs, "Read physical and reserved inventory, expiry and confirmed deliveries."),
    "read_queue": (QueueArgs, "Read existing orders and staff/station occupancy; existing commitments must be preserved."),
    "read_current_mode": (EmptyArgs, "Read current limits, amounts already used and review time."),
    "read_menu_recipes": (EmptyArgs, "Read menu categories, prices, stations and ingredient recipes."),
    "estimate_sales": (DateArgs, "Calculate demand basis; sales restricted by service settings are not normal demand."),
    "simulate_service_plan": (SimulateArgs, "Calculate a candidate. Supply every menu limit (0..100), and refer to observed result IDs. Revise using parent_candidate_id after seeing results."),
    "calculate_prep": (PrepArgs, "Calculate today's preparation, referring to this run's estimate_sales result."),
    "read_close_record": (EmptyArgs, "Read this service day's confirmed close record."),
    "compare_plan_actual": (CompareArgs, "Compare completed sales and forecast using the observed close result."),
    "calculate_next_day_prep": (NextDayArgs, "Calculate next-day preparation using this run's sales basis and close comparison."),
}


def allowed_tools(mode: str) -> dict:
    common = {"read_shift", "read_staff_menu_times", "read_inventory", "read_menu_recipes", "estimate_sales"}
    extra = {
        "in_service": {"read_queue", "read_current_mode", "simulate_service_plan"},
        "pre_open": {"calculate_prep"},
        "post_close": {"read_close_record", "compare_plan_actual", "calculate_next_day_prep"},
    }[mode]
    return {name: entry for name, entry in TOOL_SCHEMAS.items() if name in common | extra}


def declarations(mode: str) -> list[dict]:
    # Pydantic references are expanded before sending tool JSON schemas to Gemini.
    result = []
    for name, (model, description) in allowed_tools(mode).items():
        schema = model.model_json_schema()
        defs = schema.pop("$defs", {})

        def expand(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    return expand(defs[value["$ref"].split("/")[-1]])
                # Preserve a property named "title"; strip only schema title metadata.
                return {key: expand(item) for key, item in value.items()
                        if key != "title" or isinstance(item, dict)}
            if isinstance(value, list):
                return [expand(item) for item in value]
            return value

        result.append({"name": name, "description": description, "parameters_json_schema": expand(schema)})
    return result
