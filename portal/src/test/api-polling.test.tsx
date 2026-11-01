// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, useApi, useControllerStatus } from "../api";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
const response = (data: unknown) => new Response(JSON.stringify(data), { status: 200 });
const expired = () => new Response(JSON.stringify({ detail: "Session expired" }), { status: 401 });
const flush = () => act(async () => { await Promise.resolve(); });
const advance = (milliseconds: number) => act(async () => { await vi.advanceTimersByTimeAsync(milliseconds); });

beforeEach(() => vi.useFakeTimers());
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });

describe("bounded API polling", () => {
  it("does not overlap reads, times out a hung refresh and retains its error/data until a successful retry", async () => {
    const hung = deferred<Response>();
    const fetch = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(response({ value: "last known" }))
      .mockImplementation(() => hung.promise);
    const { result } = renderHook(() => useApi<{ value: string }>("/overview", 1_000));
    await flush();
    expect(result.current.data?.value).toBe("last known");

    await advance(6_000);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(result.current.error).toBeNull();
    await advance(10_000);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls[1][1]?.signal?.aborted).toBe(true);
    expect(result.current.data?.value).toBe("last known");
    expect(result.current.error).toBe("Connect did not respond in time. Try again.");
    // A fetch implementation that ignores abort cannot turn that failure green.
    await act(async () => hung.resolve(response({ value: "late stale success" })));
    expect(result.current.error).toBe("Connect did not respond in time. Try again.");
    expect(result.current.data?.value).toBe("last known");

    fetch.mockResolvedValue(response({ value: "fresh" }));
    await advance(1_000);
    expect(result.current).toMatchObject({ data: { value: "fresh" }, error: null });
  });

  it("bounds body parsing as well as the wait for response headers", async () => {
    const body = deferred<{ healthy: boolean }>();
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true, status: 200, json: () => body.promise,
    } as Response);
    const { result } = renderHook(() => useApi<{ healthy: boolean }>("/overview", 0));
    await flush();
    await advance(15_000);
    expect(fetch.mock.calls[0][1]?.signal?.aborted).toBe(true);
    expect(result.current.data).toBeNull();
    expect(result.current.error).toBe("Connect did not respond in time. Try again.");
    await act(async () => body.resolve({ healthy: true }));
    expect(result.current.data).toBeNull();
    expect(result.current.error).toBeTruthy();
  });

  it("cancels a superseded reload and ignores its late completion", async () => {
    const older = deferred<Response>();
    const newer = deferred<Response>();
    const fetch = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(response({ value: "initial" }))
      .mockImplementationOnce(() => older.promise)
      .mockImplementationOnce(() => newer.promise);
    const { result } = renderHook(() => useApi<{ value: string }>("/overview", 0));
    await flush();
    act(() => result.current.reload());
    act(() => result.current.reload());
    expect(fetch.mock.calls[1][1]?.signal?.aborted).toBe(true);
    expect(result.current.data?.value).toBe("initial");
    await act(async () => newer.resolve(response({ value: "newer" })));
    await act(async () => older.resolve(response({ value: "older" })));
    expect(result.current).toMatchObject({ data: { value: "newer" }, error: null });
  });

  it("masks old scope data immediately and rejects an old-path completion after the path changes", async () => {
    const older = deferred<Response>();
    const next = deferred<Response>();
    const fetch = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(response({ organisation: "a" }))
      .mockImplementationOnce(() => older.promise)
      .mockImplementationOnce(() => next.promise);
    const { result, rerender } = renderHook(({ path }) => useApi<{ organisation: string }>(path, 0), {
      initialProps: { path: "/overview?customer_id=a" as string | null },
    });
    await flush();
    act(() => result.current.reload());
    rerender({ path: "/overview?customer_id=b" });
    expect(result.current).toMatchObject({ data: null, error: null });
    expect(fetch.mock.calls[1][1]?.signal?.aborted).toBe(true);
    await act(async () => next.resolve(response({ organisation: "b" })));
    await act(async () => older.resolve(response({ organisation: "late a" })));
    expect(result.current.data?.organisation).toBe("b");
    rerender({ path: null });
    expect(result.current).toMatchObject({ data: null, error: null });
  });

  it("cancels an unmounted read and removes its retry/deadline timers", async () => {
    const pending = deferred<Response>();
    const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(() => pending.promise);
    const { unmount } = renderHook(() => useApi("/overview", 1_000));
    unmount();
    expect(fetch.mock.calls[0][1]?.signal?.aborted).toBe(true);
    await advance(30_000);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
    await act(async () => pending.resolve(response({ healthy: true })));
  });
});

describe("controller reachability polling", () => {
  it("withdraws reachability on a hung version refresh and restores it only after a fresh response", async () => {
    const hung = deferred<Response>();
    const fetch = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(response({ version: "v1" }))
      .mockImplementation(() => hung.promise);
    const { result } = renderHook(() => useControllerStatus());
    await flush();
    expect(result.current).toEqual({ ok: true, version: "v1" });
    expect(fetch.mock.calls[0][0]).toBe("/api/v1/version");
    expect(fetch.mock.calls[0][1]?.headers).toBeUndefined();
    expect(fetch.mock.calls[0][1]?.credentials).toBeUndefined();
    await advance(25_000);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(result.current).toEqual({ ok: false });
    expect(fetch.mock.calls[1][1]?.signal?.aborted).toBe(true);
    await act(async () => hung.resolve(response({ version: "late v1" })));
    expect(result.current).toEqual({ ok: false });
    fetch.mockResolvedValue(response({ version: "v2" }));
    await advance(5_000);
    expect(result.current).toEqual({ ok: true, version: "v2" });
  });
});

describe("read cancellation and session expiry", () => {
  it("does not let a cancelled old-path GET sign out the session when its ignored-abort response arrives late", async () => {
    const older = deferred<Response>();
    const dispatch = vi.spyOn(window, "dispatchEvent");
    vi.spyOn(globalThis, "fetch").mockImplementationOnce(() => older.promise).mockResolvedValue(response({ value: "fresh" }));
    const { result, rerender } = renderHook(({ path }) => useApi<{ value: string }>(path, 0), { initialProps: { path: "/overview?customer_id=a" } });
    rerender({ path: "/overview?customer_id=b" });
    await flush();
    await act(async () => older.resolve(expired()));
    expect(result.current).toMatchObject({ data: { value: "fresh" }, error: null });
    expect(dispatch.mock.calls.filter(([event]) => event.type === "exa-signed-out")).toHaveLength(0);
  });

  it("still signs out on an active GET's 401", async () => {
    const dispatch = vi.spyOn(window, "dispatchEvent");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(expired());
    const { result } = renderHook(() => useApi("/overview", 0));
    await flush();
    expect(result.current.error).toBe("Session expired");
    expect(dispatch.mock.calls.filter(([event]) => event.type === "exa-signed-out")).toHaveLength(1);
  });

  it("preserves write request expiry behavior even if its caller aborts", async () => {
    const pending = deferred<Response>();
    const dispatch = vi.spyOn(window, "dispatchEvent");
    vi.spyOn(globalThis, "fetch").mockImplementation(() => pending.promise);
    const controller = new AbortController();
    const write = api("/customers/a/rules", { method: "POST", signal: controller.signal, body: "{}" });
    const rejected = expect(write).rejects.toThrow("Session expired");
    controller.abort();
    pending.resolve(expired());
    await rejected;
    expect(dispatch.mock.calls.filter(([event]) => event.type === "exa-signed-out")).toHaveLength(1);
  });
});
