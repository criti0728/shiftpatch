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
export function AgentQuestions({ questions, busy, onContinue, onEdit }: { questions: AgentQuestion[]; busy: boolean; onContinue: (answers: Record<string, string>) => Promise<void>; onEdit: (field: string) => void }) {
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const defaults: Record<string, string[]> = { objective: ["balanced", "preserve_drinks", "reduce_delay"], allow_menu_pause: ["true", "false"], review_after_minutes: ["15", "30"] };
  return <form className="agent-questions" onSubmit={async e => { e.preventDefault(); await onContinue(answers); }}>
    <h3><Sparkles size={17}/> 確認して分析を続ける</h3>
    {questions.map(q => <div className="form-label" key={q.id}><label htmlFor={`agent-answer-${q.id}`}>{q.text}</label>
      {defaults[q.field] ? <select id={`agent-answer-${q.id}`} required value={answers[q.id] || ""} onChange={e => setAnswers({ ...answers, [q.id]: e.target.value })}><option value="">選択してください</option>{(q.options.length ? q.options : defaults[q.field]).map(value => <option key={value} value={value}>{preferenceLabels[value] || value}</option>)}</select> : <><p className="field-help">店舗の事実は運営画面で先に保存してください。チャットの回答だけでは在庫や勤務を変更しません。</p><button type="button" className="button" onClick={() => onEdit(q.field)}>該当する入力画面を開く</button><select id={`agent-answer-${q.id}`} required value={answers[q.id] || ""} onChange={e => setAnswers({ ...answers, [q.id]: e.target.value })}><option value="">保存後に選択</option><option value="updated">運営データを保存しました</option></select></>}
    </div>)}
    <button className="button primary" disabled={busy || questions.some(q => !answers[q.id])}>回答して新しい条件で分析</button>
  </form>;
}
