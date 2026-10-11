import { useEffect, useRef, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  ArrowRight,
  ArrowUpRight,
  CalendarDays,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleHelp,
  ClipboardCheck,
  Clock3,
  Coffee,
  FileText,
  PanelsTopLeft,
  UtensilsCrossed,
  Store,
  LoaderCircle,
  LogOut,
  Menu as MenuIcon,
  Minus,
  Package,
  Plus,
  RefreshCw,
  RotateCcw,
  Settings2,
  ShoppingBag,
  SlidersHorizontal,
  Sparkles,
  SquareArrowOutUpRight,
  TrendingUp,
  Users,
  X,
  AlertTriangle,
} from "lucide-react";
import { ApiError, request } from "./api";
import SessionGate from "./SessionGate";
import type {
  AuthenticatedSession,
  Candidate,
  Objective,
  Overview,
  Proposal,
  ProposalMode,
  Screen,
  Staff,
  Task,
} from "./types";

const money = (n: number) =>
  new Intl.NumberFormat("ja-JP", { style: "currency", currency: "JPY" }).format(
    n,
  );
const time = (value: string | null | undefined) =>
  value
    ? new Date(value).toLocaleTimeString("ja-JP", {
        hour: "2-digit",
        minute: "2-digit",
        timeZone: "Asia/Tokyo",
      })
    : "—";
const date = (value: string) =>
  new Date(value).toLocaleDateString("ja-JP", {
    month: "long",
    day: "numeric",
    weekday: "short",
    timeZone: "Asia/Tokyo",
  });
const number = (n: number) => new Intl.NumberFormat("ja-JP").format(n);
const objectiveNames: Record<Objective, string> = {
  balanced: "売上と待ち時間のバランス",
  preserve_drinks: "ドリンクの販売を優先",
  reduce_delay: "待ち時間の短縮を優先",
};
const tabs: { id: Screen; label: string; icon: typeof Coffee }[] = [
  { id: "service", label: "営業管理", icon: Activity },
  { id: "shifts", label: "スタッフ・シフト", icon: Users },
  { id: "prep", label: "仕込み・在庫", icon: Package },
  { id: "close", label: "振り返り・翌日", icon: ClipboardCheck },
];
const isOnShift = (staff: Staff, now: string) =>
  !staff.absent &&
  new Date(staff.shift_start) <= new Date(now) &&
  new Date(staff.shift_end) > new Date(now);
type Action = <T>(
  path: string,
  method?: string,
  body?: unknown,
  success?: string,
) => Promise<T | undefined>;

