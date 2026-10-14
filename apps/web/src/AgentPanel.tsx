import { useState } from "react";
import { Activity, Sparkles, LoaderCircle } from "lucide-react";
import type { AgentRun, AgentQuestion } from "./types";

const phases = { snapshot: "店舗データを固定", investigate: "状況を調査", calculate: "候補を計算・再検討", explain: "根拠とトレードオフを整理", validate: "結果を検証・保存" };
export function AgentProgress({ run, onCancel }: { run: AgentRun; onCancel: () => Promise<void> }) {
  const [cancelling, setCancelling] = useState(false);
  return <section className="agent-progress" aria-live="polite" aria-label="分析の進行状況">
    <div className="agent-progress-heading"><LoaderCircle size={18} className="spin" /><strong>{phases[run.phase] || "分析中"}</strong><span>{run.trace?.length || 0}件の実行記録</span>
      <button className="text-button" disabled={cancelling || run.cancel_requested} onClick={async () => { setCancelling(true); try { await onCancel(); } finally { setCancelling(false); } }}>{cancelling || run.cancel_requested ? "中断しています…" : "分析を中断"}</button>
    </div>
    <p>調査と計算は状況に応じて繰り返します。販売設定は承認後に反映します。</p>
    <ol>{(run.trace || []).slice(-5).map((step, i) => <li key={`${step.step}-${i}`}><Activity size={13}/><strong>{step.tool}</strong><span>{step.origin === "model" ? "AIが選択" : "サーバー"}</span></li>)}</ol>
  </section>;
}
const preferenceLabels: Record<string, string> = { balanced: "売上と待ち時間のバランス", preserve_drinks: "ドリンク販売を優先", reduce_delay: "待ち時間を優先", true: "販売停止を許可", false: "販売停止を許可しない", "15": "15分後", "30": "30分後" };
export function AgentQuestions({ questions, answers, onAnswersChange, factsUpdated, busy, onContinue, onEdit }: { questions: AgentQuestion[]; answers: Record<string, string>; onAnswersChange: (answers: Record<string, string>) => void; factsUpdated: Record<string, boolean>; busy: boolean; onContinue: (answers: Record<string, string>) => Promise<void>; onEdit: (field: string) => void }) {
  const [submitting, setSubmitting] = useState(false);
  const defaults: Record<string, string[]> = { objective: ["balanced", "preserve_drinks", "reduce_delay"], allow_menu_pause: ["true", "false"], review_after_minutes: ["15", "30"] };
  const blocked = busy || submitting;
  const missingAnswer = questions.some(q => !answers[q.id] || (!defaults[q.field] && !factsUpdated[q.id]));
  return <form className="agent-questions" onSubmit={async e => { e.preventDefault(); if (blocked || missingAnswer) return; setSubmitting(true); try { await onContinue(answers); } finally { setSubmitting(false); } }}>
    <h3><Sparkles size={17}/> 確認して分析を続ける</h3>
    {questions.map(q => <div className="form-label" key={q.id}><label htmlFor={`agent-answer-${q.id}`}>{q.text}</label>
      {defaults[q.field] ? <select id={`agent-answer-${q.id}`} disabled={blocked} required value={answers[q.id] || ""} onChange={e => onAnswersChange({ ...answers, [q.id]: e.target.value })}><option value="">選択してください</option>{(q.options.length ? q.options : defaults[q.field]).map(value => <option key={value} value={value}>{preferenceLabels[value] || value}</option>)}</select> : <><p className="field-help">該当する運営データを保存してから、この確認画面に戻ってください。{!factsUpdated[q.id] && "まだ質問対象のデータの変更が保存されていません。"}</p><button type="button" className="button" disabled={blocked} onClick={() => onEdit(q.field)}>該当する入力画面を開く</button><select id={`agent-answer-${q.id}`} disabled={blocked || !factsUpdated[q.id]} required value={answers[q.id] || ""} onChange={e => onAnswersChange({ ...answers, [q.id]: e.target.value })}><option value="">保存後に選択</option><option value="updated">運営データを保存しました</option></select></>}
    </div>)}
    <button className="button primary" disabled={blocked || missingAnswer}>{blocked ? <LoaderCircle size={16} className="spin"/> : <Sparkles size={16}/>} {blocked ? "回答から分析しています…" : "回答して新しい条件で分析"}</button>
  </form>;
}
