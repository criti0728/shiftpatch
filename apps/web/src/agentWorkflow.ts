import type { AgentQuestion, Ingredient, Overview, Proposal, ProposalMode, Screen } from "./types";

type StoreState = Pick<Overview, "store" | "business_now" | "closes">;

export function isClosed(data: StoreState) {
  return data.closes.some(close => close.service_date === data.store.service_date);
}

function serviceTime(data: StoreState) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: data.store.timezone || "Asia/Tokyo",
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(new Date(data.business_now));
  const value = (type: string) => parts.find(part => part.type === type)?.value || "";
  return { day: `${value("year")}-${value("month")}-${value("day")}`, time: `${value("hour")}:${value("minute")}` };
}

export function analysisBlockReason(data: StoreState, mode: ProposalMode) {
  if (mode === "post_close" && !isClosed(data)) return "本日の営業を締めると翌日の分析を利用できます。";
  if (mode === "in_service") {
    if (isClosed(data)) return "本日は締め済みです。翌日の準備を分析してください。";
    const now = serviceTime(data);
    if (now.day !== data.store.service_date || now.time >= (data.store.close_time || "13:00")) return "営業時間が終了しました。営業を締めて翌日の準備を分析できます。";
    if (now.time < (data.store.open_time || "11:00")) return "営業時間前です。仕込み計画を分析してください。";
  }
  return "";
}

export function entryAnalysisMode(data: StoreState, screen: Screen): ProposalMode {
  if (screen === "prep") return "pre_open";
  if (screen === "close" || isClosed(data)) return "post_close";
  const now = serviceTime(data);
  if (now.day === data.store.service_date && now.time < (data.store.open_time || "11:00")) return "pre_open";
  return "in_service";
}

export function pendingQuestionProposal(proposals: Proposal[]) {
  return proposals.find(proposal => proposal.status === "waiting_input" && !continuedProposal(proposal, proposals));
}

export function continuedProposal(proposal: Proposal, proposals: Proposal[]) {
  if (!proposal.run_id) return undefined;
  const descendants = new Set([proposal.run_id]);
  let changed = true;
  while (changed) {
    changed = false;
    for (const child of proposals) {
      if (child.parent_run_id && descendants.has(child.parent_run_id) && child.run_id && !descendants.has(child.run_id)) {
        descendants.add(child.run_id);
        changed = true;
      }
    }
  }
  return proposals.find(child => child.id !== proposal.id && child.parent_run_id && descendants.has(child.parent_run_id) && child.status !== "failed");
}

function canonical(value: unknown) {
  return JSON.stringify(value, (_key, child) => child && typeof child === "object" && !Array.isArray(child)
    ? Object.fromEntries(Object.keys(child).sort().map(key => [key, child[key]])) : child);
}

function physicalInventory(rows: Ingredient[]) {
  return rows.map(row => {
    let lots = row.lots;
    if (!lots || lots.reduce((total, lot) => total + lot.quantity, 0) !== row.on_hand || lots.reduce((total, lot) => total + lot.reserved, 0) !== row.reserved) {
      lots = [{ id: `legacy-${row.id}`, quantity: row.on_hand, reserved: row.reserved, expires_at: row.expires_at, received_at: "" }];
    } else if (lots.length === 1 && lots[0].expires_at !== row.expires_at) {
      lots = [{ ...lots[0], expires_at: row.expires_at }];
    }
    return {
      id: row.id, name: row.name, unit: row.unit, on_hand: row.on_hand,
      reserved: row.reserved, expires_at: row.expires_at, buffer: row.buffer,
      lots: lots.map(lot => ({ ...lot, received_at: lot.id === `legacy-${row.id}` ? "" : lot.received_at })),
    };
  });
}

export function questionFactUpdated(data: Pick<Overview, "revision" | "staff" | "next_day_staff" | "inventory" | "history" | "closes">, proposal: Pick<Proposal, "revision" | "snapshot">, field: AgentQuestion["field"]) {
  if (field === "objective" || field === "allow_menu_pause" || field === "review_after_minutes") return true;
  const stateField = field === "close" ? "closes" : field;
  const previous = proposal.snapshot?.[stateField];
  // Older saved proposals did not carry snapshots. Match the server fallback.
  if (previous === undefined) return data.revision !== proposal.revision;
  const current = data[stateField];
  if (field === "inventory") return canonical(physicalInventory(previous as Ingredient[])) !== canonical(physicalInventory(current as Ingredient[]));
  return canonical(current) !== canonical(previous);
}