function Badge({
  children,
  tone = "",
}: {
  children: ReactNode;
  tone?: string;
}) {
  return <span className={`badge ${tone}`}>{children}</span>;
}
function Panel({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return <section className={`panel ${className}`}>{children}</section>;
}
function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="empty">
      <Coffee size={25} strokeWidth={1.4} />
      <p>{children}</p>
    </div>
  );
}
function Modal({
  title,
  children,
  onClose,
  wide = false,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
}) {
  const dialogRef = useRef<HTMLElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const previousFocus = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const focusable = () =>
      Array.from(
        dialogRef.current?.querySelectorAll<HTMLElement>(
          'button:not(:disabled), input:not(:disabled), select:not(:disabled), a[href], summary, [tabindex="0"]',
        ) || [],
      );
    focusable()[0]?.focus();
    const fn = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeRef.current();
      if (e.key === "Tab") {
        const elements = focusable();
        const first = elements[0];
        const last = elements[elements.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last?.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first?.focus();
        }
      }
    };
    window.addEventListener("keydown", fn);
    return () => {
      window.removeEventListener("keydown", fn);
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus();
    };
  }, []);
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <section
        ref={dialogRef}
        className={`modal ${wide ? "wide" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <h2>{title}</h2>
          <button className="icon-button" onClick={onClose} aria-label="閉じる">
            <X size={20} />
          </button>
        </div>
        {children}
      </section>
    </div>
  );
}

export default function App() {
  return (
    <SessionGate>
      {(access) => (
        <Workspace
          key={`${access.session.user.id}:${access.session.store.id}`}
          {...access}
        />
      )}
    </SessionGate>
  );
}

function Workspace({
  session,
  onLogout,
  onSignIn,
  sessionBusy,
}: {
  session: AuthenticatedSession;
  onLogout: () => Promise<void>;
  onSignIn: () => Promise<void>;
  sessionBusy: boolean;
}) {
  const client = useQueryClient();
  const [screen, setScreen] = useState<Screen>("service");
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState<{ text: string; error: boolean } | null>(
    null,
  );
  const [mobileNav, setMobileNav] = useState(false);
  const [resetOpen, setResetOpen] = useState(false);
  const [objective, setObjective] = useState<Objective>("balanced");
  const [activeProposal, setActiveProposal] = useState<Proposal | null>(null);
  const [generating, setGenerating] = useState(false);
  const { data, error, isPending, refetch } = useQuery({
    queryKey: ["overview", session.store.id],
    queryFn: ({ signal }) =>
      request<Overview>("/overview", "GET", undefined, signal),
    refetchInterval: 4000,
  });
  const demo = window.location.pathname.startsWith("/demo/order");
  useEffect(() => {
    if (
      error instanceof ApiError &&
      [
        "session_required",
        "unauthorized",
        "session_expired",
        "store_forbidden",
      ].includes(error.code)
    )
      void client.invalidateQueries({ queryKey: ["session"] });
  }, [error, client]);
  const act: Action = async <T,>(
    path: string,
    method = "POST",
    body: unknown = {},
    success?: string,
  ) => {
    if (busy || sessionBusy) return undefined;
    setBusy(path);
    setNotice(null);
    try {
      const result = await request<T>(path, method, body);
      await client.invalidateQueries({ queryKey: ["overview"] });
      if (success) setNotice({ text: success, error: false });
      return result;
    } catch (e) {
      if (
        e instanceof ApiError &&
        [
          "session_required",
          "unauthorized",
          "session_expired",
          "store_forbidden",
        ].includes(e.code)
      )
        void client.invalidateQueries({ queryKey: ["session"] });
      setNotice({
        text: e instanceof Error ? e.message : "操作に失敗しました。",
        error: true,
      });
      await client.invalidateQueries({ queryKey: ["overview"] });
      return undefined;
    } finally {
      setBusy("");
    }
  };
  useEffect(() => {
    if (!notice || notice.error) return;
    const timer = window.setTimeout(() => setNotice(null), 5500);
    return () => window.clearTimeout(timer);
  }, [notice]);
  async function generate(mode: ProposalMode) {
    setGenerating(true);
    const result = await act<Proposal>("/proposals", "POST", {
      mode,
      objective,
    });
    if (result) setActiveProposal(result);
    setGenerating(false);
  }
  const header = (
    <div className="wordmark">
      <span className="brand-symbol">
        <PanelsTopLeft size={21} strokeWidth={1.7} />
      </span>
      <span>
        Shift<span className="wordmark-light">Patch</span>
        <small>OPERATIONS</small>
      </span>
    </div>
  );
  if (isPending || !data)
    return (
      <div className="boot">
        {header}
        {isPending ? (
          <>
            <LoaderCircle className="spin" />
            <p>お店の状態を読み込んでいます…</p>
          </>
        ) : (
          <>
            <AlertTriangle size={32} />
            <h2>お店に接続できませんでした</h2>
            <p>
              {error instanceof Error
                ? error.message
                : "APIサーバーの起動を確認してください。"}
            </p>
            <button className="button primary" onClick={() => refetch()}>
              <RefreshCw size={16} />
              再接続
            </button>
          </>
        )}
      </div>
    );
  const props = {
    data,
    act,
    busy: Boolean(busy) || sessionBusy,
    generate,
    generating,
    objective,
    setObjective,
  };
  return (
    <div className={demo ? "app demo-app" : "app"}>
      {!demo && (
        <>
          <aside className={`sidebar ${mobileNav ? "visible" : ""}`}>
            {header}
            <div className="store-switch">
              <span className="store-avatar">
                <Store size={17} />
              </span>
              <div>
                <strong>{data.store.name}</strong>
                <small>あなた専用のサンプル店舗</small>
              </div>
            </div>
            <p className="nav-label">店舗管理</p>
            <nav>
              {tabs.map((tab) => (
                <button
                  key={tab.id}
                  className={`nav-item ${screen === tab.id ? "active" : ""}`}
                  aria-current={screen === tab.id ? "page" : undefined}
                  onClick={() => {
                    setScreen(tab.id);
                    setMobileNav(false);
                    window.scrollTo({ top: 0, behavior: "auto" });
                  }}
                >
                  <tab.icon size={19} strokeWidth={1.65} />
                  <span>{tab.label}</span>
                  {screen === tab.id && <span className="nav-dot" />}
                </button>
              ))}
            </nav>
            <div className="sidebar-bottom">
              <a
                className="demo-link"
                href="/demo/order"
                target="_blank"
                rel="noreferrer"
              >
                <ShoppingBag size={17} />
                サンプル注文画面
                <SquareArrowOutUpRight size={14} />
              </a>
              <div className="profile">
                <span>
                  {session.user.kind === "guest"
                    ? "ゲ"
                    : session.user.display_name.slice(0, 1)}
                </span>
                <div>
                  <strong>{session.user.display_name}</strong>
                  <small>
                    {session.user.kind === "guest"
                      ? "ゲスト · ブラウザ保存"
                      : "Google アカウント保存"}
                  </small>
                </div>
              </div>
              {session.user.kind === "guest" &&
                session.firebase_enabled &&
                session.firebase_config && (
                  <button
                    className="sidebar-account-button"
                    disabled={Boolean(busy) || generating || sessionBusy}
                    onClick={onSignIn}
                  >
                    <Users size={15} />
                    Google の店舗に切り替え
                  </button>
                )}
              <button
                className="sidebar-account-button"
                disabled={Boolean(busy) || generating || sessionBusy}
                onClick={onLogout}
              >
                <LogOut size={15} />
                {sessionBusy ? "切り替え中…" : "入口に戻る・ログアウト"}
              </button>
              <p className="sidebar-sample-note">
                合成データを使った体験用店舗です。ほかの利用者の操作は反映されません。
              </p>
            </div>
          </aside>
          {mobileNav && (
            <div className="nav-overlay" onClick={() => setMobileNav(false)} />
          )}
        </>
      )}
      <main className="workspace">
        <header className="topbar">
          {demo ? (
            <a className="back-link" href="/">
              <ChevronLeft size={16} />
              店長画面へ
            </a>
          ) : (
            <div className="breadcrumb">
              <button
                className="icon-button mobile-only"
                aria-label="メニュー"
                onClick={() => setMobileNav(!mobileNav)}
              >
                <MenuIcon size={20} />
              </button>
              <span>{data.store.name}</span>
              <ChevronRight size={13} />
              <strong>{tabs.find((t) => t.id === screen)?.label}</strong>
            </div>
          )}
          <div className="topbar-right">
            <span className={`live-indicator ${error ? "offline" : ""}`}>
              <span />
              {error ? "接続エラー" : "データ接続中"}
            </span>
            <span className="topbar-date">
              <CalendarDays size={15} />
              {date(data.business_now)}
            </span>
          </div>
        </header>
        <div className="page">
          <div className="page-heading">
            <div className="heading-main">
              <div className="heading-title-row">
                <h1>
                  {demo
                    ? "サンプル注文"
                    : tabs.find((tab) => tab.id === screen)?.label}
                </h1>
                <Badge
                  tone={
                    data.closes.some(
                      (close) => close.service_date === data.store.service_date,
                    )
                      ? ""
                      : "green"
                  }
                >
                  {data.closes.some(
                    (close) => close.service_date === data.store.service_date,
                  )
                    ? "営業締め済み"
                    : "本日未締め"}
                </Badge>
              </div>
              <div className="heading-meta">
                <span>
                  <CalendarDays size={13} />
                  {data.store.service_date}
                </span>
                <span>
                  <Users size={13} />
                  勤務中{" "}
                  {
                    data.staff.filter((staff) =>
                      isOnShift(staff, data.business_now),
                    ).length
                  }
                  名
                </span>
                <span>
                  <ShoppingBag size={13} />
                  未完了 {data.metrics.waiting_orders}件
                </span>
              </div>
            </div>
            <div className="clock-card">
              <span>体験用の営業時刻</span>
              <strong>
                {time(data.business_now)}
                <small>JST</small>
              </strong>
              <div>
                <button
                  disabled={!!busy || sessionBusy}
                  onClick={() => act("/demo/advance", "POST", { minutes: 5 })}
                >
                  +5分
                </button>
                <button
                  disabled={!!busy || sessionBusy}
                  onClick={() => act("/demo/advance", "POST", { minutes: 15 })}
                >
                  +15分
                </button>
                <button
                  title="サンプル店舗をリセット"
                  aria-label="サンプル店舗をリセット"
                  disabled={!!busy || sessionBusy}
                  onClick={() => setResetOpen(true)}
                >
                  <RotateCcw size={13} />
                </button>
              </div>
            </div>
          </div>
          <div
            className={`environment-note ${data.config.agent_mode === "gemini" ? "connected" : ""}`}
          >
            <span className="note-icon">
              {data.config.agent_mode === "gemini" ? (
                <Sparkles size={15} />
              ) : (
                <CircleHelp size={15} />
              )}
            </span>
            <span>
              {data.config.agent_mode === "gemini"
                ? "Gemini モード · 運営データの調査と候補の再検討を行います。"
                : "計算モード · 現在の店舗データを使い、販売数と待ち時間を比較します。AIによる調査・判断は接続設定後に利用できます。"}
            </span>
            <Badge>自分専用のデータ</Badge>
          </div>
          {error && (
            <div className="alert warning">
              <AlertTriangle size={16} />
              最新の状態を取得できません。表示は最後に取得したデータです。
              <button onClick={() => refetch()}>再接続</button>
            </div>
          )}
          {demo ? (
            <OrderPage {...props} />
          ) : screen === "service" ? (
            <Service {...props} onProposal={setActiveProposal} />
          ) : screen === "shifts" ? (
            <Shifts {...props} />
          ) : screen === "prep" ? (
            <Preparation {...props} onProposal={setActiveProposal} />
          ) : (
            <ClosePage {...props} onProposal={setActiveProposal} />
          )}
          <footer className="page-footer">
            <span>
              <PanelsTopLeft size={13} />
              ShiftPatch <span className="footer-divider">/</span> 店舗運営管理
            </span>
            <span>
              営業日 {data.store.service_date} · revision {data.revision}
            </span>
          </footer>
        </div>
      </main>
      {notice && (
        <div
          role={notice.error ? "alert" : "status"}
          className={`toast ${notice.error ? "error" : ""}`}
        >
          {notice.error ? (
            <AlertTriangle size={18} />
          ) : (
            <CheckCircle2 size={18} />
          )}
          <span>{notice.text}</span>
          <button
            className="icon-button"
            aria-label="通知を閉じる"
            onClick={() => setNotice(null)}
          >
            <X size={16} />
          </button>
        </div>
      )}
      {activeProposal && (
        <ProposalModal
          proposal={
            data.proposals.find((p) => p.id === activeProposal.id) ||
            activeProposal
          }
          data={data}
          act={act}
          busy={!!busy || sessionBusy}
          onClose={() => setActiveProposal(null)}
        />
      )}
      {resetOpen && (
        <Modal
          title="サンプル店舗を最初からやり直す"
          onClose={() => setResetOpen(false)}
        >
          <p className="modal-description">
            あなたのサンプル店舗の注文、在庫変更、シフト変更、提案、営業締めの記録を初期データに戻します。ほかの利用者の店舗には影響しません。
          </p>
          <div className="modal-actions">
            <button className="button" onClick={() => setResetOpen(false)}>
              キャンセル
            </button>
            <button
              className="button danger"
              disabled={!!busy || sessionBusy}
              onClick={async () => {
                if (
                  await act(
                    "/demo/reset",
                    "POST",
                    {},
                    "あなたのサンプル店舗を初期状態に戻しました。",
                  )
                ) {
                  setResetOpen(false);
                  setActiveProposal(null);
                }
              }}
            >
              初期状態に戻す
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}

type CommonProps = {
  data: Overview;
  act: Action;
  busy: boolean;
  generate: (mode: ProposalMode) => Promise<void>;
  generating: boolean;
  objective: Objective;
  setObjective: (objective: Objective) => void;
};
function GenerateButton({
  mode,
  generate,
  generating,
  busy,
  children,
}: Pick<CommonProps, "generate" | "generating" | "busy"> & {
  mode: ProposalMode;
  children?: ReactNode;
}) {
  return (
    <button
      className="button primary"
      disabled={busy || generating}
      onClick={() => generate(mode)}
    >
      {generating ? (
        <LoaderCircle className="spin" size={16} />
      ) : (
        <Sparkles size={16} />
      )}
      {generating ? "計算中…" : children || "運営案を計算"}
    </button>
  );
}
function SummaryMetrics({ data }: { data: Overview }) {
  const present = data.staff.filter((staff) =>
    isOnShift(staff, data.business_now),
  ).length;
  const oldest = data.orders
    .filter((o) => o.status !== "completed")
    .reduce(
      (max, o) =>
        Math.max(
          max,
          (new Date(data.business_now).getTime() -
            new Date(o.created_at).getTime()) /
            60000,
        ),
      0,
    );
  return (
    <div className="metric-grid">
      {[
        {
          label: "未完了注文",
          value: data.metrics.waiting_orders,
          suffix: "件",
          note: `最長経過 ${Math.max(0, Math.floor(oldest))}分`,
          icon: Clock3,
        },
        {
          label: "本日の売上",
          value: money(data.metrics.revenue),
          suffix: "",
          note: "完了注文の販売金額",
          icon: TrendingUp,
        },
        {
          label: "完了注文",
          value: data.metrics.completed_orders,
          suffix: "件",
          note: "全商品の提供が完了",
          icon: CheckCircle2,
        },
        {
          label: "勤務中スタッフ",
          value: present,
          suffix: `/ ${data.staff.length}名`,
          note: data.staff.some((staff) => staff.absent)
            ? `欠勤 ${data.staff.filter((staff) => staff.absent).length}名`
            : "欠勤登録なし",
          icon: Users,
        },
      ].map((item, i) => (
        <Panel className="metric" key={item.label}>
          <div className="metric-label">
            {item.label}
            <item.icon size={17} />
          </div>
          <div className="metric-value">
            {item.value}
            <span>{item.suffix}</span>
          </div>
          <div
            className={`metric-note ${i === 0 && data.metrics.waiting_orders > 3 ? "amber" : ""}`}
          >
            {item.note}
          </div>
        </Panel>
      ))}
    </div>
  );
}
function Service(
  props: CommonProps & { onProposal: (proposal: Proposal) => void },
) {
  const { data, busy, act, onProposal } = props;
  const closed = data.closes.some(
    (close) => close.service_date === data.store.service_date,
  );
  const [orderFilter, setOrderFilter] = useState<"open" | "all">("open");
  const orders = [...data.orders]
    .filter((o) => orderFilter === "all" || o.status !== "completed")
    .sort((a, b) => a.created_at.localeCompare(b.created_at));
  const latest = data.proposals.find((p) => p.mode === "in_service");
  const limited = Object.entries(data.mode.limits || {});
  return (
    <>
      <SummaryMetrics data={data} />
      {closed && (
        <div className="alert">
          <ClipboardCheck size={16} />
          本日の営業は締め済みです。新規注文の受付は終了しました。既存注文の準備は続けられます。
        </div>
      )}
      <div className="service-grid">
        <div className="main-column">
          <Panel className="queue-panel">
            <div className="section-heading">
              <div>
                <h2>
                  注文一覧{" "}
                  <span className="count-pill">
                    {data.metrics.waiting_orders}
                  </span>
                </h2>
                {props.generating ? (
                  <p className="queue-busy-status" role="status">
                    <LoaderCircle className="spin" size={13} />
                    運営案を計算中 · 完了まで操作をお待ちください
                  </p>
                ) : (
                  <p>受付順に表示。担当者を選択して作業を進めてください。</p>
                )}
              </div>
              <div className="segmented">
                <button
                  className={orderFilter === "open" ? "selected" : ""}
                  onClick={() => setOrderFilter("open")}
                >
                  進行中
                </button>
                <button
                  className={orderFilter === "all" ? "selected" : ""}
                  onClick={() => setOrderFilter("all")}
                >
                  すべて
                </button>
              </div>
            </div>
            {orders.length > 0 && (
              <p className="table-scroll-hint">
                <ArrowRight size={12} /> 横にスクロールして担当・状態を確認
              </p>
            )}
            {orders.length === 0 ? (
              <Empty>
                表示対象の注文はありません。
                <br />
                <a href="/demo/order" target="_blank" rel="noreferrer">
                  サンプル注文画面から注文を入れる <ArrowUpRight size={12} />
                </a>
              </Empty>
            ) : (
              <div
                className="table-scroll order-table-wrap"
                role="region"
                aria-label="注文一覧（横スクロール可能）"
                tabIndex={0}
              >
                <table className="order-table">
                  <thead>
                    <tr>
                      <th>番号</th>
                      <th>受付 / 経過</th>
                      <th>明細・作業</th>
                      <th>状態</th>
                      <th className="numeric">金額</th>
                    </tr>
                  </thead>
                  <tbody>
                    {orders.map((order) => (
                      <tr
                        className={`order-table-row ${order.status}`}
                        key={order.id}
                      >
                        <td className="order-id-cell">
                          <strong>
                            #{String(order.number).padStart(3, "0")}
                          </strong>
                        </td>
                        <td className="order-time-cell">
                          <span>{time(order.created_at)}</span>
                          <small>
                            {order.status === "completed"
                              ? "完了"
                              : `${Math.max(0, Math.floor((new Date(data.business_now).getTime() - new Date(order.created_at).getTime()) / 60000))}分経過`}
                          </small>
                        </td>
                        <td className="order-tasks-cell">
                          <div className="order-task-list">
                            {order.tasks.map((task) => (
                              <TaskRow
                                key={task.id}
                                task={task}
                                orderId={order.id}
                                {...props}
                              />
                            ))}
                          </div>
                        </td>
                        <td className="order-status-cell">
                          <Badge
                            tone={
                              order.status === "completed"
                                ? "green"
                                : order.status === "preparing"
                                  ? "amber"
                                  : ""
                            }
                          >
                            {order.status === "completed"
                              ? "完了"
                              : order.status === "preparing"
                                ? "準備中"
                                : "受付済み"}
                          </Badge>
                          <small>
                            {
                              order.tasks.filter(
                                (task) => task.status === "completed",
                              ).length
                            }{" "}
                            / {order.tasks.length}点
                          </small>
                        </td>
                        <td className="order-total-cell numeric">
                          <strong>{money(order.total)}</strong>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
        </div>
        <div className="side-column">
          <Panel className="decision-panel">
            <div className="section-heading">
              <div className="heading-with-icon">
                <span className="soft-icon">
                  <Sparkles size={20} />
                </span>
                <div>
                  <h2>運営プラン</h2>
                  <p>人員・在庫・注文をもとに30分先を比較</p>
                </div>
              </div>
              <Badge tone={data.config.agent_mode === "gemini" ? "green" : ""}>
                {data.config.agent_mode === "gemini" ? "Gemini" : "計算モード"}
              </Badge>
            </div>
            <div className="decision-body">
              <p>
                {data.config.agent_mode === "gemini"
                  ? "優先事項を指定して候補を生成します。"
                  : "現在の営業条件で固定の3案を計算します。"}
                販売数・待ち時間を確認し、承認した案を適用します。
              </p>
              <div className="decision-actions">
                <label className="select-field">
                  <SlidersHorizontal size={15} />
                  <select
                    aria-label="運営案の優先事項"
                    disabled={data.config.agent_mode === "calculation_only"}
                    value={props.objective}
                    onChange={(e) =>
                      props.setObjective(e.target.value as Objective)
                    }
                  >
                    {Object.entries(objectiveNames).map(([key, label]) => (
                      <option key={key} value={key}>
                        {label}
                      </option>
                    ))}
                  </select>
                </label>
                <GenerateButton
                  {...props}
                  busy={busy || closed}
                  mode="in_service"
                />
              </div>
              {data.config.agent_mode === "calculation_only" && (
                <p className="field-help">
                  優先事項の指定にはGemini接続が必要です。
                </p>
              )}
            </div>
            {latest && (
              <button
                className="latest-proposal"
                onClick={() => onProposal(latest)}
              >
                <FileText size={16} />
                <span>
                  最新の運営プラン
                  <span>
                    {time(latest.created_at)} 作成 · {latest.candidates.length}
                    案を比較
                  </span>
                </span>
                <Badge tone={latest.status === "applied" ? "green" : ""}>
                  {statusLabel(latest.status)}
                </Badge>
                <ArrowRight size={17} />
              </button>
            )}
          </Panel>
          <Panel className="mode-panel">
            <div className="section-heading compact">
              <h2>販売設定</h2>
              <span className="green-dot" />
            </div>
            <div className="mode-body">
              <Badge
                tone={
                  data.mode.status === "review_required" ? "amber" : "green"
                }
              >
                {closed
                  ? "本日の受付終了"
                  : data.mode.status === "review_required"
                    ? "見直しが必要"
                    : limited.length
                      ? "販売制限中"
                      : "通常営業"}
              </Badge>
              <h3>
                {closed
                  ? "営業締め済み"
                  : limited.length
                    ? "受付数量の制限を適用中"
                    : "受付数量の制限なし"}
              </h3>
              <p>
                {closed
                  ? "新規注文は停止しています。未提供の注文は引き続き準備できます。"
                  : limited.length
                    ? "承認したプランが、新規注文の受付に反映されています。"
                    : "ご注文は在庫とスタッフの対応可否を確認して受け付けます。"}
              </p>
              {limited.map(([id, cap]) => (
                <div className="limit-row" key={id}>
                  <span>{data.menus.find((m) => m.id === id)?.name || id}</span>
                  <strong>
                    {cap === 0
                      ? "新規受付停止"
                      : `残り ${Math.max(0, cap - (data.mode.used[id] || 0))} / ${cap}点`}
                  </strong>
                </div>
              ))}
              {data.mode.review_at && (
                <div className="review-time">
                  <Clock3 size={14} />
                  {time(data.mode.review_at)} に見直し
                  {data.mode.status === "review_required" &&
                    " · 解除まで制限を維持"}
                </div>
              )}
              {limited.length > 0 && !closed && (
                <button
                  className="button full"
                  disabled={busy}
                  onClick={() =>
                    act(
                      "/service-modes/current/review",
                      "POST",
                      { action: "release" },
                      "販売制限を解除しました。",
                    )
                  }
                >
                  販売制限を解除
                </button>
              )}
            </div>
          </Panel>
          <Panel>
            <div className="section-heading compact">
              <h2>勤務状況</h2>
              <span className="subtle">{data.staff.length}人</span>
            </div>
            <div className="team-list">
              {data.staff.map((staff, i) => (
                <div className="team-row" key={staff.id}>
                  <span className={`avatar avatar-${i % 4}`}>
                    {staff.name.slice(0, 1)}
                  </span>
                  <div>
                    <strong>{staff.name}</strong>
                    <small>
                      {time(staff.shift_start)}–{time(staff.shift_end)}
                    </small>
                  </div>
                  <Badge
                    tone={
                      staff.absent
                        ? "red"
                        : staff.shift_start <= data.business_now &&
                            staff.shift_end > data.business_now
                          ? "green"
                          : ""
                    }
                  >
                    {staff.absent
                      ? "欠勤"
                      : staff.shift_start <= data.business_now &&
                          staff.shift_end > data.business_now
                        ? "勤務中"
                        : "時間外"}
                  </Badge>
                </div>
              ))}
            </div>
          </Panel>
          <Panel>
            <div className="section-heading compact">
              <h2>操作履歴</h2>
              <Activity size={16} />
            </div>
            <div className="event-list">
              {data.events.slice(0, 5).map((event) => (
                <div className="event" key={event.id}>
                  <span className="event-dot" />
                  <div>
                    <p>{event.message}</p>
                    <small>{time(event.at)}</small>
                  </div>
                </div>
              ))}
              {data.events.length === 0 && (
                <p className="subtle">操作履歴はありません。</p>
              )}
            </div>
          </Panel>
        </div>
      </div>
    </>
  );
}
function TaskRow({
  task,
  orderId,
  data,
  act,
  busy,
}: Pick<CommonProps, "data" | "act" | "busy"> & {
  task: Task;
  orderId: string;
}) {
  const [staffId, setStaffId] = useState("");
  const menu = data.menus.find((m) => m.id === task.menu_id);
  const order = data.orders.find((order) => order.id === orderId);
  const taskLabel = `注文 #${String(order?.number ?? orderId).padStart(3, "0")} ${menu?.name || task.menu_id} 作業${(order?.tasks.findIndex((item) => item.id === task.id) ?? 0) + 1}`;
  const preparing = data.orders
    .flatMap((o) => o.tasks)
    .filter((t) => t.status === "preparing");
  const eligible = data.staff.filter(
    (s) =>
      !s.absent &&
      s.skills[task.menu_id] > 0 &&
      new Date(s.shift_start).getTime() <=
        new Date(data.business_now).getTime() &&
      new Date(s.shift_end).getTime() >=
        new Date(data.business_now).getTime() + s.skills[task.menu_id] * 1000 &&
      !preparing.some((t) => t.staff_id === s.id),
  );
  const stationBusy = preparing.some((t) => t.station === task.station);
  const selected = eligible.some((s) => s.id === staffId)
    ? staffId
    : eligible[0]?.id || "";
  const readyAt =
    task.started_at && task.duration_seconds != null
      ? new Date(
          new Date(task.started_at).getTime() + task.duration_seconds * 1000,
        )
      : null;
  const canComplete = !readyAt || new Date(data.business_now) >= readyAt;
  return (
    <div className={`task-row ${task.status}`}>
      <span
        className="menu-symbol"
        aria-label={menu?.category === "food" ? "フード" : "ドリンク"}
      >
        {menu?.category === "food" ? (
          <UtensilsCrossed size={14} />
        ) : (
          <Coffee size={14} />
        )}
      </span>
      <div className="task-title">
        <strong>{menu?.name || task.menu_id}</strong>
        <small>
          {task.status === "preparing"
            ? `${data.staff.find((s) => s.id === task.staff_id)?.name || "担当者"} · ${time(readyAt?.toISOString())} 完了可能`
            : task.status === "completed"
              ? "作業完了"
              : stationBusy
                ? "作業台を使用中"
                : "担当者を選択"}
        </small>
      </div>
      {task.status === "accepted" ? (
        <div className="task-actions">
          <select
            aria-label={`${taskLabel}の担当スタッフ`}
            value={selected}
            onChange={(e) => setStaffId(e.target.value)}
            disabled={busy || !eligible.length || stationBusy}
          >
            {eligible.length ? (
              eligible.map((s) => (
                <option value={s.id} key={s.id}>
                  {s.name}
                </option>
              ))
            ) : (
              <option value="">担当可能者なし</option>
            )}
          </select>
          <button
            className="button small"
            aria-label={`${taskLabel}を開始`}
            disabled={busy || !selected || stationBusy}
            onClick={() =>
              act(`/orders/${orderId}/tasks/${task.id}/status`, "POST", {
                status: "preparing",
                staff_id: selected,
              })
            }
          >
            開始
          </button>
        </div>
      ) : task.status === "preparing" ? (
        <button
          className="button small primary"
          aria-label={`${taskLabel}を完了`}
          disabled={busy || !canComplete}
          title={
            !canComplete
              ? "体験用の営業時刻を進めて、準備時間の経過を待ってください。"
              : ""
          }
          onClick={() =>
            act(`/orders/${orderId}/tasks/${task.id}/status`, "POST", {
              status: "completed",
            })
          }
        >
          <Check size={14} />
          完了
        </button>
      ) : (
        <CheckCircle2 className="completed-icon" size={19} />
      )}
    </div>
  );
}

