import { flushPromises, mount } from "@vue/test-utils";
import type { VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ManualSellDialog from "../node/components/ManualSellDialog.vue";
import SellStockAction from "../node/components/SellStockAction.vue";

const now = Date.parse("2026-10-28T02:00:00Z");
const proposal = {
  operation_id: "manual-sell-" + "a".repeat(32),
  symbol: "600698.SH",
  name: "湖南天雁",
  limit_price: "7.53",
  quantity: 600,
  amount: "4518.00",
  child_count: 2,
  expires_at: new Date(now + 30_000).toISOString(),
};
const position = {
  symbol: "600698.SH",
  name: "湖南天雁",
  remaining: 600,
  sellable: 600,
  reserved: 0,
  sold_today: 0,
  sell_amount: "0",
  average_sell_price: null,
  eligible_sell_date: "2026-10-28",
  can_sell: true,
  disabled_reason: null,
  state: "HOLDING",
};
const props = {
  accountId: "account-a",
  ready: true,
  position,
  session: {
    actor_id: "operator-a",
    csrf_token: "csrf",
    expires_at: new Date(now + 3_600_000).toISOString(),
  },
};
let wrapper: VueWrapper;
let confirmation: () => Promise<Response>;
const calls: { path: string; body: Record<string, unknown> }[] = [];
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
function start() {
  wrapper = mount(ManualSellDialog, { props, attachTo: document.body });
  return wrapper;
}
async function click(label: string) {
  await wrapper
    .findAll("button")
    .find((b) => b.text() === label)!
    .trigger("click");
  await flushPromises();
}
async function prepare() {
  await wrapper.get('[aria-label="卖出价格"]').setValue("7.53");
  await click("全部可卖");
  await wrapper.get("form").trigger("submit");
  await flushPromises();
}
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(now);
  sessionStorage.clear();
  calls.length = 0;
  confirmation = async () =>
    response({ operation_id: proposal.operation_id, status: "PERSISTING", children: [] });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init: RequestInit) => {
      calls.push({ path, body: JSON.parse(String(init.body)) });
      if (path.endsWith("/preview")) return response(proposal);
      if (path.endsWith("/confirm")) return confirmation();
      if (path.endsWith("/result"))
        return response({ operation_id: proposal.operation_id, status: "SUBMITTED", children: [] });
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

describe("manual stock sale", () => {
  it("freezes the stock and exact price/quantity before a separate confirmation", async () => {
    start();
    await prepare();
    expect(wrapper.text()).toContain("湖南天雁");
    expect(wrapper.text()).toContain("拆为 2 笔委托");
    expect(calls).toHaveLength(1);
    expect(calls[0]!.body).toEqual({ symbol: "600698.SH", limit_price: "7.53", quantity: 600 });
    await click("确认卖出");
    expect(calls[1]!.body).toEqual({ operation_id: proposal.operation_id });
    expect(wrapper.emitted("submitted")).toEqual([["PERSISTING"]]);
    expect(sessionStorage.getItem("ats:manual-sell:account-a:600698.SH")).toBe(
      proposal.operation_id,
    );
  });
  it("prevents duplicate confirmation while the first request is unresolved", async () => {
    confirmation = () => new Promise(() => {});
    start();
    await prepare();
    await click("确认卖出");
    expect(wrapper.findAll("button").every((b) => b.attributes("disabled") !== undefined)).toBe(
      true,
    );
    expect(calls.filter((c) => c.path.endsWith("/confirm"))).toHaveLength(1);
  });
  it("looks up a pending operation after remount without sending again", async () => {
    sessionStorage.setItem("ats:manual-sell:account-a:600698.SH", proposal.operation_id);
    start();
    await flushPromises();
    expect(calls.map((c) => c.path.split("/").at(-1))).toEqual(["result"]);
    expect(sessionStorage.getItem("ats:manual-sell:account-a:600698.SH")).toBeNull();
    expect(wrapper.find("form").exists()).toBe(true);
  });
  it("rejects more shares than the current sellable count and expires previews", async () => {
    start();
    await wrapper.get('[aria-label="卖出价格"]').setValue("7.53");
    await wrapper.get('[aria-label="卖出数量"]').setValue("601");
    await wrapper.get("form").trigger("submit");
    await flushPromises();
    expect(calls).toHaveLength(0);
    await prepare();
    await vi.advanceTimersByTimeAsync(31_000);
    expect(wrapper.text()).toContain("本次核对已过期");
    await click("确认卖出");
    expect(calls).toHaveLength(1);
  });
  it("explains T+1 and permits cancellation of an existing owned sell order", async () => {
    const action = mount(SellStockAction, {
      props: {
        symbol: position.symbol,
        position: { ...position, can_sell: false, disabled_reason: "T_PLUS_ONE", sellable: 0 },
        ready: true,
        cancelDisabled: false,
        orders: [
          {
            client_order_id: "ord_sell0001",
            symbol: position.symbol,
            side: "SELL",
            status: "ACKED",
            quantity: 100,
            filled: 0,
            remaining: 100,
            limit_price: "7.53",
            owned_by_system: true,
            cancelable: true,
          },
        ],
      },
    });
    expect(action.text()).toContain("尚未到可卖日期");
    expect(action.findAll("button")[0]!.attributes("disabled")).toBeDefined();
    await action.findAll("button")[1]!.trigger("click");
    expect(action.emitted("cancel")).toEqual([["ord_sell0001"]]);
    action.unmount();
  });
});
