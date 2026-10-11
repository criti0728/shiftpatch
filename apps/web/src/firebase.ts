import type { FirebaseConfig } from "./types";

export async function googleIdToken(config: FirebaseConfig): Promise<string> {
  const [
    { initializeApp, getApps },
    {
      getAuth,
      GoogleAuthProvider,
      signInWithPopup,
      setPersistence,
      browserSessionPersistence,
    },
  ] = await Promise.all([import("firebase/app"), import("firebase/auth")]);
  const app =
    getApps().find((item) => item.name === "shiftpatch") ||
    initializeApp(config, "shiftpatch");
  const auth = getAuth(app);
  await setPersistence(auth, browserSessionPersistence);
  const provider = new GoogleAuthProvider();
  provider.setCustomParameters({ prompt: "select_account" });
  const result = await signInWithPopup(auth, provider);
  return result.user.getIdToken();
}

export async function signOutFirebase() {
  const [{ getApps }, { getAuth, signOut }] = await Promise.all([
    import("firebase/app"),
    import("firebase/auth"),
  ]);
  const app = getApps().find((item) => item.name === "shiftpatch");
  if (app) await signOut(getAuth(app));
}

export function loginError(error: unknown): string {
  const code = (error as { code?: string })?.code;
  if (
    code === "auth/popup-closed-by-user" ||
    code === "auth/cancelled-popup-request"
  )
    return "ログインをキャンセルしました。サンプル店舗はそのまま利用できます。";
  if (code === "auth/popup-blocked")
    return "ログイン画面を開けませんでした。このサイトのポップアップを許可して、もう一度お試しください。";
  if (code === "auth/unauthorized-domain")
    return "このサイトのログイン設定が完了していません。サンプル店舗はログインせずに利用できます。";
  return error instanceof Error
    ? error.message
    : "ログインできませんでした。もう一度お試しください。";
}
