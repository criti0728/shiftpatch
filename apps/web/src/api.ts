import type { Session } from "./types";

let activeStoreId: string | null = null;
let sessionVersion = 0;
const pendingKeys = new Map<string, string>();
const prefix = "shiftpatch:pending-request:";

export function selectStore(storeId: string | null) {
  if (activeStoreId !== storeId) sessionVersion += 1;
  activeStoreId = storeId;
}

export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

// A retry after an ambiguous response must use the same operation identity,
// including after a tab reload. Body key order must not change that identity.
function canonical(value: unknown): string {
  return (
    JSON.stringify(value, (_key, child) => {
      if (child && typeof child === "object" && !Array.isArray(child)) {
        return Object.fromEntries(
          Object.keys(child)
            .sort()
            .map((key) => [key, child[key]]),
        );
      }
      return child;
    }) || "null"
  );
}
function pendingKey(signature: string): string {
  let key = pendingKeys.get(signature);
  try {
    key = sessionStorage.getItem(prefix + signature) || key;
  } catch {
    /* Private storage can be unavailable. The in-memory fallback still deduplicates. */
  }
  if (!key) key = crypto.randomUUID();
  pendingKeys.set(signature, key);
  try {
    sessionStorage.setItem(prefix + signature, key);
  } catch {
    /* Keep the in-memory key. */
  }
  return key;
}
function clearPending(signature: string) {
  pendingKeys.delete(signature);
  try {
    sessionStorage.removeItem(prefix + signature);
  } catch {
    /* Storage is optional. */
  }
}

async function fetchApi<T>(
  url: string,
  method = "GET",
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  method = method.toUpperCase();
  const mutation = method !== "GET" && method !== "HEAD";
  // Session exchanges carry a short-lived ID token. Keep retry identities only
  // for store commands, so authentication tokens never enter our own storage.
  const tracked = mutation && url.startsWith("/api/stores/");
  const signature = `${method}:${url}:${canonical(body)}`;
  const key = tracked ? pendingKey(signature) : undefined;
  let response: Response;
  try {
    response = await fetch(url, {
      method,
      credentials: "same-origin",
      signal,
      headers: {
        "Content-Type": "application/json",
        ...(key ? { "Idempotency-Key": key } : {}),
      },
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") throw error;
    throw new ApiError(
      "NETWORK_ERROR",
      mutation
        ? "通信が途切れ、操作結果を確認できませんでした。同じ内容で再試行してください。重複を避けるため同じ操作キーを使用します。"
        : "サーバーに接続できません。APIサーバーが起動しているか確認してください。",
    );
  }
  let data: unknown;
  try {
    data = await response.json();
  } catch {
    throw new ApiError(
      "INVALID_RESPONSE",
      "サーバーから正しい応答を受け取れませんでした。同じ内容で再試行できます。",
    );
  }
  if (!response.ok) {
    const detail = (data as { detail?: unknown } | null)?.detail;
    const structured =
      detail && typeof detail === "object" && !Array.isArray(detail)
        ? (detail as { code?: string; message?: string })
        : undefined;
    const code = structured?.code || "REQUEST_FAILED";
    // A definitive client error did not apply the operation. In-progress runs
    // remain unresolved; retrying them must not launch a second run.
    if (
      tracked &&
      response.status >= 400 &&
      response.status < 500 &&
      code !== "run_in_progress"
    )
      clearPending(signature);
    throw new ApiError(
      code,
      structured?.message ||
        (typeof detail === "string"
          ? detail
          : "操作を完了できませんでした。入力内容を確認してください。"),
    );
  }
  if (tracked) clearPending(signature);
  return data as T;
}

export async function request<T>(
  path: string,
  method = "GET",
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const storeId = activeStoreId;
  const version = sessionVersion;
  if (!storeId)
    throw new ApiError("session_required", "サンプル店舗を開いてください。");
  const result = await fetchApi<T>(
    `/api/stores/${encodeURIComponent(storeId)}${path}`,
    method,
    body,
    signal,
  );
  if (activeStoreId !== storeId || sessionVersion !== version)
    throw new ApiError("session_changed", "利用する店舗が切り替わりました。");
  return result;
}

export function getSession(signal?: AbortSignal) {
  return fetchApi<Session>("/api/session", "GET", undefined, signal);
}
export function startGuest() {
  return fetchApi<Session>("/api/session/guest", "POST", {});
}
export function exchangeFirebase(idToken: string) {
  return fetchApi<Session>("/api/session/firebase", "POST", {
    id_token: idToken,
  });
}
export function endSession() {
  return fetchApi<Session>("/api/session", "DELETE");
}
