import { flushPromises, mount } from "@vue/test-utils";
import type { VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import NodeConsole from "../node/NodeConsole.vue";
import { errorText, nodeRequest, NodeApiError } from "../node/api";
import type { NodeStatus, Preview } from "../node/api";
import { pendingKey, readPending, writePending } from "../node/pending";

const identity = "control-" + "a".repeat(32);
const key = pendingKey("node-a", "account-a");
const now = Date.parse("2026-09-24T06:30:00Z");
const session = {
  actor_id: "operator-a",
  csrf_token: "csrf-test",
  expires_at: "2026-09-24T07:30:00Z",
};
let current: NodeStatus;
let proposal: Preview;
let loggedIn = true;
let wrapper: VueWrapper | undefined;
type Handler = (path: string, init: RequestInit) => Promise<Response>;
let extra: Handler | undefined;
const calls: { path: string; init: RequestInit }[] = [];
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });

function button(label: string) {
  const found = wrapper!.findAll("button").find((item) => item.text() === label);
  if (!found) throw new Error(`button missing: ${label}`);
  return found;
}
async function click(label: string) {
  await button(label).trigger("click");
  await flushPromises();
}
async function start() {
  wrapper = mount(NodeConsole);
  await flushPromises();
  return wrapper;
}
async function prepare() {
  await click("确认恢复…");
  await wrapper!.get("#confirm-phrase").setValue(proposal.confirmation_phrase);
}
async function confirm() {
  await wrapper!.get(".preview-panel form").trigger("submit");
  await flushPromises();
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(now);
  sessionStorage.clear();
  calls.length = 0;
  loggedIn = true;
  extra = undefined;
  const obs = {
    availability: "CURRENT" as const,
    age_seconds: 0,
    observed_at: new Date(now).toISOString(),
    error: null,
  };
  current = {
    node_id: "node-a",
    account_id: "account-a",
    broker: "MOCK",
    real_trading_enabled: false,
    strategy_status: "UNCONFIRMED",
    operator_access_configured: true,
    components: {
      engine: {
        ...obs,
        data: {
          state: "REVIEW_REQUIRED",
          recovery: "MATCHED",
          journal_healthy: true,
          trade_date: "2026-09-24",
          pause_reason: "STARTUP",
          plan: { plan_id: "plan-a", strategy_id: "baseline", symbols: ["600000.SH"] },
        },
      },
      supervisor: { ...obs, data: { action: "HEALTHY", failures: 0, reason: null } },
      agent: { ...obs, data: { status: "IDLE", last_result: { status: "SYNCED" } } },
    },
  };
  proposal = {
    preview_id: "preview-" + "b".repeat(32),
    confirmation_phrase: "CONFIRM PAPER CONFIRM_RECOVERY bbbbbbbb",
    actor_id: "operator-a",
    account_id: "account-a",
    trade_date: "2026-09-24",
    permission_epoch: 1,
    request: {
      request_id: identity,
      action: "CONFIRM_RECOVERY",
      deadline: new Date(now + 20000).toISOString(),
      engine_boot_id: "boot-a",
      expected_state_version: 4,
      grant: null,
    },
    execution_scope: { kind: "PLAN", payload_hash: "scope-hash" },
    installed_limits: {},
    recovery: { payload_hash: "report-hash" },
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const path = url.replace("/api/v2/node/", "");
      calls.push({ path, init });
      if (extra) return extra(path, init);
      return standard(path, init);
    }),
  );
});
function standard(path: string, init: RequestInit) {
  if (path === "status") return response(current);
  if (path === "trading-settings" && init.method === "GET")
    return response({
      draft: {
        version: 1,
        saved_at: new Date(now).toISOString(),
        settings: {
          buy: {
            order_method: "ASK1_PROTECTED_LIMIT",
            max_daily_buy_value: "0",
            max_symbol_buy_value: "0",
            max_order_value: "0",
            cancel_after_seconds: 10,
            max_retries: 3,
          },
          sell: { take_profit_pct: "5", stop_loss_pct: "3", sell_by: "14:30:00" },
          safety: {
            max_daily_loss: "0",
            max_positions: 0,
            emergency_liquidation_enabled: false,
          },
          advanced: {
            max_order_quantity: 10000,
            max_chase_bps: 30,
            max_spread_bps: 20,
            quote_max_age_ms: 1000,
          },
        },
      },
      buy_configured: false,
      simulation_ready: false,
      active: false,
      active_version: null,
    });
  if (path === "session" && init.method === "GET")
    return loggedIn ? response(session) : response({ code: "OPERATOR_SESSION_REQUIRED" }, 401);
  if (path === "session") return response(init.method === "DELETE" ? {} : session);
  if (path === "preview") return response(proposal);
  if (path === "control") return response({ status: "APPLIED", action: "HALT" });
  if (path === "trades")
    return response({ available: false, positions: [], orders: [], trades: [] });
  return response({ status: "APPLIED", request_id: identity, reason_code: "CONFIRMED" });
}
afterEach(() => {
  wrapper?.unmount();
  wrapper = undefined;
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("V2 operator console", () => {
  it("identifies invalid settings before sending them to the backend", async () => {
    await start();
    await click("尾盘策略");
    const form = wrapper!.get(".settings-form");
    await form.findAll("input")[0]!.setValue("50000");
    await form.trigger("submit");
    await flushPromises();
    expect(wrapper!.get("#settings-errors").text()).toContain("三个买入金额");
    expect(
      calls.filter((call) => call.path === "trading-settings" && call.init.method === "POST"),
    ).toHaveLength(0);
  });
  it("does not silently turn an empty required retry count into zero", async () => {
    await start();
    await click("尾盘策略");
    const form = wrapper!.get(".settings-form");
    await form.findAll("input")[4]!.setValue("");
    await form.trigger("submit");
    await flushPromises();
    expect(wrapper!.get("#settings-errors").text()).toContain("最多重新下单次数");
    expect(
      calls.filter((call) => call.path === "trading-settings" && call.init.method === "POST"),
    ).toHaveLength(0);
  });
  it("shows the applied pause on both pages and prevents duplicate pause requests", async () => {
    current.components.engine.data!.state = "ARMED";
    current.components.engine.data!.state_version = 5;
    await start();
    await click("立即急停");
    expect(button("自动交易已暂停").attributes("disabled")).toBeDefined();
    await click("系统设置");
    expect(wrapper!.text()).toContain("暂停操作已完成：自动交易已暂停");
    expect(wrapper!.text()).toContain("已有持仓仍保留");
    await button("自动交易已暂停").trigger("click");
    expect(calls.filter((call) => call.path === "control")).toHaveLength(1);

    current.components.engine.data!.state = "HALTED";
    current.components.engine.data!.halted = true;
    current.components.engine.data!.state_version = 6;
    await vi.advanceTimersByTimeAsync(1000);
    expect(button("自动交易已暂停").attributes("disabled")).toBeDefined();
    current.components.engine.data!.state = "ARMED";
    current.components.engine.data!.halted = false;
    current.components.engine.data!.state_version = 8;
    await vi.advanceTimersByTimeAsync(1000);
    expect(button("立即急停").attributes("disabled")).toBeUndefined();
    await click("首页");
    expect(wrapper!.find(".hero-status").text()).not.toContain("自动交易已暂停");
  });

  it("does not claim a successful pause when the request fails", async () => {
    extra = async (path, init) =>
      path === "control" ? response({ code: "PIPE_UNAVAILABLE" }, 503) : standard(path, init);
    await start();
    await click("立即急停");
    expect(button("立即急停").attributes("disabled")).toBeUndefined();
    expect(wrapper!.text()).not.toContain("暂停操作已完成");
  });

  it("shows real trading running from engine state and clears that display after pause", async () => {
    current.broker = "QMT_TRADING";
    current.real_trading_enabled = true;
    current.components.engine.data!.state = "ARMED";
    await start();
    expect(wrapper!.get(".hero-status").text()).toContain("实盘自动交易运行中");
    await click("立即急停");
    expect(wrapper!.get(".hero-status").text()).not.toContain("实盘自动交易运行中");
    expect(wrapper!.get(".hero-status").text()).toContain("自动交易已暂停");
  });

  it("takes an unconfigured trading settings warning directly to the settings form", async () => {
    extra = async (path, init) =>
      path === "trading-settings"
        ? response({ draft: null, simulation_ready: false, active: false, active_version: null })
        : standard(path, init);
    await start();
    await click("系统设置");
    expect(wrapper!.text()).toContain("尾盘策略 → 交易设置");
    const warning = wrapper!.get(".primary-settings .notice.warning");
    await warning.get("button").trigger("click");
    await flushPromises();
    expect(wrapper!.get("#trading-settings-heading").text()).toBe("尾盘策略 · 交易设置");
    expect(wrapper!.find(".settings-form").exists()).toBe(true);
    expect(button("保存交易设置").attributes("disabled")).toBeUndefined();
  });

  it("hides placeholder settings until login and restores saved values inline", async () => {
    loggedIn = false;
    await start();
    await click("尾盘策略");
    expect(wrapper!.find(".settings-form").exists()).toBe(false);
    expect(wrapper!.text()).toContain("已保存的交易设置暂未读取");
    await wrapper!.get("#settings-password").setValue("ephemeral-password");
    await wrapper!.get(".login-form").trigger("submit");
    await flushPromises();
    expect(wrapper!.get(".settings-form").isVisible()).toBe(true);
    expect(wrapper!.get('.settings-form input[type="time"]').element).toHaveProperty(
      "value",
      "14:30",
    );
    expect(wrapper!.text()).toContain("已保存第 1 版");
  });

  it("does not show defaults or permit overwrites when saved settings cannot be read", async () => {
    let fail = true;
    extra = async (path, init) =>
      path === "trading-settings" && init.method === "GET" && fail
        ? response({ code: "TRADING_SETTINGS_STORE_INVALID" }, 503)
        : standard(path, init);
    await start();
    await click("尾盘策略");
    expect(wrapper!.find(".settings-form").exists()).toBe(false);
    expect(wrapper!.text()).toContain("读取已保存设置失败");
    fail = false;
    await click("重新读取设置");
    expect(wrapper!.get(".settings-form").isVisible()).toBe(true);
    expect(wrapper!.text()).toContain("已保存第 1 版");
  });

  it("opens the full system while QMT setup is still locked", async () => {
    current.node_id = "unconfigured-local";
    current.account_id = "qmt-not-configured-local";
    await start();
    await click("系统设置");

    expect(wrapper!.text()).toContain("尚未连接 QMT");
    expect(wrapper!.text()).toContain("系统已经正常打开");
    expect(wrapper!.get('a[href="/connect-qmt"]').text()).toBe("开始连接 QMT");
    expect(button("QMT 未连接").attributes("disabled")).toBeDefined();
  });

  it("requires a second click before sending a cancel for an owned active order", async () => {
    extra = async (path, init) => {
      if (path === "trades")
        return response({
          available: true,
          positions: [],
          trades: [],
          orders: [
            {
              client_order_id: "ord_owned0001",
              symbol: "600000.SH",
              side: "SELL",
              status: "ACKED",
              quantity: 100,
              filled: 0,
              remaining: 100,
              reason: "QMT_STATUS_50",
              owned_by_system: true,
              cancelable: true,
            },
          ],
        });
      if (path === "orders/cancel") return response({ status: "REQUESTED" });
      return standard(path, init);
    };
    await start();
    await click("交易监控");
    await flushPromises();
    await click("撤单");
    expect(calls.some((call) => call.path === "orders/cancel")).toBe(false);
    await click("确认撤单");
    await flushPromises();
    expect(calls.filter((call) => call.path === "orders/cancel")).toHaveLength(1);
    expect(wrapper!.text()).toContain("等待券商确认");
  });

  it("shows recent broker orders and fills in messages", async () => {
    extra = async (path, init) => {
      if (path === "trades")
        return response({
          available: true,
          positions: [],
          orders: [
            {
              client_order_id: "ord_owned0002",
              symbol: "510300.SH",
              side: "SELL",
              status: "PARTIALLY_FILLED",
              quantity: 200,
              filled: 100,
              remaining: 100,
              reason: "QMT_STATUS_55",
              owned_by_system: true,
              cancelable: true,
            },
          ],
          trades: [
            {
              trade_id: "trade-1",
              symbol: "510300.SH",
              side: "SELL",
              quantity: 100,
              price: "4.20",
              traded_at: new Date(now).toISOString(),
            },
          ],
        });
      return standard(path, init);
    };
    await start();
    await click("消息与日志");
    expect(wrapper!.text()).toContain("最近委托");
    expect(wrapper!.text()).toContain("已成交 100/200");
    expect(wrapper!.text()).toContain("最近成交");
    expect(wrapper!.text()).toContain("4.20 元");
  });

  it("separates per-symbol signals from orders and shows the current-start activity history", async () => {
    current.components.engine.data!.market_session = {
      driver_state: "READY",
      subscription_count: 1,
      entry_progress: [{ symbol: "000001.SZ", state: "WAITING_CAPACITY" }],
      entry_targets: [
        {
          symbol: "000001.SZ",
          target_quantity: 300,
          reason: "ELIGIBLE",
          decided_at: new Date(now).toISOString(),
        },
      ],
      recent_activity: [
        {
          occurred_at: new Date(now).toISOString(),
          category: "ENTRY_TARGET",
          state: "BUY_TARGET_READY:ELIGIBLE:300",
          symbol: "000001.SZ",
          detail: "ELIGIBLE",
        },
      ],
    };
    await start();
    await click("交易监控");
    expect(wrapper!.text()).toContain("逐只股票信号");
    expect(wrapper!.text()).toContain("已产生买入目标 300 股");
    expect(wrapper!.text()).toContain("是否下单及成交请继续看委托和成交");

    await click("消息与日志");
    expect(wrapper!.text()).toContain("本次启动活动记录");
    expect(wrapper!.text()).toContain("买入目标已确认");
  });

  it("tells the operator exactly how saved live settings become active", async () => {
    current.broker = "QMT_TRADING";
    current.real_trading_enabled = true;
    extra = async (path, init) => {
      if (path === "trading-settings" && init.method === "POST") {
        const base = await standard("trading-settings", { method: "GET" });
        const payload = await base.json();
        return response({ ...payload, live_ready: false, restart_required: true });
      }
      return standard(path, init);
    };
    await start();
    await click("尾盘策略");
    await wrapper!.get(".settings-form").trigger("submit");
    await flushPromises();
    expect(wrapper!.text()).toContain("请关闭自动交易系统主窗口并重新打开");
    expect(wrapper!.text()).toContain("重启后系统仍保持暂停");
  });

  it("saves one simple trading-settings draft and tests its stop-loss without placing an order", async () => {
    let version = 0;
    let savedDraft: unknown = null;
    extra = async (path, init) => {
      if (path === "trading-settings" && init.method === "GET")
        return response({
          draft: savedDraft,
          buy_configured: false,
          simulation_ready: false,
          active: false,
          active_version: null,
        });
      if (path === "sell-rules" && init.method === "GET")
        return response({
          draft: null,
          simulation_ready: false,
          active: false,
          active_version: null,
        });
      if (path === "trading-settings" && init.method === "POST") {
        const body = JSON.parse(String(init.body)) as {
          expected_version: number;
          settings: {
            buy: {
              max_daily_buy_value: unknown;
              max_symbol_buy_value: unknown;
              max_order_value: unknown;
            };
            safety: { max_daily_loss: unknown };
          };
        };
        expect(body.expected_version).toBe(version);
        for (const amount of [
          body.settings.buy.max_daily_buy_value,
          body.settings.buy.max_symbol_buy_value,
          body.settings.buy.max_order_value,
          body.settings.safety.max_daily_loss,
        ]) {
          expect(typeof amount).toBe("string");
        }
        version++;
        savedDraft = { version, saved_at: new Date(now).toISOString(), settings: body.settings };
        return response({
          draft: savedDraft,
          buy_configured: true,
          simulation_ready: true,
          active: false,
          active_version: null,
        });
      }
      if (path === "sell-rules/test")
        return response({
          reasons: ["STOP_LOSS"],
          should_sell: true,
          change_pct: "-3",
          actual_order_sent: false,
        });
      return standard(path, init);
    };
    await start();
    await click("尾盘策略");
    expect(wrapper!.text()).toContain("当前没有交易设置");
    expect(wrapper!.text()).toContain("已有持仓不会套用默认卖出规则");
    const form = wrapper!.get(".settings-form");
    const inputs = form.findAll("input");
    await inputs[0]!.setValue("50000");
    await inputs[1]!.setValue("10000");
    await inputs[2]!.setValue("5000");
    await inputs[5]!.setValue("5");
    await inputs[6]!.setValue("3");
    await inputs[7]!.setValue("14:30");
    await inputs[8]!.setValue("2000");
    await inputs[9]!.setValue("5");
    await form.trigger("submit");
    await flushPromises();
    expect(wrapper!.text()).toContain("已保存第 1 版");
    expect(wrapper!.text()).toContain("启用模拟…");
    expect(
      calls.filter((call) => call.path === "trading-settings" && call.init.method === "POST"),
    ).toHaveLength(1);
    await inputs[7]!.setValue("12:00");
    expect(wrapper!.text()).toContain("午休");
    expect(form.get("button.primary").attributes("disabled")).toBeDefined();
    await inputs[7]!.setValue("14:30");
    await click("试算卖出规则");
    const testInputs = wrapper!.findAll(".rule-form input");
    await testInputs[0]!.setValue("10");
    await testInputs[1]!.setValue("9.70");
    await wrapper!.get(".rule-form").trigger("submit");
    await flushPromises();
    expect(wrapper!.text()).toContain("达到止损幅度");
    expect(wrapper!.text()).toContain("未发出真实委托");
    wrapper!.unmount();
    await start();
    await click("尾盘策略");
    const restored = wrapper!.findAll(".settings-form input");
    for (const [index, expected] of [
      [0, "50000"],
      [1, "10000"],
      [2, "5000"],
      [5, "5"],
      [6, "3"],
      [7, "14:30"],
      [8, "2000"],
      [9, "5"],
    ] as const) {
      expect(restored[index]!.element).toHaveProperty("value", expected);
    }
    expect(wrapper!.text()).toContain("已保存第 1 版");
  });
  it("keeps an unconfigured operator profile read-only", async () => {
    loggedIn = false;
    current.operator_access_configured = false;
    await start();
    expect(wrapper!.text()).toContain("当前配置未启用操作员访问");
    expect(button("登录本机操作员").attributes("disabled")).toBeDefined();
    await wrapper!.get("form").trigger("submit");
    expect(calls.some((c) => c.path === "session" && c.init.method === "POST")).toBe(false);
  });
  it("shows actual observations, plan and explicit mock/strategy boundaries", async () => {
    await start();
    for (const value of [
      "今天执行到哪一步",
      "MOCK",
      "plan-a",
      "600000.SH",
      "监护中",
      "最近任务已同步",
      "以具体任务结果为准",
    ])
      expect(wrapper!.text()).toContain(value);
    expect(button("模拟授权…").attributes("disabled")).toBeUndefined();
    expect(calls.filter((c) => c.path === "status")).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1000);
    await flushPromises();
    expect(calls.filter((c) => c.path === "status")).toHaveLength(2);
  });
  it("labels the QMT live profile and exposes explicit live authorization controls", async () => {
    current.broker = "QMT_TRADING";
    current.components.engine.data!.broker_account = {
      observed_at: new Date(now).toISOString(),
      position_count: 1,
      order_count: 2,
      trade_count: 1,
      coherent_for_recovery: true,
    };
    await start();
    expect(wrapper!.text()).toContain("QMT 实盘环境");
    expect(wrapper!.text()).toContain("QMT实盘账户已连接，自动交易等待本机启动");
    expect(wrapper!.text()).toContain("实际报单结果以券商回报为准");
    expect(wrapper!.findAll("button").some((item) => item.text() === "模拟授权…")).toBe(false);
    expect(button("启动实盘自动交易…").exists()).toBe(true);
    expect(button("确认恢复…").exists()).toBe(true);
  });
  it("shows observed installed limits and the exact grant instead of hardcoded zeros", async () => {
    current.components.engine.data!.installed_limits = {
      max_buy_value: "5000.00",
      max_order_value: "2000.00",
      max_positions: 3,
    };
    proposal.request.grant = {
      mode: "PAPER",
      directions: ["SELL"],
      max_buy_value: "0.00",
      max_order_value: "1500.00",
      max_positions: 2,
      valid_until: "2026-09-24T06:31:00Z",
    };
    await start();
    expect(wrapper!.text()).toContain("5000.00 元");
    expect(wrapper!.text()).toContain("2000.00 元");
    await click("模拟授权…");
    const review = wrapper!.get(".preview-panel").text();
    expect(review).toContain("SELL");
    expect(review).toContain("1500.00 元");
    expect(review).toContain("2026-09-24T06:31:00Z");
    expect(review).not.toContain("额度固定为 0");
  });
  it("logs in with cleared password and logs out without persisting secrets", async () => {
    loggedIn = false;
    await start();
    await wrapper!.get("#operator-password").setValue("ephemeral-password");
    await wrapper!.get("form").trigger("submit");
    await flushPromises();
    expect(wrapper!.text()).toContain("已登录 · operator-a");
    const login = calls.find((c) => c.path === "session" && c.init.method === "POST")!;
    expect(JSON.parse(String(login.init.body)).password).toBe("ephemeral-password");
    expect(sessionStorage.length).toBe(0);
    await click("退出登录");
    expect(wrapper!.get<HTMLInputElement>("#operator-password").element.value).toBe("");
  });
  it("requires exact phrase, confirms once and clears retained identity on receipt", async () => {
    await start();
    await click("确认恢复…");
    expect(button("确认本次操作").attributes("disabled")).toBeDefined();
    expect(wrapper!.text()).toContain("report-hash");
    await wrapper!.get("#confirm-phrase").setValue("wrong");
    await confirm();
    expect(calls.some((c) => c.path === "confirm")).toBe(false);
    await wrapper!.get("#confirm-phrase").setValue(proposal.confirmation_phrase);
    await confirm();
    expect(calls.filter((c) => c.path === "confirm")).toHaveLength(1);
    expect(wrapper!.text()).toContain("已生效");
    expect(readPending(key)).toBe("");
    expect(wrapper!.find(".preview-panel").exists()).toBe(false);
  });
  it("retains an uncertain request across refresh and only queries its original ID", async () => {
    extra = async (path, init) => {
      if (path === "confirm") throw new Error("lost");
      return standard(path, init);
    };
    await start();
    await prepare();
    await confirm();
    expect(wrapper!.text()).toContain("请求结果待确认");
    expect(button("模拟授权…").attributes("disabled")).toBeDefined();
    expect(readPending(key)).toBe(identity);
    wrapper!.unmount();
    wrapper = undefined;
    await start();
    expect(wrapper!.text()).toContain(identity);
    await click("查询原请求结果");
    expect(calls.filter((c) => c.path === "confirm")).toHaveLength(1);
    const result = calls.find((c) => c.path === "result")!;
    expect(JSON.parse(String(result.init.body))).toEqual({ request_id: identity });
    expect(readPending(key)).toBe("");
  });
  it("keeps PENDING or mismatched receipts unresolved", async () => {
    extra = async (path, init) =>
      path === "confirm"
        ? response({ status: "PENDING", request_id: identity })
        : path === "result"
          ? response({ status: "APPLIED", request_id: "wrong" })
          : standard(path, init);
    await start();
    await prepare();
    await confirm();
    await click("查询原请求结果");
    expect(wrapper!.text()).toContain("CONTROL_RESULT_UNCERTAIN");
    expect(readPending(key)).toBe(identity);
  });
  it("locks grants on stale observations but leaves authenticated halt available", async () => {
    current.components.engine.availability = "STALE";
    await start();
    expect(wrapper!.text()).toContain("状态过期");
    expect(button("模拟授权…").attributes("disabled")).toBeDefined();
    await click("立即急停");
    expect(calls.find((c) => c.path === "control")?.init.headers).toHaveProperty(
      "X-CSRF-Token",
      session.csrf_token,
    );
  });
  it("detects lost HTTP refreshes and locks actions while retaining last facts", async () => {
    await start();
    extra = async () => {
      throw new Error("network");
    };
    await vi.advanceTimersByTimeAsync(1000);
    await flushPromises();
    expect(wrapper!.text()).toContain("页面连接中断");
    expect(wrapper!.text()).toContain("plan-a");
    expect(button("模拟授权…").attributes("disabled")).toBeDefined();
    await click("立即急停");
    expect(wrapper!.text()).toContain("连接中断或超时");
  });
  it("does not overlap slow status polls and locally marks stale before timeout", async () => {
    await start();
    extra = (path, init) =>
      path === "status" ? new Promise(() => {}) : Promise.resolve(standard(path, init));
    await vi.advanceTimersByTimeAsync(6000);
    await flushPromises();
    expect(calls.filter((c) => c.path === "status")).toHaveLength(2);
    expect(button("模拟授权…").attributes("disabled")).toBeDefined();
  });
  it("expires previews and allows cancel without any control call", async () => {
    proposal.request.deadline = new Date(now + 1000).toISOString();
    await start();
    await prepare();
    await vi.advanceTimersByTimeAsync(1500);
    await flushPromises();
    expect(button("预览已过期，请重新获取").attributes("disabled")).toBeDefined();
    await confirm();
    expect(calls.some((c) => c.path === "confirm")).toBe(false);
    await click("取消预览");
    expect(wrapper!.find(".preview-panel").exists()).toBe(false);
  });
  it("fences a slow preview after emergency halt", async () => {
    let resolve!: (value: Response) => void;
    extra = (path, init) =>
      path === "preview"
        ? new Promise((r) => {
            resolve = r;
          })
        : Promise.resolve(standard(path, init));
    await start();
    await click("确认恢复…");
    await click("立即急停");
    resolve(response(proposal));
    await flushPromises();
    expect(wrapper!.find(".preview-panel").exists()).toBe(false);
  });
  it("recovery and simulated ARM/RESUME use separate explicit actions", async () => {
    await start();
    await click("重新核验恢复");
    expect(JSON.parse(String(calls.find((c) => c.path === "control")!.init.body))).toEqual({
      action: "RECOVER",
    });
    proposal.request.grant = { max_buy_value: "0", max_order_value: "0.00" };
    proposal.execution_scope = null;
    await click("模拟授权…");
    expect(wrapper!.text()).toContain("买入总额上限 0 元");
    await click("取消预览");
    await click("模拟恢复授权…");
    expect(
      calls.filter((c) => c.path === "preview").map((c) => JSON.parse(String(c.init.body)).action),
    ).toEqual(["ARM", "RESUME"]);
  });
  it("drops invalid sessions and displays login failures without the password", async () => {
    await start();
    extra = async (path, init) =>
      path === "preview"
        ? response({ code: "OPERATOR_SESSION_INVALID" }, 401)
        : standard(path, init);
    await click("确认恢复…");
    expect(wrapper!.find("#operator-password").exists()).toBe(true);
    extra = async () => response({ code: "OPERATOR_LOGIN_FAILED" }, 401);
    await wrapper!.get("#operator-password").setValue("password-not-visible");
    await wrapper!.get("form").trigger("submit");
    await flushPromises();
    expect(wrapper!.text()).toContain("口令不正确");
    expect(wrapper!.text()).not.toContain("password-not-visible");
  });
  it("rejects corrupt stored identities and fails closed on tracking write failure", async () => {
    sessionStorage.setItem(key, "corrupt");
    await start();
    expect(wrapper!.text()).toContain("浏览器无法保留原请求编号");
    expect(button("确认恢复…").attributes("disabled")).toBeDefined();
    wrapper!.unmount();
    wrapper = undefined;
    sessionStorage.clear();
    await start();
    await prepare();
    vi.stubGlobal("sessionStorage", {
      getItem: () => null,
      setItem: () => {
        throw new Error("full");
      },
    });
    await confirm();
    expect(calls.some((c) => c.path === "confirm")).toBe(false);
  });
  it("shows unavailable and empty states without invented account facts", async () => {
    current.components.engine = {
      availability: "UNAVAILABLE",
      age_seconds: null,
      observed_at: null,
      error: "PROBE_FAILED",
      data: null,
    };
    loggedIn = false;
    await start();
    expect(wrapper!.text()).toContain("尚未加载计划");
    expect(wrapper!.text()).toContain("进程未连接");
    current.components.engine.data = { journal_healthy: false, plan: { symbols: [] } };
    await vi.advanceTimersByTimeAsync(1000);
    await flushPromises();
    expect(wrapper!.text()).toContain("异常");
    expect(wrapper!.text()).toContain("空计划");
  });
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
