import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, expect, it, vi } from "vitest";
import HistoryPanel from "../node/components/HistoryPanel.vue";
import * as api from "../node/api";

vi.mock("../node/api", async (original) => ({
  ...(await original<typeof api>()),
  nodeRequest: vi.fn(),
}));
const session = { actor_id: "a", csrf_token: "csrf", expires_at: "2099-01-01" };
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});
const button = (w: ReturnType<typeof mount>, text: string) =>
  w.findAll("button").find((b) => b.text() === text)!;

it("combines filters, retains queried conditions for paging and exports every match", async () => {
  vi.mocked(api.nodeRequest).mockResolvedValue({ items: [], has_more: true });
  const fetchMock = vi.fn().mockResolvedValue(new Response(new Blob(["xlsx"])));
  vi.stubGlobal("fetch", fetchMock);
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:history");
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  const w = mount(HistoryPanel, { props: { session } });
  await flushPromises();
  await w.get('[aria-label="股票代码"]').setValue("000001");
  await w.get('[aria-label="买入开始日期"]').setValue("2026-10-01");
  await w.get('[aria-label="卖出结束日期"]').setValue("2026-10-23");
  await w.get("form.history-filters").trigger("submit");
  await flushPromises();
  const params = "symbol=000001&buy_from=2026-10-01&sell_to=2026-10-23";
  expect(vi.mocked(api.nodeRequest).mock.calls.at(-1)![0]).toBe(
    `history?${params}&offset=0&limit=50`,
  );
  await w.get('[aria-label="股票代码"]').setValue("600000");
  await button(w, "下一页").trigger("click");
  await flushPromises();
  expect(vi.mocked(api.nodeRequest).mock.calls.at(-1)![0]).toBe(
    `history?${params}&offset=50&limit=50`,
  );
  await button(w, "导出 Excel").trigger("click");
  await flushPromises();
  expect(fetchMock.mock.calls[0]![0]).toBe(`/api/v2/node/history?${params}&export=true`);
  w.unmount();
});

it("rejects reversed dates before querying and clears private data when logged out", async () => {
  vi.mocked(api.nodeRequest).mockResolvedValue({ items: [], has_more: false });
  const w = mount(HistoryPanel, { props: { session } });
  await flushPromises();
  await w.get('[aria-label="买入开始日期"]').setValue("2026-10-23");
  await w.get('[aria-label="买入结束日期"]').setValue("2026-10-01");
  await w.get("form.history-filters").trigger("submit");
  expect(w.text()).toContain("开始日期不能晚于结束日期");
  expect(api.nodeRequest).toHaveBeenCalledTimes(1);
  await w.setProps({ session: null });
  expect(button(w, "导出 Excel").attributes("disabled")).toBeDefined();
  w.unmount();
});

it("uses the configured API access and never asks for backend account credentials", async () => {
  vi.mocked(api.nodeRequest).mockResolvedValue({ items: [], has_more: false });
  const w = mount(HistoryPanel, { props: { session } });
  await flushPromises();
  expect(api.nodeRequest).toHaveBeenCalledTimes(1);
  expect(vi.mocked(api.nodeRequest).mock.calls[0]![0]).toContain("history?");
  expect(w.find('input[type="password"]').exists()).toBe(false);
  expect(w.text()).not.toContain("量化后台账户");
  expect(w.text()).not.toContain("登录量化后台");
  w.unmount();
});

it.each(["QUANT_ACCESS_REQUIRED", "HISTORY_ACCESS_REJECTED"])(
  "shows a settings error for %s without requesting admin login",
  async (code) => {
    vi.mocked(api.nodeRequest).mockRejectedValue(new api.NodeApiError(code, 503));
    const w = mount(HistoryPanel, { props: { session } });
    await flushPromises();
    expect(w.text()).toContain("系统设置");
    expect(w.find('input[type="password"]').exists()).toBe(false);
    expect(w.text()).not.toContain("没有符合条件的历史记录");
    expect(w.emitted("expired")).toBeUndefined();
    w.unmount();
  },
);

it("shows board exclusion reason instead of waiting or a generic skipped label", async () => {
  vi.mocked(api.nodeRequest).mockResolvedValue({
    items: [
      {
        execution_id: "excluded",
        identity: { symbol: "301413.SZ" },
        name: "安培龙",
        first_buy_at: null,
        last_sell_at: null,
        bought_quantity: 0,
        sold_quantity: 0,
        remaining_quantity: 0,
        buy_average_price: null,
        sell_average_price: null,
        buy_amount: "0.00",
        sell_amount: "0.00",
        buy_status: "SKIPPED",
        sell_status: "NOT_STARTED",
        reason_code: "BOARD_NOT_ALLOWED",
      },
    ],
    has_more: false,
  });
  const w = mount(HistoryPanel, { props: { session } });
  await flushPromises();
  expect(w.text()).toContain("不符合板块选择，已剔除");
  expect(w.text()).not.toContain("等待买入");
  w.unmount();
});