function Shifts(props: CommonProps) {
  const { data, act, busy } = props;
  const [editing, setEditing] = useState<{
    staff: Staff;
    next: boolean;
  } | null>(null);
  const [nextDay, setNextDay] = useState(false);
  const list = nextDay ? data.next_day_staff || [] : data.staff;
  return (
    <>
      <div className="section-toolbar">
        <div className="segmented">
          <button
            className={!nextDay ? "selected" : ""}
            onClick={() => setNextDay(false)}
          >
            今日のシフト
          </button>
          <button
            className={nextDay ? "selected" : ""}
            onClick={() => setNextDay(true)}
          >
            翌日のシフト
          </button>
        </div>
        <span className="subtle">欠勤・時間・メニュー別の調理時間を管理</span>
      </div>
      <div className="staff-grid">
        {list.map((staff, i) => (
          <Panel className="staff-card" key={staff.id}>
            <div className="staff-card-head">
              <span className={`avatar large avatar-${i % 4}`}>
                {staff.name.slice(0, 1)}
              </span>
              <div>
                <h2>{staff.name}</h2>
                <p>{staff.role}</p>
              </div>
              <Badge tone={staff.absent ? "red" : "green"}>
                {staff.absent ? "欠勤" : "出勤予定"}
              </Badge>
            </div>
            <div className="shift-time">
              <Clock3 size={17} />
              <strong>
                {time(staff.shift_start)} <span>—</span> {time(staff.shift_end)}
              </strong>
              <button
                className="text-button"
                onClick={() => setEditing({ staff, next: nextDay })}
              >
                <Settings2 size={14} />
                編集
              </button>
            </div>
            <div className="skill-list">
              <div className="small-label">1点あたりの準備時間</div>
              {data.menus.map((menu) => (
                <div className="skill-row" key={menu.id}>
                  <span>
                    {menu.emoji} {menu.name}
                  </span>
                  {staff.skills[menu.id] > 0 ? (
                    <strong>
                      {Math.round(staff.skills[menu.id] / 6) / 10}
                      <small>分</small>
                    </strong>
                  ) : (
                    <span className="subtle">対応なし</span>
                  )}
                </div>
              ))}
            </div>
            <button
              className={`button full ${staff.absent ? "" : "absent-button"}`}
              disabled={busy}
              onClick={() =>
                act(
                  `${nextDay ? "/next-day-staff" : "/staff"}/${staff.id}`,
                  "PATCH",
                  { absent: !staff.absent },
                  staff.absent
                    ? "出勤予定に戻しました。"
                    : "欠勤を登録しました。営業プランを見直せます。",
                )
              }
            >
              {staff.absent ? <Check size={15} /> : <Users size={15} />}
              {staff.absent ? "出勤予定に戻す" : "欠勤を登録"}
            </button>
          </Panel>
        ))}
      </div>
      {list.length === 0 && (
        <Panel>
          <Empty>翌日のシフトはまだ登録されていません。</Empty>
        </Panel>
      )}
      <Panel className="info-panel">
        <CircleHelp size={19} />
        <div>
          <h3>勤務情報を変更した場合</h3>
          <p>
            欠勤・勤務時間・準備時間の変更後は、営業管理で運営案を再計算してください。承認時に最新の状態を検証します。
          </p>
        </div>
      </Panel>
      {editing && (
        <StaffEditor
          staff={editing.staff}
          next={editing.next}
          data={data}
          act={act}
          busy={busy}
          onClose={() => setEditing(null)}
        />
      )}
    </>
  );
}
function StaffEditor({
  staff,
  next,
  data,
  act,
  busy,
  onClose,
}: {
  staff: Staff;
  next: boolean;
  data: Overview;
  act: Action;
  busy: boolean;
  onClose: () => void;
}) {
  const [start, setStart] = useState(time(staff.shift_start));
  const [end, setEnd] = useState(time(staff.shift_end));
  const [skills, setSkills] = useState<Record<string, number>>(staff.skills);
  const [available, setAvailable] = useState(staff.available_dates || []);
  const [month, setMonth] = useState(() => staff.shift_start.slice(0, 7));
  const [year, mon] = month.split("-").map(Number);
  const days = new Date(year, mon, 0).getDate();
  const offset = new Date(year, mon - 1, 1).getDay();
  const onDay = (day: number) => {
    const key = `${month}-${String(day).padStart(2, "0")}`;
    setAvailable((a) =>
      a.includes(key) ? a.filter((x) => x !== key) : [...a, key],
    );
  };
  function iso(t: string) {
    const day = new Date(staff.shift_start).toLocaleDateString("sv-SE", {
      timeZone: "Asia/Tokyo",
    });
    return new Date(`${day}T${t}:00+09:00`).toISOString();
  }
  return (
    <Modal title={`${staff.name}の勤務設定`} onClose={onClose} wide>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          const result = await act(
            `${next ? "/next-day-staff" : "/staff"}/${staff.id}`,
            "PATCH",
            {
              shift_start: iso(start),
              shift_end: iso(end),
              skills,
              available_dates: available,
            },
            "勤務設定を保存しました。",
          );
          if (result) onClose();
        }}
      >
        <div className="editor-grid">
          <div>
            <h3>確定シフト</h3>
            <div className="form-row">
              <label>
                開始時刻
                <input
                  required
                  type="time"
                  value={start}
                  onChange={(e) => setStart(e.target.value)}
                />
              </label>
              <label>
                終了時刻
                <input
                  required
                  type="time"
                  value={end}
                  onChange={(e) => setEnd(e.target.value)}
                />
              </label>
            </div>
            <h3 className="form-subheading">メニュー別の準備時間</h3>
            <p className="field-help">秒単位で入力。0 は対応不可です。</p>
            {data.menus.map((menu) => (
              <label className="number-field" key={menu.id}>
                <span>
                  {menu.emoji} {menu.name}
                </span>
                <div>
                  <input
                    type="number"
                    min="0"
                    max="7200"
                    step="1"
                    value={skills[menu.id] || 0}
                    onChange={(e) =>
                      setSkills({
                        ...skills,
                        [menu.id]: Number(e.target.value),
                      })
                    }
                  />
                  <span>秒</span>
                </div>
              </label>
            ))}
          </div>
          <div>
            <h3>勤務可能日</h3>
            <p className="field-help">
              日付を選択して、勤務可能日を登録します。
            </p>
            <input
              aria-label="勤務可能日の月"
              type="month"
              value={month}
              onChange={(e) => e.target.value && setMonth(e.target.value)}
            />
            <div className="calendar">
              <div className="calendar-week">
                {["日", "月", "火", "水", "木", "金", "土"].map((day) => (
                  <span key={day}>{day}</span>
                ))}
              </div>
              <div className="calendar-days">
                {Array.from({ length: offset }, (_, i) => (
                  <span key={`empty-${i}`} />
                ))}
                {Array.from({ length: days }, (_, i) => {
                  const day = i + 1;
                  const key = `${month}-${String(day).padStart(2, "0")}`;
                  return (
                    <button
                      type="button"
                      className={available.includes(key) ? "available" : ""}
                      key={day}
                      aria-label={`${key} 勤務可能${available.includes(key) ? "を解除" : "にする"}`}
                      aria-pressed={available.includes(key)}
                      onClick={() => onDay(day)}
                    >
                      {day}
                    </button>
                  );
                })}
              </div>
            </div>
            <div className="calendar-legend">
              <span className="tiny-dot" />
              勤務可能{" "}
              <span className="subtle">
                {available.filter((d) => d.startsWith(month)).length}日を選択
              </span>
            </div>
            <p className="field-help">
              可能日の登録だけでは確定シフトは変更されません。
            </p>
          </div>
        </div>
        <div className="modal-actions">
          <button type="button" className="button" onClick={onClose}>
            キャンセル
          </button>
          <button className="button primary" disabled={busy}>
            <Check size={15} />
            設定を保存
          </button>
        </div>
      </form>
    </Modal>
  );
}

