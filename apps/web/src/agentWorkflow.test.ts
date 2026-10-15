import { expect, it } from "vitest";
import { analysisBlockReason, continuedProposal, entryAnalysisMode, pendingQuestionProposal, proposalErrorMessage, questionFactUpdated } from "./agentWorkflow";
import type { Ingredient, Overview, Proposal, Staff } from "./types";

function state(now = "2026-10-11T11:00:00+09:00", closed = false) {
  return {
    store: { id: "sample", name: "Cafe", service_date: "2026-10-11", open_time: "11:00", close_time: "13:00", timezone: "Asia/Tokyo" },
    business_now: now,
    closes: closed ? [{ id: "close", service_date: "2026-10-11" }] : [],
  } satisfies Pick<Overview, "store" | "business_now" | "closes">;
}
it("routes the entry to preparation before opening and next-day analysis after closing", () => {
  expect(entryAnalysisMode(state("2026-10-11T10:30:00+09:00"), "service")).toBe("pre_open");
  expect(entryAnalysisMode(state(), "service")).toBe("in_service");
  expect(entryAnalysisMode(state(undefined, true), "service")).toBe("post_close");
});
it("blocks incompatible service and next-day analyses", () => {
  expect(analysisBlockReason(state(), "post_close")).toContain("締める");
  expect(analysisBlockReason(state(undefined, true), "in_service")).toContain("締め済み");
  expect(analysisBlockReason(state("2026-10-11T13:00:00+09:00"), "in_service")).toContain("終了");
  expect(analysisBlockReason(state("2026-10-12T11:00:00+09:00"), "in_service")).toContain("終了");
  expect(analysisBlockReason(state(undefined, true), "post_close")).toBe("");
});
it("uses the store timezone rather than the computer's date and time", () => {
  expect(analysisBlockReason(state("2026-10-11T02:00:00Z"), "in_service")).toBe("");
  expect(analysisBlockReason(state("2026-10-11T04:00:00Z"), "in_service")).toContain("終了");
});
it("keeps unanswered questions available after a failed continuation", () => {
  const parent = { id: "parent", run_id: "parent-run", status: "waiting_input" } as Proposal;
  const failed = { id: "failed", run_id: "failed-run", parent_run_id: "parent-run", status: "failed" } as Proposal;
  expect(pendingQuestionProposal([failed, parent])).toBe(parent);
  expect(pendingQuestionProposal([{ ...failed, status: "ready" }, parent])).toBeUndefined();
  expect(pendingQuestionProposal([{ id: "old", status: "waiting_input" } as Proposal, parent])).toBeDefined();
  const retried = { id: "retry", parent_run_id: "failed-run", status: "ready" } as Proposal;
  expect(pendingQuestionProposal([retried, failed, parent])).toBeUndefined();
  expect(continuedProposal(parent, [retried, failed, parent])).toBe(retried);
});

