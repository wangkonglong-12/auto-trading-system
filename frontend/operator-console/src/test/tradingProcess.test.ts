import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import TradingProcessPanel from "../node/components/TradingProcessPanel.vue";
import type { TradingProcess } from "../node/consoleTypes";

function process(): TradingProcess {
  const stocks = [
    { symbol: "003021.SZ", name: "兆威机电" },
    { symbol: "000001.SZ", name: "平安银行" },
    { symbol: "600580.SH", name: "卧龙电驱" },
  ];
  return {
    day: "2026-10-27",
    stream_id: "stream-a",
    warning: null,
    retained: 4,
    maximum: 500,
    stocks,
    events: [
      {
        seq: 1,
        occurred_at: "2026-10-27T14:40:00+08:00",
        level: "INFO",
        symbol: null,
        name: "",
        message: "开始获取选股",
      },
      ...stocks.map((item, i) => ({
        ...item,
        seq: i + 2,
        occurred_at: "2026-10-27T14:40:01+08:00",
        level: ["INFO", "ERROR", "SUCCESS"][i] as "INFO" | "ERROR" | "SUCCESS",
        message: ["券商回报全部成交，等待成交核对", "委托被拒绝", "买入成功：实际成交100股"][i]!,
      })),
    ],
  };
}

function panel() {
  return mount(TradingProcessPanel, {
    props: { process: process(), loggedIn: true, stateLabel: "运行中" },
  });
}

describe("trading process window", () => {
  it("lists every code and name and keeps stock identity on each stock record", () => {
    const wrapper = panel();
    expect(wrapper.get('[aria-label="交易股票名单"]').text()).toContain("共 3 只股票");
    for (const item of process().stocks) {
      expect(wrapper.get('[aria-label="交易股票名单"]').text()).toContain(item.symbol);
      expect(wrapper.get('[aria-label="交易股票名单"]').text()).toContain(item.name);
      const line = wrapper
        .findAll(".process-line")
        .find((row) => row.text().includes(item.symbol))!;
      expect(line.text()).toContain(item.name);
    }
    expect(wrapper.findAll(".process-line")[0]!.text()).toContain("系统");
    expect(wrapper.findAll(".process-error")).toHaveLength(1);
    expect(wrapper.get(".process-error").text()).toContain("委托被拒绝");
    expect(wrapper.findAll(".process-success")).toHaveLength(1);
    expect(wrapper.get(".process-success").text()).toContain("实际成交100股");
    expect(wrapper.get(".process-info:nth-child(2)").text()).toContain("等待成交核对");
    wrapper.unmount();
  });

  it("filters records and resets filters when the account/day stream changes", async () => {
    const wrapper = panel();
    await wrapper.get('[aria-label="筛选交易过程股票"]').setValue("000001.SZ");
    expect(wrapper.findAll(".process-line")).toHaveLength(1);
    await wrapper.get('[aria-label="筛选交易过程内容"]').setValue("SUCCESS");
    expect(wrapper.findAll(".process-line")).toHaveLength(0);
    await wrapper.setProps({ process: { ...process(), stream_id: "stream-b" } });
    expect(wrapper.findAll(".process-line")).toHaveLength(4);
    wrapper.unmount();
  });

  it("keeps explicit scrolling pause on updates and can return to the latest record", async () => {
    const wrapper = panel();
    const viewport = wrapper.get(".process-viewport").element as HTMLElement;
    Object.defineProperty(viewport, "scrollHeight", { value: 1000 });
    await wrapper.get("button").trigger("click");
    viewport.scrollTop = 100;
    await wrapper.setProps({
      process: { ...process(), events: [...process().events, { ...process().events[0]!, seq: 5 }] },
    });
    await flushPromises();
    expect(viewport.scrollTop).toBe(100);
    expect(wrapper.get("button").text()).toBe("回到最新");
    await wrapper.get("button").trigger("click");
    await flushPromises();
    expect(viewport.scrollTop).toBe(1000);
    expect(wrapper.get("button").text()).toBe("暂停滚动");
    wrapper.unmount();
  });

  it("shows transient read warnings outside the event log and clears them on recovery", async () => {
    const wrapper = panel();
    const warning = "页面暂时无法读取交易引擎状态，等待刷新核对";
    await wrapper.setProps({ process: { ...process(), warning } });
    expect(wrapper.get('[role="status"]').text()).toBe(warning);
    expect(wrapper.get('[role="log"]').text()).not.toContain(warning);
    expect(wrapper.findAll(".process-line")).toHaveLength(4);
    await wrapper.setProps({ process: process() });
    expect(wrapper.find('[role="status"]').exists()).toBe(false);
    expect(wrapper.findAll(".process-line")).toHaveLength(4);
    expect(wrapper.get(".process-success").text()).toContain("实际成交100股");
    wrapper.unmount();
  });

  it("hides account records after logout", async () => {
    const wrapper = panel();
    await wrapper.setProps({ loggedIn: false });
    expect(wrapper.find(".process-line").exists()).toBe(false);
    expect(wrapper.text()).not.toContain("003021.SZ");
    expect(wrapper.text()).toContain("登录后查看");
    wrapper.unmount();
  });
});
