import { beforeEach, expect, it, vi } from "vitest";

const storage = new Map<string, string>();
const fetchMock = vi.fn<typeof fetch>();
beforeEach(async () => {
  vi.resetModules();
  storage.clear();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => storage.get(key) || null,
    setItem: (key: string, value: string) => storage.set(key, value),
    removeItem: (key: string) => storage.delete(key),
  });
  const { selectStore } = await import("./api");
  selectStore("sample-a");
});
function keyAt(index: number) {
  return (fetchMock.mock.calls[index][1]?.headers as Record<string, string>)[
    "Idempotency-Key"
  ];
}
const ok = () =>
  new Response(JSON.stringify({ order: { number: 4 } }), { status: 200 });

it("reuses an unresolved operation key after network loss and a module reload", async () => {
  const first = await import("./api");
  fetchMock.mockRejectedValueOnce(new Error("connection lost"));
  await expect(
    first.request("/orders", "POST", {
      items: [{ menu_id: "latte", quantity: 1 }],
    }),
  ).rejects.toMatchObject({ code: "NETWORK_ERROR" });
  expect(storage.size).toBe(1);
  vi.resetModules();
  const afterReload = await import("./api");
  afterReload.selectStore("sample-a");
  fetchMock.mockResolvedValueOnce(ok());
  await afterReload.request("/orders", "POST", {
    items: [{ quantity: 1, menu_id: "latte" }],
  });
  expect(keyAt(1)).toBe(keyAt(0));
  expect(storage.size).toBe(0);
  fetchMock.mockResolvedValueOnce(ok());
  await afterReload.request("/orders", "POST", {
    items: [{ menu_id: "latte", quantity: 1 }],
  });
  expect(keyAt(2)).not.toBe(keyAt(1));
});

it.each([503, 500])(
  "keeps the key after ambiguous server error %s",
  async (status) => {
    const { request } = await import("./api");
    fetchMock
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            detail: { code: "server_error", message: "retry" },
          }),
          { status },
        ),
      )
      .mockResolvedValueOnce(ok());
    await expect(request("/orders", "POST", { items: [] })).rejects.toThrow(
      "retry",
    );
    await request("/orders", "POST", { items: [] });
    expect(keyAt(1)).toBe(keyAt(0));
  },
);

it("keeps the key when the response body is unreadable", async () => {
  const { request } = await import("./api");
  fetchMock
    .mockResolvedValueOnce(new Response("truncated body", { status: 200 }))
    .mockResolvedValueOnce(ok());
  await expect(request("/orders", "POST", {})).rejects.toMatchObject({
    code: "INVALID_RESPONSE",
  });
  await request("/orders", "POST", {});
  expect(keyAt(1)).toBe(keyAt(0));
});

it("clears keys for definitive validation errors", async () => {
  const { request } = await import("./api");
  fetchMock
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          detail: { code: "insufficient_stock", message: "在庫不足" },
        }),
        { status: 409 },
      ),
    )
    .mockResolvedValueOnce(ok());
  await expect(request("/orders", "POST", {})).rejects.toThrow("在庫不足");
  await request("/orders", "POST", {});
  expect(keyAt(1)).not.toBe(keyAt(0));
});

it("reuses the key while an agent run is still in progress", async () => {
  const { request } = await import("./api");
  fetchMock
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          detail: { code: "run_in_progress", message: "実行中" },
        }),
        { status: 409 },
      ),
    )
    .mockResolvedValueOnce(ok());
  await expect(
    request("/proposals", "POST", { mode: "in_service" }),
  ).rejects.toThrow("実行中");
  await request("/proposals", "POST", { mode: "in_service" });
  expect(keyAt(1)).toBe(keyAt(0));
});

it("does not attach an operation key to read-only requests", async () => {
  const { request } = await import("./api");
  fetchMock.mockResolvedValueOnce(ok());
  await request("/overview");
  expect(keyAt(0)).toBeUndefined();
  expect(storage.size).toBe(0);
});

