import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { errorText, nodeRequest, NodeApiError } from "../node/api";
import { pendingKey, readPending, writePending } from "../node/pending";
const identity = "control-" + "a".repeat(32);
const key = pendingKey("node-a", "account-a");
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
beforeEach(() => {
  vi.useFakeTimers();
  sessionStorage.clear();
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});
describe("bounded API and request tracking", () => {
  it("shows the backend field error instead of blaming all failures on trading time", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        response(
          {
            code: "INVALID_ARGUMENTS",
            issues: [
              { field: "safety", message: "启用自动买入时，请填写当日最大亏损。" },
              { field: 123 },
            ],
          },
          400,
        ),
      ),
    );
    const error = await nodeRequest("trading-settings", "POST", {}).catch((e: unknown) => e);
    expect(errorText(error)).toBe("启用自动买入时，请填写当日最大亏损。");
    expect(errorText(new NodeApiError("INVALID_ARGUMENTS"))).not.toContain("午休");
  });
  it("aborts timeout, external cancellation, and pre-aborted requests", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_url, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            if (init.signal?.aborted) reject(new Error("aborted"));
            else init.signal?.addEventListener("abort", () => reject(new Error("aborted")));
          }),
      ),
    );
    const request = nodeRequest("status").catch((e) => e);
    await vi.advanceTimersByTimeAsync(18000);
    expect(await request).toBeInstanceOf(NodeApiError);
    const controller = new AbortController();
    const external = nodeRequest("status", "GET", undefined, undefined, controller.signal).catch(
      (e) => e,
    );
    controller.abort();
    expect(await external).toBeInstanceOf(NodeApiError);
    await expect(
      nodeRequest("status", "GET", undefined, undefined, controller.signal),
    ).rejects.toThrow("CONNECTION_LOST_OR_TIMEOUT");
  });
  it("normalizes protocol errors and never exposes thrown transport text", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => response({}, 503)),
    );
    await expect(nodeRequest("status")).rejects.toThrow("REQUEST_FAILED");
    expect(errorText(new Error("sensitive"))).not.toContain("sensitive");
    expect(errorText(new NodeApiError("CONSOLE_BUSY"))).toContain("控制通道正忙");
    expect(errorText(new NodeApiError("OPERATOR_LOGIN_RATE_LIMITED"))).toContain("登录尝试过多");
  });
  it("retains only IDs scoped to node and account, and detects silent storage loss", () => {
    writePending(key, identity);
    expect(readPending(key)).toBe(identity);
    expect(readPending(pendingKey("other", "account-a"))).toBe("");
    writePending(key, "");
    expect(readPending(key)).toBe("");
    vi.stubGlobal("sessionStorage", { getItem: () => null, setItem: () => {} });
    expect(() => writePending(key, identity)).toThrow("not retained");
  });
});
