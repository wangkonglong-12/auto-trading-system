import { flushPromises, mount } from "@vue/test-utils";
import type { VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ManualBuyDialog from "../node/components/ManualBuyDialog.vue";

const now = Date.parse("2026-10-26T02:00:00Z");
const proposal = {
  preview_id: "manual-" + "a".repeat(32),
  client_order_id: "ord_manual0001",
  symbol: "000001.SZ",
  name: "平安银行",
  limit_price: "10",
  quantity: 100,
  amount: "1000.00",
  reserved_amount: "1005.00",
  cash: "10000.00",
  expires_at: new Date(now + 30_000).toISOString(),
};
const props = {
  accountId: "account-a",
  ready: true,
  cash: "10000.00",
  session: {
    actor_id: "operator-a",
    csrf_token: "csrf",
    expires_at: new Date(now + 3600_000).toISOString(),
  },
};
let wrapper: VueWrapper;
const calls: { path: string; body: Record<string, unknown> }[] = [];
let confirm: () => Promise<Response>;
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
function start() {
  wrapper = mount(ManualBuyDialog, { props, attachTo: document.body });
  return wrapper;
}
async function click(text: string) {
  await wrapper
    .findAll("button")
    .find((b) => b.text() === text)!
    .trigger("click");
  await flushPromises();
}
async function prepare() {
  await wrapper.get('[aria-label="股票代码"]').setValue("000001");
  await wrapper.get('[aria-label="买入价格"]').setValue("10");
  await wrapper.get('[aria-label="买入数量"]').setValue("100");
  await wrapper.get("form").trigger("submit");
  await flushPromises();
}
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(now);
  sessionStorage.clear();
  calls.length = 0;
  confirm = async () => response({ status: "PERSISTING" });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init: RequestInit) => {
      calls.push({ path: input, body: JSON.parse(String(init.body)) });
      if (input.endsWith("/preview")) return response(proposal);
      if (input.endsWith("/confirm")) return confirm();
      if (input.endsWith("/result")) return response({ status: "SDK_ACCEPTED" });
      throw new Error("unexpected route");
    }),
  );
});
afterEach(() => {
  wrapper?.unmount();
  document.body.innerHTML = "";
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("manual purchase confirmation", () => {
  it("freezes code price quantity and shows resolved name before sending an order", async () => {
    start();
    await prepare();
    expect(wrapper.text()).toContain("平安银行");
    expect(wrapper.text()).toContain("卖出规则待配置");
    expect(calls.map((c) => c.path.split("/").at(-1))).toEqual(["preview"]);
    expect(calls[0]!.body).toEqual({ symbol: "000001", limit_price: "10", quantity: 100 });
    await click("确认买入");
    expect(calls[1]!.body).toEqual({ preview_id: proposal.preview_id });
    expect(wrapper.emitted("submitted")).toHaveLength(1);
    expect(sessionStorage.getItem("ats:manual-buy:account-a")).toBeNull();
  });
  it("cannot send a second confirmation while the first is unresolved", async () => {
    confirm = () => new Promise(() => {});
    start();
    await prepare();
    await click("确认买入");
    expect(
      wrapper
        .findAll("button")
        .find((b) => b.text() === "查询本次结果")!
        .attributes("disabled"),
    ).toBeDefined();
    expect(calls.filter((c) => c.path.endsWith("/confirm"))).toHaveLength(1);
  });
  it("remembers uncertain submission across a page reload and only queries its original ID", async () => {
    confirm = async () => {
      throw new Error("lost receipt");
    };
    start();
    await prepare();
    await click("确认买入");
    expect(wrapper.text()).toContain("勿重复提交");
    expect(sessionStorage.getItem("ats:manual-buy:account-a")).toBe(proposal.client_order_id);
    wrapper.unmount();
    start();
    await flushPromises();
    expect(wrapper.find('[aria-label="股票代码"]').exists()).toBe(false);
    await click("查询本次结果");
    expect(calls.at(-1)!.body).toEqual({ client_order_id: proposal.client_order_id });
    expect(calls.filter((c) => c.path.endsWith("/confirm"))).toHaveLength(1);
  });
  it("returns to editing after an authoritative rejection without trapping a pending request", async () => {
    confirm = async () => response({ code: "MANUAL_BUY_REJECTED" }, 400);
    start();
    await prepare();
    await click("确认买入");
    expect(wrapper.find('[aria-label="股票代码"]').exists()).toBe(true);
    expect(wrapper.text()).toContain("未提交委托");
    expect(sessionStorage.getItem("ats:manual-buy:account-a")).toBeNull();
  });
  it("disables confirmation on expiration or loss of readiness", async () => {
    start();
    await prepare();
    await wrapper.setProps({ ready: false });
    expect(
      wrapper
        .findAll("button")
        .find((b) => b.text() === "确认买入")!
        .attributes("disabled"),
    ).toBeDefined();
    await wrapper.setProps({ ready: true });
    await vi.advanceTimersByTimeAsync(31_000);
    expect(wrapper.text()).toContain("本次核对已过期");
    expect(calls.filter((c) => c.path.endsWith("/confirm"))).toHaveLength(0);
  });
  it("rejects zero price and noninteger share counts without requesting a preview", async () => {
    start();
    await wrapper.get('[aria-label="股票代码"]').setValue("000001");
    await wrapper.get('[aria-label="买入价格"]').setValue("0");
    await wrapper.get('[aria-label="买入数量"]').setValue("100.5");
    await wrapper.get("form").trigger("submit");
    await flushPromises();
    expect(calls).toHaveLength(0);
  });
});