it("routes all operations to the authenticated sample and includes its cookie", async () => {
  const { request, selectStore } = await import("./api");
  selectStore("guest store/one");
  fetchMock.mockResolvedValueOnce(ok());
  await request("/overview");
  expect(fetchMock.mock.calls[0][0]).toBe(
    "/api/stores/guest%20store%2Fone/overview",
  );
  expect(fetchMock.mock.calls[0][1]?.credentials).toBe("same-origin");
});

it("never falls back to the shared demo when no session is open", async () => {
  const { request, selectStore } = await import("./api");
  selectStore(null);
  await expect(request("/overview")).rejects.toMatchObject({
    code: "session_required",
  });
  expect(fetchMock).not.toHaveBeenCalled();
});

it("isolates retries between samples and resumes the original sample's unresolved operation", async () => {
  const { request, selectStore } = await import("./api");
  fetchMock.mockRejectedValueOnce(new Error("offline"));
  await expect(request("/orders", "POST", { items: [] })).rejects.toThrow();
  selectStore("sample-b");
  fetchMock.mockResolvedValueOnce(ok());
  await request("/orders", "POST", { items: [] });
  expect(keyAt(1)).not.toBe(keyAt(0));
  expect(fetchMock.mock.calls[1][0]).toBe("/api/stores/sample-b/orders");
  selectStore("sample-a");
  fetchMock.mockResolvedValueOnce(ok());
  await request("/orders", "POST", { items: [] });
  expect(keyAt(2)).toBe(keyAt(0));
  expect(storage.size).toBe(0);
});

it("rejects a response from the previous sample after switching accounts", async () => {
  const { request, selectStore } = await import("./api");
  let resolve!: (value: Response) => void;
  fetchMock.mockReturnValueOnce(
    new Promise<Response>((done) => {
      resolve = done;
    }),
  );
  const pending = request("/overview");
  selectStore("sample-b");
  resolve(ok());
  await expect(pending).rejects.toMatchObject({ code: "session_changed" });
});

it("rejects an old response even when logout is followed by resuming the same guest", async () => {
  const { request, selectStore } = await import("./api");
  let resolve!: (value: Response) => void;
  fetchMock.mockReturnValueOnce(
    new Promise<Response>((done) => {
      resolve = done;
    }),
  );
  const pending = request("/overview");
  selectStore(null);
  selectStore("sample-a");
  resolve(ok());
  await expect(pending).rejects.toMatchObject({ code: "session_changed" });
});

it("bootstraps and exchanges sessions without an active store or bearer token in store requests", async () => {
  const { getSession, startGuest, exchangeFirebase, endSession, selectStore } =
    await import("./api");
  selectStore(null);
  fetchMock.mockImplementation(
    async () =>
      new Response(
        JSON.stringify({ authenticated: false, firebase_enabled: false }),
        { status: 200 },
      ),
  );
  await getSession();
  await startGuest();
  await exchangeFirebase("verified-by-server");
  await endSession();
  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
    "/api/session",
    "/api/session/guest",
    "/api/session/firebase",
    "/api/session",
  ]);
  expect(fetchMock.mock.calls[2][1]?.body).toBe(
    JSON.stringify({ id_token: "verified-by-server" }),
  );
  expect(fetchMock.mock.calls[3][1]?.method).toBe("DELETE");
  for (const [, options] of fetchMock.mock.calls) {
    expect(options?.credentials).toBe("same-origin");
    expect(options?.headers).not.toHaveProperty("Authorization");
  }
  expect(storage.size).toBe(0);
});

it("preserves request cancellation so switching samples can cancel their pending queries", async () => {
  const { request } = await import("./api");
  const controller = new AbortController();
  const aborted = new DOMException("query cancelled", "AbortError");
  fetchMock.mockRejectedValueOnce(aborted);
  await expect(
    request("/overview", "GET", undefined, controller.signal),
  ).rejects.toBe(aborted);
  expect(fetchMock.mock.calls[0][1]?.signal).toBe(controller.signal);
});
