import { useEffect, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  ArrowRight,
  Check,
  Coffee,
  LoaderCircle,
  PanelsTopLeft,
  RefreshCw,
  ShieldCheck,
  Store,
  Users,
} from "lucide-react";
import {
  endSession,
  exchangeFirebase,
  getSession,
  selectStore,
  startGuest,
} from "./api";
import { googleIdToken, loginError, signOutFirebase } from "./firebase";
import type { AuthenticatedSession, Session } from "./types";

type Access = {
  session: AuthenticatedSession;
  onLogout: () => Promise<void>;
  onSignIn: () => Promise<void>;
  sessionBusy: boolean;
};
const syncKey = "shiftpatch:session-updated";

export default function SessionGate({
  children,
}: {
  children: (access: Access) => ReactNode;
}) {
  const client = useQueryClient();
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const sessionQuery = useQuery({
    queryKey: ["session"],
    queryFn: ({ signal }) => getSession(signal),
    enabled: !busy,
    refetchInterval: 15000,
    retry: 1,
  });
  const session = sessionQuery.data;
  const storeId = session?.authenticated ? session.store.id : null;
  // Choose the store before mounting its workspace. Never use a shared fallback.
  selectStore(storeId);

  useEffect(() => {
    const otherStores = {
      predicate: (query: { queryKey: readonly unknown[] }) =>
        query.queryKey[0] === "overview" && query.queryKey[1] !== storeId,
    };
    // Expiry and switches from another tab use the same isolation as logout.
    void client.cancelQueries(otherStores);
    client.removeQueries(otherStores);
  }, [client, storeId]);

  useEffect(() => {
    const update = (event: StorageEvent) => {
      if (event.key === syncKey) {
        setNotice(null);
        selectStore(null);
        void client.cancelQueries({ queryKey: ["overview"] });
        client.removeQueries({ queryKey: ["overview"] });
        void client.resetQueries({ queryKey: ["session"] });
      }
    };
    window.addEventListener("storage", update);
    return () => window.removeEventListener("storage", update);
  }, [client]);

  async function accept(next: Session) {
    await client.cancelQueries({ queryKey: ["overview"] });
    client.removeQueries({ queryKey: ["overview"] });
    selectStore(next.authenticated ? next.store.id : null);
    client.setQueryData(["session"], next);
    try {
      localStorage.setItem(syncKey, crypto.randomUUID());
    } catch {
      /* Cross-tab sync is optional. */
    }
    window.scrollTo({ top: 0, behavior: "auto" });
  }
  async function run(kind: string, operation: () => Promise<Session>) {
    if (busy) return;
    setBusy(kind);
    setNotice(null);
    await client.cancelQueries({ queryKey: ["session"] });
    try {
      await accept(await operation());
    } catch (error) {
      setNotice(
        kind === "login"
          ? loginError(error)
          : error instanceof Error
            ? error.message
            : "接続できませんでした。もう一度お試しください。",
      );
      // An interrupted exchange may have set the cookie; read the actual session.
      void client.invalidateQueries({ queryKey: ["session"] });
    } finally {
      setBusy("");
    }
  }
  async function login() {
    if (!session?.firebase_enabled || !session.firebase_config) return;
    const config = session.firebase_config;
    await run("login", async () =>
      exchangeFirebase(await googleIdToken(config)),
    );
  }
  async function logout() {
    const wasFirebase =
      session?.authenticated && session.user.kind === "firebase";
    await run("logout", async () => {
      const next = await endSession();
      if (wasFirebase) await signOutFirebase().catch(() => undefined);
      return next;
    });
  }

  if (session?.authenticated)
    return (
      <>
        {notice && (
          <div className="session-notice" role="alert">
            {notice}
            <button onClick={() => setNotice(null)} aria-label="通知を閉じる">
              閉じる
            </button>
          </div>
        )}
        {children({
          session,
          onLogout: logout,
          onSignIn: login,
          sessionBusy: Boolean(busy),
        })}
      </>
    );

  return (
    <div className="entry-shell">
      <header className="entry-header">
        <a href="/" className="entry-brand">
          <span>
            <PanelsTopLeft size={22} />
          </span>
          ShiftPatch
        </a>
        <span>店舗運営を、試してから。</span>
      </header>
      <main className="entry-main">
        <section className="entry-intro">
          <span className="entry-eyebrow">CAFE OPERATIONS WORKSPACE</span>
          <h1>
            あなた専用の
            <br />
            サンプル店舗で、
            <br />
            今日の運営を試す。
          </h1>
          <p>
            注文の流れ、スタッフの配置、在庫を見ながら、カフェの販売戦略を組み直す。登録なしで、すべての操作を体験できます。
          </p>
          <div className="entry-benefits">
            <span>
              <Check size={16} />
              ほかの人とデータを共有しません
            </span>
            <span>
              <Check size={16} />
              変更を保存し、あとで続きを試せます
            </span>
            <span>
              <Check size={16} />
              いつでも初期状態に戻せます
            </span>
          </div>
          <div className="entry-stats">
            <span>
              <strong>1</strong>あなた専用の店舗
            </span>
            <span>
              <strong>4</strong>スタッフ
            </span>
            <span>
              <strong>6</strong>メニュー
            </span>
          </div>
        </section>
        <section className="entry-access" aria-label="サンプル店舗へのアクセス">
          <div className="entry-store-icon">
            <Store size={27} />
          </div>
          <span className="entry-card-label">SAMPLE STORE</span>
          <h2>まずは、店長になってみる。</h2>
          <p>
            スタッフ・メニュー・在庫が設定済みのカフェを用意します。実際の注文や支払いは発生しません。
          </p>
          {sessionQuery.isPending ? (
            <div className="entry-loading" role="status">
              <LoaderCircle className="spin" size={19} />
              利用状況を確認しています…
            </div>
          ) : sessionQuery.isError && !session ? (
            <div className="entry-error" role="alert">
              <p>{sessionQuery.error.message}</p>
              <button className="button" onClick={() => sessionQuery.refetch()}>
                <RefreshCw size={15} />
                再接続
              </button>
            </div>
          ) : (
            <>
              <button
                className="button primary entry-start"
                disabled={Boolean(busy)}
                onClick={() => run("guest", startGuest)}
              >
                {busy === "guest" ? (
                  <LoaderCircle size={18} className="spin" />
                ) : (
                  <Coffee size={18} />
                )}
                {busy === "guest"
                  ? "店舗を開いています…"
                  : session?.has_saved_guest
                    ? "保存したサンプル店舗に戻る"
                    : "サンプル店舗を体験"}
                {!busy && <ArrowRight size={17} />}
              </button>
              <small className="entry-save-note">
                ゲストの店舗はこのブラウザに紐づいて保存されます。ブラウザのデータを削除すると戻れなくなります。
              </small>
              <div className="entry-separator">
                <span>アカウントで続きを保存</span>
              </div>
              <button
                className="button entry-google"
                disabled={
                  Boolean(busy) ||
                  !session?.firebase_enabled ||
                  !session?.firebase_config
                }
                onClick={login}
              >
                {busy === "login" ? (
                  <LoaderCircle className="spin" size={17} />
                ) : (
                  <span className="google-letter" aria-hidden="true">
                    G
                  </span>
                )}
                {busy === "login" ? "ログイン中…" : "Google でログイン"}
              </button>
              <small className="entry-save-note">
                {session?.firebase_enabled && session?.firebase_config
                  ? "ログインすると、別のブラウザからも自分のサンプル店舗に戻れます。"
                  : "Google ログインは、この環境ではまだ設定されていません。ゲストで全機能を試せます。"}
              </small>
            </>
          )}
          {notice && (
            <p className="entry-error" role="alert">
              {notice}
            </p>
          )}
          <div className="entry-isolation">
            <ShieldCheck size={17} />
            <span>
              店舗の操作は、あなたのサンプルデータにだけ反映されます。
            </span>
          </div>
        </section>
        <section className="entry-flow" aria-label="体験できる操作">
          <div>
            <Coffee size={20} />
            <span>
              <strong>注文を入れる</strong>
              <small>注文画面から、カフェの混雑を作る。</small>
            </span>
          </div>
          <div>
            <Users size={20} />
            <span>
              <strong>現場の条件を変える</strong>
              <small>欠勤や仕込み不足を反映する。</small>
            </span>
          </div>
          <div>
            <Activity size={20} />
            <span>
              <strong>運営案を比較する</strong>
              <small>売上と待ち時間を見て、提案を適用する。</small>
            </span>
          </div>
        </section>
      </main>
      <footer className="entry-footer">
        ShiftPatch · サンプル店舗の体験環境
      </footer>
    </div>
  );
}