function Preparation(
  props: CommonProps & { onProposal: (proposal: Proposal) => void },
) {
  const { data, act, busy } = props;
  const [adjust, setAdjust] = useState<{
    id: string;
    name: string;
    unit: string;
    value: number;
    reserved: number;
  } | null>(null);
  const [receive, setReceive] = useState<{
    id: string;
    name: string;
    unit: string;
    value: number;
  } | null>(null);
  const latest = data.proposals.find((p) => p.mode === "pre_open");
  return (
    <>
      <div className="prep-banner">
        <div>
          <h2>仕込み計画</h2>
          <p>過去の販売と勤務体制、実在庫・入荷予定から必要量を計算します。</p>
        </div>
        <GenerateButton {...props} mode="pre_open">
          仕込み計画を計算
        </GenerateButton>
      </div>
      {latest && (
        <button className="plan-strip" onClick={() => props.onProposal(latest)}>
          <ClipboardCheck size={19} />
          <div>
            <strong>直近の仕込みプラン</strong>
            <span>
              {time(latest.created_at)} · {statusLabel(latest.status)}
            </span>
          </div>
          <ArrowRight size={18} />
        </button>
      )}
      <div className="prep-grid">
        <Panel>
          <div className="section-heading">
            <div>
              <h2>原材料在庫</h2>
              <p>実在庫から受付済み注文の予約分を除いた数量が利用可能です。</p>
            </div>
            <Badge>{data.inventory.length}品目</Badge>
          </div>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>材料</th>
                  <th className="numeric">実在庫</th>
                  <th className="numeric">予約済み</th>
                  <th className="numeric">利用可能</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.inventory.map((item) => {
                  const usable = item.on_hand - item.reserved;
                  return (
                    <tr key={item.id}>
                      <td>
                        <strong>{item.name}</strong>
                        <small>期限 {date(item.expires_at)}</small>
                      </td>
                      <td className="numeric">
                        {number(item.on_hand)}
                        <small>{item.unit}</small>
                      </td>
                      <td className="numeric subtle">
                        {number(item.reserved)}
                      </td>
                      <td className="numeric">
                        <strong
                          className={usable <= item.buffer ? "amber" : ""}
                        >
                          {number(usable)}
                        </strong>
                        {usable <= item.buffer && (
                          <small className="amber">バッファ以下</small>
                        )}
                      </td>
                      <td>
                        <button
                          className="icon-button"
                          aria-label={`${item.name}の在庫を編集`}
                          onClick={() =>
                            setAdjust({
                              id: item.id,
                              name: item.name,
                              unit: item.unit,
                              value: item.on_hand,
                              reserved: item.reserved,
                            })
                          }
                        >
                          <Settings2 size={16} />
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Panel>
        <Panel>
          <div className="section-heading">
            <div>
              <h2>入荷予定</h2>
              <p>実際の入荷数量を在庫に反映します。</p>
            </div>
            <Package size={19} />
          </div>
          <div className="delivery-list">
            {data.deliveries.map((delivery) => {
              const ingredient = data.inventory.find(
                (i) => i.id === delivery.ingredient_id,
              );
              return (
                <div className="delivery" key={delivery.id}>
                  <div className="delivery-icon">
                    <Package size={21} />
                  </div>
                  <div>
                    <strong>
                      {ingredient?.name || delivery.ingredient_id}
                    </strong>
                    <p>
                      {number(delivery.quantity)} {ingredient?.unit} ·{" "}
                      {date(delivery.arrives_at)} {time(delivery.arrives_at)}
                    </p>
                    <Badge
                      tone={
                        delivery.received
                          ? "green"
                          : delivery.confirmed
                            ? ""
                            : "amber"
                      }
                    >
                      {delivery.received
                        ? "入荷済み"
                        : delivery.confirmed
                          ? "入荷確定"
                          : "未確定"}
                    </Badge>
                  </div>
                  {!delivery.received && (
                    <button
                      className="text-button"
                      onClick={() =>
                        setReceive({
                          id: delivery.id,
                          name: ingredient?.name || delivery.ingredient_id,
                          unit: ingredient?.unit || "",
                          value: delivery.quantity,
                        })
                      }
                    >
                      検品
                      <ArrowRight size={13} />
                    </button>
                  )}
                </div>
              );
            })}
            {!data.deliveries.length && <Empty>入荷予定はありません。</Empty>}
          </div>
        </Panel>
      </div>
      <Panel className="menu-stock-panel">
        <div className="section-heading">
          <div>
            <h2>メニュー別の販売可能数</h2>
            <p>
              現在の在庫と販売制限に基づく数量。材料の共用により同時販売可能数とは異なります。
            </p>
          </div>
        </div>
        <div className="menu-stock-grid">
          {data.menus.map((menu) => (
            <div className="menu-stock" key={menu.id}>
              <span>{menu.emoji}</span>
              <strong>{menu.name}</strong>
              <div>
                {menu.available_units}
                <small>点</small>
              </div>
              {menu.limit_remaining !== null && (
                <Badge tone="amber">受付残り {menu.limit_remaining}点</Badge>
              )}
            </div>
          ))}
        </div>
      </Panel>
      {adjust && (
        <Modal
          title={`${adjust.name}の在庫調整`}
          onClose={() => setAdjust(null)}
        >
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              if (
                await act(
                  "/inventory-adjustments",
                  "POST",
                  { ingredient_id: adjust.id, on_hand: adjust.value },
                  "実在庫を更新しました。",
                )
              )
                setAdjust(null);
            }}
          >
            <p className="modal-description">
              棚卸しで確認した実際の数量を入力してください。予約済みの{" "}
              {adjust.reserved}
              {adjust.unit} は確保されます。
            </p>
            <label className="form-label">
              実在庫（{adjust.unit}）
              <input
                autoFocus
                required
                type="number"
                min={adjust.reserved}
                step="1"
                value={adjust.value}
                onChange={(e) =>
                  setAdjust({ ...adjust, value: Number(e.target.value) })
                }
              />
            </label>
            <div className="modal-actions">
              <button
                type="button"
                className="button"
                onClick={() => setAdjust(null)}
              >
                キャンセル
              </button>
              <button className="button primary" disabled={busy}>
                在庫を更新
              </button>
            </div>
          </form>
        </Modal>
      )}
      {receive && (
        <Modal
          title={`${receive.name}の入荷確認`}
          onClose={() => setReceive(null)}
        >
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              if (
                await act(
                  `/deliveries/${receive.id}/receive`,
                  "POST",
                  { quantity: receive.value },
                  "検品した数量を在庫に反映しました。",
                )
              )
                setReceive(null);
            }}
          >
            <p className="modal-description">
              実際に届いた数量を入力してください。予定と異なる場合も、実数を記録します。
            </p>
            <label className="form-label">
              入荷数量（{receive.unit}）
              <input
                autoFocus
                required
                type="number"
                min="0"
                step="1"
                value={receive.value}
                onChange={(e) =>
                  setReceive({ ...receive, value: Number(e.target.value) })
                }
              />
            </label>
            <div className="modal-actions">
              <button
                type="button"
                className="button"
                onClick={() => setReceive(null)}
              >
                キャンセル
              </button>
              <button className="button primary" disabled={busy}>
                入荷を確定
              </button>
            </div>
          </form>
        </Modal>
      )}
    </>
  );
}