const stock: Ingredient = { id: "bread", name: "Bread", unit: "unit", on_hand: 10, reserved: 2, expires_at: "2026-10-12T00:00:00+09:00", buffer: 1 };
const staff: Staff = { id: "a", name: "A", role: "staff", absent: false, shift_start: "2026-10-11T11:00:00+09:00", shift_end: "2026-10-11T13:00:00+09:00", skills: { toast: 10 } };
function facts() {
  const snapshot: NonNullable<Proposal["snapshot"]> = { business_now: "2026-10-11T11:00:00+09:00", staff: [staff], next_day_staff: [staff], inventory: [stock], history: [], closes: [] };
  const data: Pick<Overview, "revision" | "business_now" | "staff" | "next_day_staff" | "inventory" | "history" | "closes"> = { business_now: "2026-10-11T11:05:00+09:00", staff: [staff], next_day_staff: [staff], inventory: [stock], history: [], closes: [], revision: 2 };
  const proposal: Pick<Proposal, "revision" | "snapshot"> = { revision: 1, snapshot };
  return {
    proposal,
    data,
  };
}
it("requires changes to each question's own data rather than a clock or unrelated edit", () => {
  const { data, proposal } = facts();
  expect(questionFactUpdated(data, proposal, "inventory")).toBe(false);
  data.staff = [{ ...staff, absent: true }];
  expect(questionFactUpdated(data, proposal, "staff")).toBe(true);
  expect(questionFactUpdated(data, proposal, "next_day_staff")).toBe(false);
  expect(questionFactUpdated(data, proposal, "inventory")).toBe(false);
  expect(questionFactUpdated(data, proposal, "history")).toBe(false);
  data.history = [{ date: "2026-10-10", counts: { toast: 10 }, restricted: false }];
  expect(questionFactUpdated(data, proposal, "history")).toBe(true);
  data.closes = [{ id: "close", service_date: "2026-10-11" }];
  expect(questionFactUpdated(data, proposal, "close")).toBe(true);
});
it("does not mistake a legacy stock lot projection for a saved correction", () => {
  const { data, proposal } = facts();
  data.inventory = [{ ...stock, lots: [{ id: "legacy-bread", quantity: 10, reserved: 2, expires_at: stock.expires_at, received_at: data.business_now }] }];
  expect(questionFactUpdated(data, proposal, "inventory")).toBe(false);
  data.inventory[0] = { ...stock, on_hand: 11 };
  expect(questionFactUpdated(data, proposal, "inventory")).toBe(true);
});
it("detects actual changes to stock lots even when aggregate stock is unchanged", () => {
  const { data, proposal } = facts();
  const existing = { ...stock, lots: [{ id: "lot-1", quantity: 10, reserved: 2, expires_at: stock.expires_at, received_at: data.business_now }] };
  proposal.snapshot!.inventory = [existing];
  data.inventory = [{ ...existing, lots: [{ ...existing.lots[0], expires_at: "2026-10-13T00:00:00+09:00" }, { id: "lot-2", quantity: 0, reserved: 0, expires_at: stock.expires_at, received_at: data.business_now }] }];
  expect(questionFactUpdated(data, proposal, "inventory")).toBe(true);
});
it("uses the compatibility fallback only for historical proposals without a fact snapshot", () => {
  const { data, proposal } = facts();
  delete proposal.snapshot;
  expect(questionFactUpdated(data, proposal, "inventory")).toBe(true);
  data.revision = proposal.revision;
  expect(questionFactUpdated(data, proposal, "inventory")).toBe(false);
  expect(questionFactUpdated(data, proposal, "objective")).toBe(true);
});

it("explains an already saved generic Gemini failure as a timeout when HTTP 504 was recorded", () => {
  const message = proposalErrorMessage({ error: "model_unavailable", provider_http_status: 504 });
  expect(message).toContain("Geminiの応答が制限時間内に返りませんでした");
  expect(message).toContain("再分析");
  expect(message).not.toContain("認証");
  expect(message).not.toContain("model_unavailable");
});

it.each([
  [400, "model_request_invalid", "リクエストが拒否"],
  [401, "model_authentication_failed", "認証を更新"],
  [403, "model_permission_denied", "Vertex AI権限"],
  [404, "model_not_found", "モデルID"],
  [408, "model_timeout", "制限時間内"],
  [429, "model_rate_limited", "クォータ・課金・クレジット残高"],
  [503, "model_service_unavailable", "一時的な障害"],
  [504, "model_timeout", "制限時間内"],
] as const)("shows an actionable provider diagnostic for HTTP %s and its explicit error code", (status, error, expected) => {
  const legacy = proposalErrorMessage({ error: "model_unavailable", provider_http_status: status });
  const current = proposalErrorMessage({ error, provider_http_status: null });
  expect(legacy).toContain(expected);
  expect(current).toBe(legacy);
  expect(current).not.toContain(error);
});

it("keeps a user interruption message even if an earlier provider timeout was recorded", () => {
  expect(proposalErrorMessage({ error: "interrupted", provider_http_status: 504 })).toContain("中断");
});

it("does not show raw unknown error codes or an error message for successful proposals", () => {
  expect(proposalErrorMessage({ error: "internal_unknown_error" })).not.toContain("internal_unknown_error");
  expect(proposalErrorMessage({})).toBeNull();
});
