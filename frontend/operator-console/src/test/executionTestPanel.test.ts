import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ExecutionTestPanel from "../node/components/ExecutionTestPanel.vue";
import { NodeApiError } from "../node/api";
import * as api from "../node/api";

vi.mock("../node/api", async (original) => ({
  ...(await original<typeof api>()),
  nodeRequest: vi.fn(),
}));
const session = { actor_id: "operator", csrf_token: "local-only", expires_at: "2099-01-01" };
const blank = (): {
  state: string;
  busy: boolean;
  error: string | null;
  selection_date: string | null;
  maximum: number;
  amount: string;
  verified_at: string | null;
  uploaded_events: number;
  phases: string[];
  rows: object[];
} => ({
  state: "IDLE",
  busy: false,
  error: null,
  selection_date: null,
  maximum: 3,
  amount: "2000.00",
  verified_at: null,
  uploaded_events: 0,
  phases: [],
  rows: [] as object[],
});
let current: ReturnType<typeof blank>;
let calls: { path: string; method: string; body: object | undefined }[];
const wrappers: ReturnType<typeof mount>[] = [];
const render = (loggedIn = true) => {
  const wrapper = mount(ExecutionTestPanel, { props: { session: loggedIn ? session : null } });
  wrappers.push(wrapper);
  return wrapper;
};
function button(wrapper: ReturnType<typeof render>, label: string) {
  return wrapper.findAll("button").find((b) => b.text() === label)!;
}
beforeEach(() => {
  vi.useFakeTimers();
  current = blank();
  calls = [];
  vi.mocked(api.nodeRequest).mockImplementation(
    async <T>(path: string, method = "GET", body?: object) => {
      calls.push({ path, method, body });
      if (method === "POST") {
        const action = (body as { action: string }).action;
        if (action === "FETCH")
          current = {
            ...current,
            state: "READY",
            selection_date: "2026-10-14",
            phases: [],
            rows: [{ symbol: "002317.SZ", name: "众生药业", execution: null, events: [] }],
          };
        else current = { ...current, state: "UPLOADING", busy: true };
      }
      return structuredClone(current) as T;
    },
  );
});
afterEach(() => {
  wrappers.forEach((w) => w.unmount());
  wrappers.length = 0;
  vi.useRealTimers();
  vi.clearAllMocks();
});
describe("今日交易中的测试交易", () => {
  it("shows actual historical stock immediately and keeps simulation separate", async () => {
    const wrapper = render();
    await flushPromises();
    expect(wrapper.text()).toContain("不会向QMT发单");
    expect(wrapper.text()).toContain("这些价格不是历史行情或真实成交");
    await button(wrapper, "获取测试股票").trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("002317.SZ");
    expect(wrapper.text()).toContain("众生药业");
    expect(wrapper.text()).toContain("2026-10-14");
    expect(wrapper.text()).toContain("等待模拟");
    expect(calls.filter((c) => c.method === "POST").map((c) => c.body)).toEqual([
      { action: "FETCH" },
    ]);
    expect(calls.every((c) => c.path === "execution-test")).toBe(true);
    await button(wrapper, "运行模拟并回传").trigger("click");
    await flushPromises();
    expect(button(wrapper, "获取测试股票").attributes("disabled")).toBeDefined();
    current = {
      ...current,
      state: "SYNCED",
      busy: false,
      verified_at: "2026-10-23T15:00:00+08:00",
      rows: [
        {
          symbol: "002317.SZ",
          name: "众生药业",
          execution: {
            buy_status: "FILLED",
            sell_status: "FILLED",
            bought_quantity: 100,
            sold_quantity: 100,
            buy_average_price: "10.00000000",
            buy_amount: "1000.00",
            first_buy_at: "2026-10-14T14:51:00+08:00",
          },
          events: [
            {
              seq: 1,
              occurred_at: "2026-10-14T14:51:00+08:00",
              fact: {
                kind: "ORDER",
                side: "BUY",
                status: "REJECTED",
                reason_code: "TEST_BUY_REJECTED",
              },
            },
          ],
        },
      ],
    };
    await vi.advanceTimersByTimeAsync(1000);
    await flushPromises();
    expect(wrapper.text()).toContain("已核验");
    expect(wrapper.text()).toContain("10.00000000");
    await button(wrapper, "详情").trigger("click");
    expect(wrapper.text()).toContain("模拟买入失败");
    expect(wrapper.text()).toContain("模拟卖出100股");
    expect(button(wrapper, "再次核验回传").attributes("disabled")).toBeUndefined();
  });
  it("requires login, clears private test rows on logout and cancels polling on unmount", async () => {
    const wrapper = render(false);
    await flushPromises();
    expect(calls).toHaveLength(0);
    expect(button(wrapper, "获取测试股票").attributes("disabled")).toBeDefined();
    await wrapper.setProps({ session });
    await flushPromises();
    await button(wrapper, "获取测试股票").trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("众生药业");
    await wrapper.setProps({ session: null });
    await flushPromises();
    expect(wrapper.text()).not.toContain("众生药业");
  });
  it("offers retry without buying again and handles expired sessions", async () => {
    current = {
      ...current,
      state: "ERROR",
      error: "TEST_REQUEST_FAILED",
      rows: [
        {
          symbol: "002317.SZ",
          name: "众生药业",
          execution: null,
          events: [{ seq: 1, fact: { kind: "CANDIDATE" } }],
        },
      ],
    };
    const wrapper = render();
    await flushPromises();
    expect(wrapper.text()).toContain("测试请求或回查未通过");
    await button(wrapper, "重试回传").trigger("click");
    await flushPromises();
    expect(calls.some((c) => (c.body as { action?: string })?.action === "RETRY")).toBe(true);
    vi.mocked(api.nodeRequest).mockRejectedValue(new NodeApiError("OPERATOR_SESSION_EXPIRED", 401));
    await vi.advanceTimersByTimeAsync(1000);
    await flushPromises();
    expect(wrapper.emitted("expired")).toHaveLength(1);
  });
  it("does not repeat a timed-out mutation and shows empty and configuration states", async () => {
    const wrapper = render();
    await flushPromises();
    vi.mocked(api.nodeRequest).mockImplementation(async <T>(path: string, method = "GET") => {
      if (method === "POST") throw new NodeApiError("CONNECTION_LOST_OR_TIMEOUT");
      return { ...blank(), state: "ERROR", error: "TEST_CONNECTION_REQUIRED" } as T;
    });
    await button(wrapper, "获取测试股票").trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("测试连接尚未配置");
    vi.mocked(api.nodeRequest).mockResolvedValue({ ...blank(), state: "EMPTY" });
    await button(wrapper, "刷新测试结果").trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("最近366天没有可用");
  });
});