function ClosePage(
  props: CommonProps & { onProposal: (proposal: Proposal) => void },
) {
  const { data, busy, act } = props;
  const [confirm, setConfirm] = useState(false);
  const [historyEdit, setHistoryEdit] = useState<
    Overview["history"][number] | null
  >(null);
  const close = data.closes.find(
    (c) => c.service_date === data.store.service_date,
  );
  const preparingCount = data.orders
    .flatMap((order) => order.tasks)
    .filter((task) => task.status === "preparing").length;
  const latest = data.proposals.find((p) => p.mode === "post_close");
  return (
    <>
      <SummaryMetrics data={data} />
      <div className="close-grid">
        <Panel>
          <div className="section-heading">
            <div>
              <h2>営業締め</h2>
              <p>完了した注文と在庫、販売制限の履歴を記録します。</p>
            </div>
            <ClipboardCheck size={20} />
          </div>
          <div className="close-body">
            {close ? (
              <>
                <span className="large-check">
                  <CheckCircle2 size={26} />
                </span>
                <h3>本日の営業締めが完了しました</h3>
                <p>
                  {time(close.closed_at || close.at)} 確定 · 完了{" "}
                  {close.completed_orders || 0}件 · 売上{" "}
                  {money(close.revenue || 0)}
                </p>
                <Badge tone={close.restricted ? "amber" : "green"}>
                  {close.restricted
                    ? "制限・未提供・短縮営業あり · 通常日の集計から除外"
                    : "販売制限なし"}
                </Badge>
                {close.notes && (
                  <ul className="notes close-notes">
                    {close.notes.map((note, index) => (
                      <li key={index}>{note}</li>
                    ))}
                  </ul>
                )}
              </>
            ) : (
              <>
                <span className="large-check neutral">
                  <Coffee size={27} />
                </span>
                <h3>本日の実績を確定</h3>
                <p>
                  {preparingCount > 0
                    ? `準備中の商品が ${preparingCount}点あります。準備中の作業を完了してから締めてください。`
                    : data.metrics.waiting_orders > 0
                      ? `未提供の注文 ${data.metrics.waiting_orders}件と予約材料を残して締めます。既存注文は締め後も準備できます。`
                      : "完了注文・在庫・販売制限の記録を確定します。"}
                </p>
                <button
                  className="button primary"
                  disabled={busy || preparingCount > 0}
                  onClick={() => setConfirm(true)}
                >
                  本日の営業を締める
                  <ArrowRight size={15} />
                </button>
              </>
            )}
          </div>
        </Panel>
        <Panel className="next-day-panel">
          <div className="section-heading">
            <div>
              <h2>翌日の準備計画</h2>
            </div>
            <CalendarDays size={21} />
          </div>
          <div className="next-day-body">
            <p>
              今日の実績と翌日のスタッフをもとに、準備する材料と追加発注量を見直します。
            </p>
            {!close && (
              <div className="field-help">
                本日の営業を締めると利用できます。
              </div>
            )}
            <GenerateButton {...props} busy={busy || !close} mode="post_close">
              翌日の準備を計算
            </GenerateButton>
            {latest && (
              <button
                className="text-button"
                onClick={() => props.onProposal(latest)}
              >
                直近の翌日プランを見る
                <ArrowRight size={15} />
              </button>
            )}
          </div>
        </Panel>
      </div>
      {close?.counts && (
        <Panel className="variance-panel">
          <div className="section-heading">
            <div>
              <h2>
                {close.forecast_source === "acknowledged_prep_plan"
                  ? "準備プランの予測と実績"
                  : "販売基準と実績"}
              </h2>
              <p>
                {close.forecast_source === "acknowledged_prep_plan"
                  ? "確認済みの準備プランと実績を比較しています。"
                  : "事前予測がないため、営業締め時点の過去販売基準と比較しています。"}
              </p>
            </div>
          </div>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>メニュー</th>
                  <th className="numeric">
                    {close.forecast_source === "acknowledged_prep_plan"
                      ? "準備案の予測"
                      : "過去販売基準"}
                  </th>
                  <th className="numeric">実績</th>
                  <th className="numeric">差分</th>
                </tr>
              </thead>
              <tbody>
                {data.menus.map((menu) => (
                  <tr key={menu.id}>
                    <td>
                      {menu.emoji} {menu.name}
                    </td>
                    <td className="numeric">
                      {close.forecast?.[menu.id] ?? "—"}
                    </td>
                    <td className="numeric">{close.counts?.[menu.id] || 0}</td>
                    <td className="numeric">
                      {close.variance?.[menu.id] != null
                        ? `${close.variance[menu.id] > 0 ? "+" : ""}${close.variance[menu.id]}`
                        : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      )}
      <Panel>
        <div className="section-heading">
          <div>
            <h2>販売履歴</h2>
            <p>
              初期履歴は合成サンプルデータです。営業締め後の実績もここに保存されます。
            </p>
          </div>
          <FileText size={18} />
        </div>
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>営業日</th>
                {data.menus.map((menu) => (
                  <th className="numeric" key={menu.id}>
                    {menu.name}
                  </th>
                ))}
                <th>販売制限</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {[...data.history]
                .sort((a, b) => b.date.localeCompare(a.date))
                .map((day) => (
                  <tr key={day.date}>
                    <td>
                      <strong>{day.date.slice(5).replace("-", "/")}</strong>
                    </td>
                    {data.menus.map((menu) => (
                      <td className="numeric" key={menu.id}>
                        {day.counts[menu.id] || 0}
                      </td>
                    ))}
                    <td>
                      <Badge tone={day.restricted ? "amber" : ""}>
                        {day.restricted ? "あり" : "なし"}
                      </Badge>
                    </td>
                    <td>
                      <button
                        className="icon-button"
                        aria-label={`${day.date}の販売記録を編集`}
                        onClick={() =>
                          setHistoryEdit({ ...day, counts: { ...day.counts } })
                        }
                      >
                        <Settings2 size={15} />
                      </button>
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      </Panel>
      {confirm && (
        <Modal title="本日の営業を締める" onClose={() => setConfirm(false)}>
          <p className="modal-description">
            完了 {data.metrics.completed_orders}件・売上{" "}
            {money(data.metrics.revenue)}{" "}
            と現在の在庫を記録します。締め後は本日の注文を追加できません。
            {data.metrics.waiting_orders > 0 &&
              ` 未提供 ${data.metrics.waiting_orders}件と予約材料を残して締めます。既存注文は引き続き準備できます。`}
          </p>
          <div className="modal-actions">
            <button className="button" onClick={() => setConfirm(false)}>
              キャンセル
            </button>
            <button
              className="button primary"
              disabled={busy}
              onClick={async () => {
                if (
                  await act(
                    "/closes",
                    "POST",
                    {},
                    "本日の営業実績を記録しました。",
                  )
                )
                  setConfirm(false);
              }}
            >
              営業を締める
            </button>
          </div>
        </Modal>
      )}
      {historyEdit && (
        <Modal
          title={`${historyEdit.date} の販売記録`}
          onClose={() => setHistoryEdit(null)}
        >
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              if (
                await act(
                  `/history/${historyEdit.date}`,
                  "PATCH",
                  {
                    counts: historyEdit.counts,
                    restricted: historyEdit.restricted,
                  },
                  "販売記録を更新しました。",
                )
              )
                setHistoryEdit(null);
            }}
          >
            {data.menus.map((menu) => (
              <label className="number-field" key={menu.id}>
                <span>
                  {menu.emoji} {menu.name}
                </span>
                <div>
                  <input
                    type="number"
                    min="0"
                    max="10000"
                    step="1"
                    value={historyEdit.counts[menu.id] || 0}
                    onChange={(e) =>
                      setHistoryEdit({
                        ...historyEdit,
                        counts: {
                          ...historyEdit.counts,
                          [menu.id]: Number(e.target.value),
                        },
                      })
                    }
                  />
                  <span>点</span>
                </div>
              </label>
            ))}
            <label className="check-field">
              <input
                type="checkbox"
                checked={historyEdit.restricted}
                onChange={(e) =>
                  setHistoryEdit({
                    ...historyEdit,
                    restricted: e.target.checked,
                  })
                }
              />
              品切れ・販売制限があった
            </label>
            <div className="modal-actions">
              <button
                type="button"
                className="button"
                onClick={() => setHistoryEdit(null)}
              >
                キャンセル
              </button>
              <button className="button primary" disabled={busy}>
                記録を保存
              </button>
            </div>
          </form>
        </Modal>
      )}
    </>
  );
}

function statusLabel(status: string) {
  return (
    (
      {
        ready: "確認待ち",
        stale: "再計算が必要",
        applied: "適用済み",
        acknowledged: "確認済み",
        failed: "生成失敗",
        waiting_input: "情報の確認が必要",
      } as Record<string, string>
    )[status] || status
  );
}
function ProposalModal({
  proposal,
  data,
  act,
  busy,
  onClose,
}: {
  proposal: Proposal;
  data: Overview;
  act: Action;
  busy: boolean;
  onClose: () => void;
}) {
  const [selected, setSelected] = useState(
    proposal.recommended_candidate_id ||
      proposal.candidates.find((c) => c.feasible)?.id ||
      "",
  );
  const canApply =
    proposal.status === "ready" && proposal.type === "service_plan";
  const candidate = proposal.candidates.find((c) => c.id === selected);
  const stale =
    proposal.status === "stale" ||
    (proposal.status === "ready" && proposal.revision !== data.revision);
  return (
    <Modal
      title={
        proposal.type === "service_plan"
          ? "営業プランを比較"
          : proposal.type === "prep_plan"
            ? "今日の仕込みプラン"
            : "翌日の準備プラン"
      }
      onClose={onClose}
      wide
    >
      <div className="proposal-meta">
        <Badge tone={proposal.source === "gemini" ? "green" : ""}>
          {proposal.source === "gemini"
            ? "Gemini による提案"
            : "計算結果 · AI未使用"}
        </Badge>
        <Badge>{statusLabel(proposal.status)}</Badge>
        <span>{time(proposal.created_at)} 作成</span>
      </div>
      <p className="proposal-summary">{proposal.summary}</p>
      {stale && (
        <div className="alert warning">
          <AlertTriangle size={16} />
          作成後にお店の状態が変わりました。営業画面から再計算してください。
        </div>
      )}
      {proposal.error && <div className="alert warning">{proposal.error}</div>}
      {proposal.questions?.map((question) => (
        <div className="alert" key={question}>
          <CircleHelp size={17} />
          {question}
        </div>
      ))}
      {proposal.candidates.length > 0 && (
        <>
          <div className="comparison-label">
            30分間の予測 · 候補ごとに同じ注文流入を使用
          </div>
          <div className="candidate-grid">
            {proposal.candidates.map((item) => (
              <CandidateCard
                key={item.id}
                candidate={item}
                selected={selected === item.id}
                recommended={proposal.recommended_candidate_id === item.id}
                menus={data.menus}
                onSelect={() => setSelected(item.id)}
              />
            ))}
          </div>
        </>
      )}
      {proposal.prep && (
        <>
          <div className="subsection-heading">
            <h3>{proposal.prep.service_date} の準備・発注候補</h3>
            <Badge>発注は自動実行しません</Badge>
          </div>
          <div className="forecast-chips">
            {Object.entries(proposal.prep.forecast).map(([id, count]) => (
              <span key={id}>
                {data.menus.find((m) => m.id === id)?.name || id}
                <small>需要予測 {count}点</small>
                <strong>
                  仕込み計画 {proposal.prep?.planned?.[id] ?? count}点
                </strong>
              </span>
            ))}
          </div>
          <p className="field-help">
            材料の必要量は、勤務体制で準備できる仕込み計画の数量をもとに計算しています。
          </p>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>材料</th>
                  <th className="numeric">必要量</th>
                  <th className="numeric">利用可</th>
                  <th className="numeric">入荷</th>
                  <th className="numeric">バッファ</th>
                  <th className="numeric">追加発注</th>
                </tr>
              </thead>
              <tbody>
                {proposal.prep.items.map((item) => (
                  <tr key={item.ingredient_id}>
                    <td>
                      <strong>{item.name}</strong>
                      <small>{item.unit}</small>
                    </td>
                    <td className="numeric">{number(item.needed)}</td>
                    <td className="numeric">{number(item.available)}</td>
                    <td className="numeric">{number(item.incoming)}</td>
                    <td className="numeric">{number(item.buffer)}</td>
                    <td className="numeric">
                      <strong className={item.to_order > 0 ? "amber" : ""}>
                        {number(item.to_order)}
                      </strong>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <ul className="notes">
            {proposal.prep.notes.map((note, i) => (
              <li key={i}>{note}</li>
            ))}
          </ul>
        </>
      )}
      <details className="trace">
        <summary>
          <Activity size={16} />
          {proposal.source === "gemini"
            ? "エージェントの実行記録"
            : "計算処理の実行記録"}
          <span>{proposal.trace.length}ステップ</span>
          <ChevronDown size={15} />
        </summary>
        <p className="field-help">
          実際に呼ばれたツールと返された結果です。モデル内部の思考ではありません。
        </p>
        <div className="trace-list">
          {proposal.trace.map((item, i) => (
            <div className="trace-item" key={`${item.step}-${i}`}>
              <span className="trace-number">{item.step}</span>
              <div>
                <div>
                  <strong>{item.tool}</strong>
                  <Badge tone={item.origin === "model" ? "green" : ""}>
                    {item.origin === "model" ? "AIが選択" : "サーバー"}
                  </Badge>
                </div>
                <p>
                  {typeof item.result_summary === "string"
                    ? item.result_summary
                    : JSON.stringify(item.result_summary)}
                </p>
                <details>
                  <summary>入力を表示</summary>
                  <pre>{JSON.stringify(item.args, null, 2)}</pre>
                </details>
              </div>
            </div>
          ))}
        </div>
      </details>
      <div className="modal-actions sticky-actions">
        <button className="button" onClick={onClose}>
          閉じる
        </button>
        {canApply && (
          <button
            className="button primary"
            disabled={busy || stale || !candidate?.feasible}
            onClick={async () => {
              if (
                await act(
                  `/proposals/${proposal.id}/approve`,
                  "POST",
                  {
                    candidate_id: selected,
                    expected_revision: proposal.revision,
                  },
                  "プランを承認し、販売設定に反映しました。",
                )
              )
                onClose();
            }}
          >
            <Check size={16} />
            選んだプランを承認・適用
          </button>
        )}
        {proposal.status === "ready" && proposal.type !== "service_plan" && (
          <button
            className="button primary"
            disabled={busy || stale}
            onClick={async () => {
              if (
                await act(
                  `/proposals/${proposal.id}/acknowledge`,
                  "POST",
                  {},
                  "準備プランを確認済みとして記録しました。",
                )
              )
                onClose();
            }}
          >
            <Check size={16} />
            プランを確認・記録
          </button>
        )}
      </div>
    </Modal>
  );
}
function CandidateCard({
  candidate: c,
  selected,
  recommended,
  menus,
  onSelect,
}: {
  candidate: Candidate;
  selected: boolean;
  recommended: boolean;
  menus: Overview["menus"];
  onSelect: () => void;
}) {
  return (
    <div
      className={`candidate ${selected ? "selected" : ""} ${!c.feasible ? "infeasible" : ""}`}
    >
      <button
        className="candidate-select"
        disabled={!c.feasible}
        aria-pressed={selected}
        onClick={onSelect}
      >
        <span className={`radio ${selected ? "checked" : ""}`}>
          {selected && <Check size={12} />}
        </span>
        <strong>{c.title}</strong>
        {recommended && <Badge tone="green">おすすめ</Badge>}
      </button>
      <div className="candidate-main">
        <span>最大待ち時間</span>
        <strong>
          {c.metrics.max_wait_minutes}
          <small>分</small>
        </strong>
      </div>
      <dl className="candidate-metrics">
        <div>
          <dt>販売金額</dt>
          <dd>{money(c.metrics.revenue)}</dd>
        </div>
        <div>
          <dt>完了 / 未完了</dt>
          <dd>
            {c.metrics.completed_units} / {c.metrics.pending_units}点
          </dd>
        </div>
        <div>
          <dt>受付を見送る商品</dt>
          <dd>{c.metrics.rejected_units}点</dd>
        </div>
        <div>
          <dt>既存注文の履行リスク</dt>
          <dd className={c.metrics.existing_at_risk > 0 ? "amber" : ""}>
            {c.metrics.existing_at_risk}点
          </dd>
        </div>
      </dl>
      <div className="candidate-limits">
        {c.action === "keep_current" ? (
          <span>販売設定を維持</span>
        ) : Object.entries(c.limits).length ? (
          Object.entries(c.limits).map(([id, limit]) => (
            <span key={id}>
              {menus.find((m) => m.id === id)?.name || id}:{" "}
              {limit === 0 ? "受付停止" : `${limit}点まで`}
            </span>
          ))
        ) : (
          <span>販売制限を解除</span>
        )}
        <small>{c.review_after_minutes}分後に見直し</small>
      </div>
      <ul className="notes">
        {c.notes.map((note, i) => (
          <li key={i}>{note}</li>
        ))}
      </ul>
      {!c.feasible && <Badge tone="red">この条件では適用できません</Badge>}
    </div>
  );
}

function OrderPage({ data, act, busy }: CommonProps) {
  const [cart, setCart] = useState<Record<string, number>>({});
  const [category, setCategory] = useState("all");
  const [receipt, setReceipt] = useState<{
    number: number | string;
    total: number;
  } | null>(null);
  const closed = data.closes.some(
    (c) => c.service_date === data.store.service_date,
  );
  const total = Object.entries(cart).reduce(
    (sum, [id, count]) =>
      sum + (data.menus.find((m) => m.id === id)?.price || 0) * count,
    0,
  );
  const count = Object.values(cart).reduce((sum, n) => sum + n, 0);
  const cap = (menu: Overview["menus"][number]) =>
    Math.max(
      0,
      Math.min(menu.available_units, menu.limit_remaining ?? Infinity),
    );
  const update = (id: string, change: number) => {
    setReceipt(null);
    setCart((c) => {
      const next = { ...c, [id]: Math.max(0, (c[id] || 0) + change) };
      if (!next[id]) delete next[id];
      return next;
    });
  };
  async function order() {
    const result = await act<{
      order: { number: number | string; total: number };
    }>("/orders", "POST", {
      items: Object.entries(cart)
        .filter(([, quantity]) => quantity > 0)
        .map(([menu_id, quantity]) => ({ menu_id, quantity })),
    });
    if (result) {
      setCart({});
      setReceipt(result.order);
    }
  }
  return (
    <>
      {closed && (
        <div className="alert warning">
          <Clock3 size={17} />
          本日の営業は終了しました。サンプル店舗をリセットすると、最初から試せます。
        </div>
      )}
      <div className="ordering-grid">
        <div>
          <div className="section-toolbar">
            <div className="segmented">
              {[
                ["all", "すべて"],
                ["drink", "ドリンク"],
                ["food", "フード"],
              ].map(([id, label]) => (
                <button
                  key={id}
                  className={category === id ? "selected" : ""}
                  onClick={() => setCategory(id)}
                >
                  {label}
                </button>
              ))}
            </div>
            <span className="subtle">{data.store.name}</span>
          </div>
          <div className="order-menu-grid">
            {data.menus
              .filter((m) => category === "all" || m.category === category)
              .map((menu) => (
                <Panel
                  className={`menu-item ${cap(menu) === 0 ? "unavailable" : ""}`}
                  key={menu.id}
                >
                  <div className={`menu-illustration ${menu.category}`}>
                    <span>{menu.emoji}</span>
                    <span className="illustration-circle" />
                  </div>
                  <div className="menu-item-body">
                    <div className="small-label">
                      {menu.category === "drink"
                        ? "DRINKS"
                        : "FROM THE KITCHEN"}
                    </div>
                    <h2>{menu.name}</h2>
                    <p>
                      {cap(menu) === 0
                        ? "受付停止中"
                        : `現在 ${cap(menu)}点までご注文いただけます`}
                    </p>
                    <div>
                      <strong>{money(menu.price)}</strong>
                      <button
                        className="add-button"
                        aria-label={`${menu.name}を追加`}
                        disabled={
                          busy || closed || (cart[menu.id] || 0) >= cap(menu)
                        }
                        onClick={() => update(menu.id, 1)}
                      >
                        <Plus size={18} />
                      </button>
                    </div>
                  </div>
                </Panel>
              ))}
          </div>
        </div>
        <Panel className="cart-panel">
          <div className="section-heading">
            <h2>注文内容</h2>
            <Badge>{count}点</Badge>
          </div>
          {receipt ? (
            <div className="receipt">
              <CheckCircle2 size={35} />
              <h3>ご注文を受け付けました。</h3>
              <p>受付番号</p>
              <strong>#{String(receipt.number).padStart(3, "0")}</strong>
              <span>合計 {money(receipt.total)}</span>
              <p>
                店長画面の注文一覧で
                <br />
                準備の進行を確認できます。
              </p>
            </div>
          ) : count === 0 ? (
            <Empty>
              気になるメニューを
              <br />
              選んでください。
            </Empty>
          ) : (
            <div className="cart-items">
              {Object.entries(cart).map(([id, qty]) => {
                const menu = data.menus.find((m) => m.id === id)!;
                return (
                  <div className="cart-item" key={id}>
                    <div>
                      <span>{menu.emoji}</span>
                      <strong>{menu.name}</strong>
                      <small>{money(menu.price * qty)}</small>
                    </div>
                    <div className="quantity-control">
                      <button
                        aria-label={`${menu.name}を減らす`}
                        onClick={() => update(id, -1)}
                      >
                        <Minus size={13} />
                      </button>
                      <span>{qty}</span>
                      <button
                        aria-label={`${menu.name}を増やす`}
                        disabled={qty >= cap(menu)}
                        onClick={() => update(id, 1)}
                      >
                        <Plus size={13} />
                      </button>
                    </div>
                    {qty > cap(menu) && (
                      <p className="amber">
                        販売可能数が変わりました。数量を減らしてください。
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
          )}
          <div className="cart-total">
            <span>
              合計<small>登録価格</small>
            </span>
            <strong>{money(total)}</strong>
          </div>
          <div className="cart-submit">
            <button
              className="button primary full"
              disabled={
                busy ||
                closed ||
                !count ||
                Object.entries(cart).some(
                  ([id, qty]) =>
                    qty > cap(data.menus.find((m) => m.id === id)!),
                )
              }
              onClick={order}
            >
              {busy ? (
                <LoaderCircle className="spin" size={17} />
              ) : (
                <ShoppingBag size={17} />
              )}
              注文を確定する
            </button>
            <p>
              サンプル注文のため、お支払いは発生しません。
              <br />
              在庫は確定時に確保されます。
            </p>
          </div>
        </Panel>
      </div>
    </>
  );
}
