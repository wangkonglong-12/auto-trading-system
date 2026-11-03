import { flushPromises, mount } from "@vue/test-utils";
import type { VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import NodeConsole from "../node/NodeConsole.vue";
import type { DailyAutoPreview, DailyAutoState, NodeStatus, Preview } from "../node/api";
import type { AccountOrder, Dashboard, SettingsResponse } from "../node/consoleTypes";
import { pendingKey, writePending } from "../node/pending";

const now = Date.parse("2026-10-23T06:46:00Z");
const identity = "control-" + "a".repeat(32);
const key = pendingKey("node-a", "account-a");
const session = {
  actor_id: "operator-a",
  csrf_token: "csrf-local",
  expires_at: new Date(now + 3600000).toISOString(),
};
let current: NodeStatus;
let settings: SettingsResponse;
let dashboard: Dashboard;
let proposal: Preview;
let wrapper: VueWrapper | undefined;
let loggedIn: boolean;
let override: ((path: string, init: RequestInit) => Promise<Response | undefined>) | undefined;
const calls: { path: string; init: RequestInit }[] = [];
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
class TestEvents {
  static instances: TestEvents[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor() {
    TestEvents.instances.push(this);
  }
  close() {
    this.closed = true;
  }
  emit(value: unknown) {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(value) }));
  }
}
function button(label: string) {
  const found = wrapper!.findAll("button").find((b) => b.text() === label);
  if (!found) throw new Error(`button missing: ${label}`);
  return found;
}

describe("stock row sale lifecycle", () => {
  it.each([
    ["sold", 0, 100, true, false, "已全部卖出"],
    ["partial", 60, 40, true, false, "当前持仓部分卖出"],
    ["unfilled", 100, 0, true, false, "已完成买入"],
    ["incoherent", 0, 100, false, false, "已完成买入"],
    ["truncated", 0, 100, true, true, "已完成买入"],
    ["stale", 0, 100, true, false, "已完成买入"],
    ["other stock", 0, 100, true, false, "已完成买入"],
  ])(
    "shows %s from checked holdings without replacing original buy facts",
    async (scenario, remaining, sold, coherent, truncated, expected) => {
      dashboard.buys.rows[0]!.is_snapshot = true;
      dashboard.account.coherent = coherent;
      dashboard.account.truncated = truncated;
      if (scenario === "stale") dashboard.account.observed_at = new Date(now - 16000).toISOString();
      dashboard.account.sell_positions = [
        {
          symbol: scenario === "other stock" ? "600698.SH" : "000001.SZ",
          name: "平安银行",
          remaining,
          sellable: remaining,
          reserved: 0,
          sold_today: sold,
          sell_amount: "1000",
          average_sell_price: "10",
          eligible_sell_date: "2026-10-23",
          can_sell: false,
          disabled_reason: "NO_POSITION",
          state: remaining === 0 ? "SOLD" : "PARTIAL_SOLD",
        },
      ];
      // A raw broker FILLED report alone is never an actual disposition proof.
      dashboard.account.orders = [
        {
          client_order_id: "ord_sell_test123",
          symbol: "000001.SZ",
          side: "SELL",
          status: "FILLED",
          quantity: 100,
          filled: 100,
          remaining: 0,
          limit_price: "10",
          owned_by_system: true,
          cancelable: false,
        },
      ];
      await start();
      const cells = wrapper!.get(".stock-table tbody tr").findAll("td");
      expect(cells[2]!.get(".execution-state").text()).toBe(expected);
      expect(cells[4]!.text()).toBe("10.0100");
      expect(cells[5]!.text()).toContain("1,001.00");
      if (scenario === "sold" || scenario === "partial")
        expect(cells[2]!.text()).toContain("买入结果：已完成买入");
      expect(calls.some((call) => call.path.includes("manual-sell/confirm"))).toBe(false);
    },
  );
});

describe("account recovery failure diagnosis", () => {
  it("offers recovery after a market halt and explains the disabled sale", async () => {
    Object.assign(current.components.engine.data!, {
      state: "HALTED",
      recovery: "CLEAR",
      pause_reason: "QMT_TICK_CALLBACK_INVALID",
    });
    dashboard.account.sell_positions = [
      {
        symbol: "000001.SZ",
        name: "平安银行",
        remaining: 100,
        sellable: 100,
        reserved: 0,
        sold_today: 0,
        sell_amount: "0",
        average_sell_price: null,
        eligible_sell_date: "2026-10-23",
        can_sell: false,
        disabled_reason: "NOT_READY",
        state: "HOLDING",
      },
    ];
    await start();
    expect(wrapper!.text()).toContain("连续收到异常行情");
    expect(wrapper!.text()).not.toContain("账户尚未准备好");
    expect(wrapper!.get('[aria-label="卖出000001.SZ"]').attributes("disabled")).toBeDefined();
    await click("重新核验账户");
    const controls = calls.filter((call) => call.path === "control");
    expect(JSON.parse(String(controls[0]!.init.body))).toEqual({ action: "RECOVER" });
  });
  it("shows the failure and disabled reason instead of indefinitely waiting for QMT data", async () => {
    Object.assign(current.components.engine.data!, {
      state: "HALTED",
      recovery: null,
      pause_reason: "RECOVERY_FAILED",
      recovery_error: "QMT_TIMESTAMP_OUTSIDE_TRADE_DATE",
    });
    dashboard.account.available = false;
    await start();
    expect(wrapper!.text()).toContain("QMT账户核验失败");
    expect(wrapper!.text()).toContain("不属于当前交易日");
    expect(wrapper!.text()).toContain("手动买入不可用：账户核验失败");
    expect(button("手动买入").attributes("disabled")).toBeDefined();
    await click("重新核验账户");
    const controls = calls.filter((call) => call.path === "control");
    expect(controls).toHaveLength(1);
    expect(JSON.parse(String(controls[0]!.init.body))).toEqual({ action: "RECOVER" });
    expect(
      calls.some((call) => call.path.includes("manual-buy/confirm") || call.path === "confirm"),
    ).toBe(false);
  });
  it("also explains failure when an older process only publishes RECOVERY_FAILED", async () => {
    Object.assign(current.components.engine.data!, {
      state: "HALTED",
      recovery: null,
      pause_reason: "RECOVERY_FAILED",
    });
    await start();
    expect(wrapper!.text()).toContain("后台账户核验失败");
    expect(button("手动买入").attributes("disabled")).toBeDefined();
  });
  it("keeps board sell engines distinct from the legacy sell blocker", async () => {
    current.components.engine.data!.exit_context_loaded = true;
    current.components.engine.data!.legacy_exit_context_loaded = false;
    await start();
    expect(button("手动买入").attributes("disabled")).toBeUndefined();
    expect(wrapper!.text()).not.toContain("旧版卖出规则仍在运行");
    expect(wrapper!.text()).toContain("主板、创业板卖出引擎独立管理");
  });
  it("does not require an automatic selection or a configured sell rule to open manual buy", async () => {
    current.components.engine.data!.plan_loaded = false;
    await start();
    expect(button("手动买入").attributes("disabled")).toBeUndefined();
    await click("手动买入");
    expect(wrapper!.find('[aria-labelledby="manual-buy-title"]').exists()).toBe(true);
    expect(calls.some((call) => call.path.endsWith("/confirm"))).toBe(false);
  });
});
async function click(label: string) {
  await button(label).trigger("click");
  await flushPromises();
}
async function start() {
  wrapper = mount(NodeConsole);
  await flushPromises();
  return wrapper;
}
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(now);
  sessionStorage.clear();
  calls.length = 0;
  loggedIn = true;
  override = undefined;
  TestEvents.instances = [];
  vi.stubGlobal("EventSource", TestEvents);
  const obs = {
    availability: "CURRENT" as const,
    age_seconds: 0,
    observed_at: new Date(now).toISOString(),
    error: null,
  };
  current = {
    node_id: "node-a",
    account_id: "account-a",
    broker: "QMT_TRADING",
    real_trading_enabled: true,
    strategy_status: "READY",
    operator_access_configured: true,
    components: {
      engine: {
        ...obs,
        data: {
          state: "READY_PAUSED",
          halted: true,
          recovery: "CLEAR",
          plan_loaded: true,
          trade_date: "2026-10-23",
          boot_id: "boot-a",
        },
      },
      agent: { ...obs, data: {} },
      supervisor: { ...obs, data: {} },
    },
  };
  settings = {
    draft: {
      version: 1,
      saved_at: new Date(now).toISOString(),
      settings: {
        buy: {
          signal_mode: "TAIL_EXCELLENT_3_PHASES",
          order_method: "ASK1_PROTECTED_LIMIT",
          max_daily_symbols: 3,
          max_daily_buy_value: "30000",
          max_symbol_buy_value: "10000",
          max_order_value: "10000",
          cancel_after_seconds: 10,
          max_retries: 3,
        },
        sell: null,
        safety: { max_daily_loss: "1000", max_positions: 3, emergency_liquidation_enabled: false },
        advanced: {
          max_order_quantity: 10000,
          max_chase_bps: 30,
          max_spread_bps: 20,
          quote_max_age_ms: 1000,
        },
      },
    },
    live_ready: true,
    restart_required: false,
    buy_configured: true,
  };
  dashboard = {
    account: {
      available: true,
      observed_at: new Date(now).toISOString(),
      cash: "12345.67",
      total_equity: "50000.00",
      market_value: "37654.33",
      names: { "000001.SZ": "平安银行" },
      positions: [
        { symbol: "000001.SZ", quantity: 100, sellable: 0, frozen: 0, market_value: "1001" },
      ],
      orders: [],
      trades: [
        {
          trade_id: "fill-a",
          symbol: "000001.SZ",
          side: "BUY",
          quantity: 100,
          price: "10.01",
          traded_at: new Date(now - 1000).toISOString(),
          owned_by_system: true,
        },
      ],
    },
    buys: {
      selection_date: "2026-10-23",
      starts_at: "2026-10-23T14:30:00+08:00",
      expires_at: "2026-10-26T14:30:00+08:00",
      display_state: "CURRENT",
      decision_phases: [],
      available: true,
      observed_at: new Date(now).toISOString(),
      rows: [
        {
          symbol: "000001.SZ",
          name: "平安银行",
          state: "FILLED",
          signal_state: "COMPLETE",
          quantity: 100,
          bought_at: new Date(now - 1000).toISOString(),
          average_price: "10.0100",
          amount: "1001.00",
        },
      ],
      completed: 1,
      partial: 0,
      pending: 0,
      not_bought: 0,
      occupied: 1,
      remaining: 2,
      maximum: 3,
    },
  };
  proposal = {
    preview_id: "preview-" + "b".repeat(32),
    confirmation_phrase: "CONFIRM LIVE ARM bbbbbbbb",
    actor_id: "operator-a",
    account_id: "account-a",
    trade_date: "2026-10-23",
    permission_epoch: 1,
    request: {
      request_id: identity,
      action: "ARM",
      deadline: new Date(now + 20000).toISOString(),
      engine_boot_id: "boot-a",
      expected_state_version: 4,
      grant: { max_buy_value: "30000" },
    },
    execution_scope: {},
    installed_limits: {},
    recovery: {},
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      const path = url.replace("/api/v2/node/", "");
      calls.push({ path, init });
      const extra = await override?.(path, init);
      if (extra) return extra;
      if (path === "status") return response(current);
      if (path === "session" && init.method === "GET")
        return loggedIn ? response(session) : response({ code: "OPERATOR_SESSION_REQUIRED" }, 401);
      if (path === "session" && init.method === "POST") {
        loggedIn = true;
        return response(session);
      }
      if (path === "session" && init.method === "DELETE") return response({ logged_out: true });
      if (path === "trading-settings") return response(settings);
      if (path === "system-settings")
        return response({
          version: 0,
          quant_scheme: "https",
          quant_host: "quant.test",
          quant_port: 443,
          close_warning: true,
          active_origin: "https://quant.test",
          restart_required: false,
        });
      if (path === "password") return response({ password_changed: true });
      if (path === "dashboard") return response(dashboard);
      if (path === "preview") return response(proposal);
      if (path === "confirm" || path === "result")
        return response({ status: "APPLIED", request_id: identity });
      if (path === "control") {
        current.components.engine.data!.state = "HALTED";
        current.components.engine.data!.halted = true;
        return response({ status: "APPLIED", action: "HALT" });
      }
      throw new Error(`unhandled endpoint ${path}`);
    }),
  );
});
afterEach(() => {
  wrapper?.unmount();
  wrapper = undefined;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("today stock order cancellation", () => {
  function order(change: Partial<AccountOrder> = {}): AccountOrder {
    return {
      client_order_id: "ord_manual0001",
      symbol: "000001.SZ",
      side: "BUY",
      status: "PARTIALLY_FILLED",
      quantity: 100,
      filled: 40,
      remaining: 60,
      limit_price: "10.00",
      owned_by_system: true,
      cancelable: true,
      ...change,
    };
  }
  const cancelButtons = () =>
    wrapper!
      .findAll('button[aria-label^="撤销"]')
      .filter((b) => !b.attributes("aria-label")?.includes("卖出委托"));

  it("offers a cancel retry only after an explicit failed outcome", async () => {
    dashboard.account.orders = [
      order({ cancel_state: "FAILED", cancel_reason: "QMT_CANCEL_ERROR_1" }),
    ];
    override = async (path) => {
      if (path !== "orders/cancel") return;
      dashboard.account.orders = [
        order({ cancel_state: "PENDING", cancel_requested: true, cancelable: false }),
      ];
      return response({ status: "REQUESTED" });
    };
    await start();
    expect(wrapper!.get(".stock-table").text()).toContain("券商拒绝本次撤单");
    await click("重试撤单");
    expect(wrapper!.get(".stock-table").text()).toContain("撤单处理中");
    expect(wrapper!.get(".stock-table").text()).not.toContain("重试撤单");
    expect(calls.filter((call) => call.path === "orders/cancel")).toHaveLength(1);
  });

  it("shows an unknown cancellation separately and never offers retry", async () => {
    dashboard.account.orders = [
      order({ cancel_state: "UNKNOWN", cancel_requested: true, cancelable: false }),
    ];
    await start();
    expect(wrapper!.get(".stock-table").text()).toContain("撤单结果待核对");
    expect(wrapper!.get(".stock-table").text()).not.toContain("撤单处理中");
    expect(cancelButtons()).toHaveLength(0);
  });

  it("can request cancellation of an automatic order while buying is paused", async () => {
    current.components.engine.data!.state = "HALTED";
    current.components.engine.data!.halted = true;
    dashboard.account.orders = [order()];
    dashboard.buys.rows[0]!.source = "AUTO";
    override = async (path) =>
      path === "orders/cancel" ? response({ status: "REQUESTED" }) : undefined;
    await start();
    expect(cancelButtons()[0]!.attributes("disabled")).toBeUndefined();
    await cancelButtons()[0]!.trigger("click");
    await flushPromises();
    expect(calls.filter((call) => call.path === "orders/cancel")).toHaveLength(1);
    expect(calls.some((call) => ["preview", "confirm", "control"].includes(call.path))).toBe(false);
  });

  it.each(["MANUAL", "AUTO"] as const)(
    "cancels a specific %s order directly in the row and waits for broker confirmation",
    async (source) => {
      dashboard.buys.rows[0]!.source = source;
      dashboard.buys.rows[0]!.state = "PARTIAL";
      dashboard.account.orders = [order()];
      override = async (path) => {
        if (path !== "orders/cancel") return;
        dashboard.account.orders[0]!.cancel_requested = true;
        dashboard.account.orders[0]!.cancelable = false;
        return response({ status: "REQUESTED" });
      };
      await start();
      expect(wrapper!.find(".detail-row").exists()).toBe(false);
      expect(wrapper!.get(".stock-table").text()).toContain("未成交 60 股");
      await cancelButtons()[0]!.trigger("click");
      await flushPromises();
      const sent = calls.filter((call) => call.path === "orders/cancel");
      expect(sent).toHaveLength(1);
      expect(sent[0]!.init.method).toBe("POST");
      expect(JSON.parse(String(sent[0]!.init.body))).toEqual({ client_order_id: "ord_manual0001" });
      expect(new Headers(sent[0]!.init.headers).get("x-csrf-token")).toBe("csrf-local");
      expect(wrapper!.text()).toContain("撤单请求已提交，请等待券商确认");
      expect(wrapper!.get(".stock-table").text()).toContain("撤单处理中");
      expect(wrapper!.get(".stock-table").text()).not.toContain("已撤单");
      expect(cancelButtons()).toHaveLength(0);
      expect(
        calls.some((call) => ["confirm", "orders/manual-buy/confirm"].includes(call.path)),
      ).toBe(false);
    },
  );

  it("keeps buy cancellations separate from external orders and the sell action", async () => {
    dashboard.account.orders = [
      order(),
      order({ client_order_id: "ord_manual0002", limit_price: "10.02", remaining: 20, filled: 80 }),
      order({ client_order_id: "ord_external01", owned_by_system: false }),
      order({ client_order_id: "ord_sell00001", side: "SELL" }),
    ];
    override = async (path) =>
      path === "orders/cancel" ? response({ status: "REQUESTED" }) : undefined;
    await start();
    expect(cancelButtons()).toHaveLength(2);
    await cancelButtons()[1]!.trigger("click");
    await flushPromises();
    const sent = calls.filter((call) => call.path === "orders/cancel");
    expect(sent).toHaveLength(1);
    expect(JSON.parse(String(sent[0]!.init.body))).toEqual({ client_order_id: "ord_manual0002" });
  });

  it("refreshes again after an older account query finishes during cancellation", async () => {
    dashboard.account.orders = [order()];
    await start();
    const staleResponse = response(dashboard);
    let finishCancel: ((value: Response) => void) | undefined;
    let finishQuery: ((value: Response) => void) | undefined;
    let reads = 0;
    override = async (path) => {
      if (path === "orders/cancel")
        return new Promise<Response>((resolve) => {
          finishCancel = resolve;
        });
      if (path === "dashboard" && reads++ === 0)
        return new Promise<Response>((resolve) => {
          finishQuery = resolve;
        });
    };
    await cancelButtons()[0]!.trigger("click");
    await flushPromises();
    await vi.advanceTimersByTimeAsync(5000);
    expect(finishQuery).toBeDefined();
    finishCancel!(response({ status: "REQUESTED" }));
    await flushPromises();
    dashboard.account.orders = [order({ status: "CANCELLED", cancelable: false })];
    dashboard.buys.rows[0]!.state = "CANCELLED";
    finishQuery!(staleResponse);
    await flushPromises();
    expect(calls.filter((call) => call.path === "dashboard")).toHaveLength(3);
    expect(wrapper!.get(".stock-table").text()).toContain("已撤单");
    expect(cancelButtons()).toHaveLength(0);
    expect(calls.filter((call) => call.path === "orders/cancel")).toHaveLength(1);
  });

  it("updates the list automatically when the broker later confirms cancellation", async () => {
    dashboard.account.orders = [order()];
    override = async (path) => {
      if (path !== "orders/cancel") return;
      dashboard.account.orders = [order({ cancel_requested: true, cancelable: false })];
      return response({ status: "REQUESTED" });
    };
    await start();
    await cancelButtons()[0]!.trigger("click");
    await flushPromises();
    expect(wrapper!.get(".stock-table").text()).toContain("撤单处理中");
    dashboard.account.orders = [order({ status: "PARTIALLY_FILLED_CANCELLED", cancelable: false })];
    dashboard.buys.rows[0]!.state = "PARTIALLY_FILLED_CANCELLED";
    await vi.advanceTimersByTimeAsync(5000);
    expect(wrapper!.get(".stock-table").text()).toContain("部成部撤");
    expect(wrapper!.get(".stock-table").text()).not.toContain("撤单处理中");
    expect(calls.filter((call) => call.path === "orders/cancel")).toHaveLength(1);
  });

  it.each(["snapshot", "previous day", "terminal", "external"])(
    "does not offer cancellation for a %s row",
    async (kind) => {
      dashboard.account.orders = [order()];
      if (kind === "snapshot") dashboard.buys.rows[0]!.is_snapshot = true;
      if (kind === "previous day") dashboard.buys.rows[0]!.trade_date = "2026-10-22";
      if (kind === "terminal")
        dashboard.account.orders = [order({ status: "FILLED", remaining: 0, cancelable: false })];
      if (kind === "external") dashboard.account.orders = [order({ owned_by_system: false })];
      await start();
      expect(cancelButtons()).toHaveLength(0);
      expect(calls.some((call) => call.path === "orders/cancel")).toBe(false);
    },
  );

  it.each(["stale account", "truncated account", "read only", "disconnected"])(
    "disables cancellation with %s data",
    async (kind) => {
      dashboard.account.orders = [order()];
      if (kind === "stale account")
        dashboard.account.observed_at = new Date(now - 16000).toISOString();
      if (kind === "truncated account") dashboard.account.truncated = true;
      if (kind === "read only") current.broker = "QMT_READ_ONLY";
      await start();
      if (kind === "disconnected") {
        TestEvents.instances.at(-1)!.onerror?.();
        await flushPromises();
      }
      expect(cancelButtons()[0]!.attributes("disabled")).toBeDefined();
      await cancelButtons()[0]!.trigger("click");
      expect(calls.some((call) => call.path === "orders/cancel")).toBe(false);
    },
  );

  it("refreshes a rejected cancellation without claiming success or retrying", async () => {
    dashboard.account.orders = [order()];
    override = async (path) => {
      if (path !== "orders/cancel") return;
      dashboard.account.orders = [
        order({ status: "FILLED", filled: 100, remaining: 0, cancelable: false }),
      ];
      return response({ code: "ORDER_NOT_CANCELABLE" }, 409);
    };
    await start();
    await cancelButtons()[0]!.trigger("click");
    await flushPromises();
    expect(wrapper!.text()).toContain("该委托已结束");
    expect(wrapper!.text()).not.toContain("撤单请求已提交");
    expect(cancelButtons()).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(2000);
    expect(calls.filter((call) => call.path === "orders/cancel")).toHaveLength(1);
  });
});

describe("persistent daily automatic buying", () => {
  function automatic() {
    const state: DailyAutoState = {
      supported: true,
      enabled: false,
      configured: false,
      state: "DISABLED",
      reason: "NOT_ENABLED",
      settings_hash: "sha256:" + "c".repeat(64),
      calendar_through: "2026-12-31",
      limits: {
        daily_value: "10000",
        symbol_value: "10000",
        order_value: "10000",
        daily_symbols: 1,
        positions: 1,
        position_pct: "100",
        daily_loss: "1000",
      },
    };
    const preview: DailyAutoPreview = {
      preview_id: "daily-auto-" + "a".repeat(32),
      confirmation_phrase: "ENABLE DAILY AUTO BUY aaaaaaaa",
      expires_at: new Date(now + 30000).toISOString(),
      settings_hash: state.settings_hash!,
      state,
      buy_only: true,
    };
    return { state, preview };
  }
  it("enables only after one explicit confirmation, without manual recovery or an ARM request", async () => {
    const { state, preview } = automatic();
    current.components.engine.data!.plan_loaded = false;
    current.components.engine.data!.state = "HALTED";
    current.components.engine.data!.pause_reason = "DAILY_AUTO_OPERATOR_DISABLED";
    override = async (path) => {
      if (path === "daily-auto") return response(state);
      if (path === "daily-auto/preview") return response(preview);
      if (path === "daily-auto/confirm")
        return response({
          ...state,
          enabled: true,
          configured: true,
          state: "WAITING",
          reason: "WAITING_FORMAL_PICKS",
          consent_id: preview.preview_id,
        });
    };
    await start();
    expect(wrapper!.text()).toContain("持续自动买入已关闭");
    expect(calls.some((call) => call.path.startsWith("daily-auto/"))).toBe(false);
    await click("开启持续自动买入");
    expect(wrapper!.text()).not.toContain("账户核验与运行控制");
    expect(calls.filter((call) => call.path === "daily-auto/confirm")).toHaveLength(0);
    await wrapper!
      .get('input[aria-label="持续自动买入确认文字"]')
      .setValue(preview.confirmation_phrase);
    await wrapper!.get(".daily-auto-confirm").trigger("submit");
    await flushPromises();
    expect(calls.filter((call) => call.path === "daily-auto/confirm")).toHaveLength(1);
    expect(calls.some((call) => ["control", "preview", "confirm"].includes(call.path))).toBe(false);
    expect(wrapper!.text()).toContain("无需每天点启动");
    expect(wrapper!.text()).not.toContain("持续自动买入已关闭");
    expect(wrapper!.text()).not.toContain("DAILY_AUTO_OPERATOR_DISABLED");
    expect(wrapper!.findAll("button").some((b) => b.text() === "重新核验账户")).toBe(false);
  });
  it.each(["DAILY_AUTO_OPERATOR_DISABLED", "DAILY_AUTO_SETTINGS_CHANGED"])(
    "does not show historical %s as a current stop after enabling",
    async (reason) => {
      const { state } = automatic();
      const enabled = { ...state, enabled: true, state: "WAITING", reason: "WAITING_FORMAL_PICKS" };
      current.components.engine.data!.state = "HALTED";
      current.components.engine.data!.pause_reason = reason;
      current.components.engine.data!.daily_auto = enabled;
      override = async (path) => (path === "daily-auto" ? response(enabled) : undefined);
      await start();
      expect(wrapper!.text()).toContain("持续自动买入已开启");
      expect(wrapper!.text()).not.toContain("持续自动买入已关闭");
      expect(wrapper!.text()).not.toContain(reason);
      expect(wrapper!.findAll("button").some((b) => b.text() === "重新核验账户")).toBe(false);
      // A new real fault must still be visible even before the switch is disabled.
      current.components.engine.data!.pause_reason = "QMT_TICK_CALLBACK_INVALID";
      TestEvents.instances.at(-1)!.emit(current);
      await flushPromises();
      expect(wrapper!.text()).toContain("系统安全暂停");
      expect(wrapper!.text()).toContain("连续收到异常行情");
      current.components.engine.data!.pause_reason = reason;
      current.components.engine.data!.recovery_error = "RECOVERY_RECONCILE_FAILED";
      TestEvents.instances.at(-1)!.emit(current);
      await flushPromises();
      expect(wrapper!.text()).toContain("券商数据与本地交易记录核对失败");
      expect(wrapper!.find('[role="alert"]').exists()).toBe(true);
      expect(calls.some((call) => call.path === "control")).toBe(false);
    },
  );
  it("reads back an uncertain confirmation without sending it twice", async () => {
    const { state, preview } = automatic();
    let confirmed = false;
    override = async (path) => {
      if (path === "daily-auto")
        return response(
          confirmed ? { ...state, enabled: true, consent_id: preview.preview_id } : state,
        );
      if (path === "daily-auto/preview") return response(preview);
      if (path === "daily-auto/confirm") {
        confirmed = true;
        throw new Error("connection lost");
      }
    };
    await start();
    await click("开启持续自动买入");
    await wrapper!
      .get('input[aria-label="持续自动买入确认文字"]')
      .setValue(preview.confirmation_phrase);
    await wrapper!.get(".daily-auto-confirm").trigger("submit");
    await flushPromises();
    expect(calls.filter((call) => call.path === "daily-auto/confirm")).toHaveLength(1);
    expect(wrapper!.text()).toContain("持续开关已保存并核对");
  });
  it("lets a stop fence a late enable response", async () => {
    const { state, preview } = automatic();
    let finish: ((value: Response) => void) | undefined;
    override = async (path) => {
      if (path === "daily-auto") return response(state);
      if (path === "daily-auto/preview") return response(preview);
      if (path === "daily-auto/confirm")
        return await new Promise<Response>((resolve) => {
          finish = resolve;
        });
      if (path === "daily-auto/disable")
        return response({ ...state, state: "PAUSED", reason: "OPERATOR_DISABLED" });
    };
    await start();
    await click("开启持续自动买入");
    await wrapper!
      .get('input[aria-label="持续自动买入确认文字"]')
      .setValue(preview.confirmation_phrase);
    await wrapper!.get(".daily-auto-confirm").trigger("submit");
    await flushPromises();
    await click("关闭持续自动买入");
    finish!(response({ ...state, enabled: true, consent_id: preview.preview_id }));
    await flushPromises();
    expect(wrapper!.text()).toContain("你已关闭持续自动买入");
    expect(wrapper!.text()).not.toContain("持续自动买入已开启；");
    expect(calls.filter((call) => call.path === "daily-auto/disable")).toHaveLength(1);
  });
});

describe("real QMT trading console", () => {
  it("opens independent sell configuration without changing buying", async () => {
    override = async (path) =>
      path === "sell-configuration"
        ? response({
            draft: null,
            defaults: {
              main: { mode: "TAKE_PROFIT_OR_TIME", take_profit_pct: "7.00", sell_at: "10:30" },
              chinext: { mode: "TIME_ONLY", take_profit_pct: null, sell_at: "10:50" },
              include_automatic: true,
              include_system_manual: true,
              cancel_after_seconds: 5,
            },
            execution_enabled: false,
            execution_supported: false,
          })
        : undefined;
    await start();
    await click("交易设置");
    await click("卖出配置");
    expect(wrapper!.text()).toContain("自动卖出配置");
    expect(wrapper!.text()).not.toContain("账户核验与运行控制");
    expect(wrapper!.text()).not.toContain("卖出规则未配置；");
    expect(calls.filter((c) => c.init.method === "POST")).toHaveLength(0);
    await click("买入配置");
    expect(wrapper!.text()).toContain("保存买入设置");
  });
  it.each([
    ["DAILY_AUTO_OPERATOR_DISABLED", "持续自动买入已关闭"],
    ["DAILY_AUTO_SETTINGS_CHANGED", "请重新打开主程序加载新设置"],
  ])("describes normal pause %s without reporting a system failure", async (reason, text) => {
    current.components.engine.data!.state = "HALTED";
    current.components.engine.data!.pause_reason = reason;
    await start();
    expect(wrapper!.text()).toContain(text);
    expect(wrapper!.text()).not.toContain("系统安全暂停");
    expect(wrapper!.text()).not.toContain("操作未完成");
    expect(button("重新核验账户").exists()).toBe(true);
  });
  it("keeps real market faults visible as safety pauses", async () => {
    current.components.engine.data!.state = "HALTED";
    current.components.engine.data!.pause_reason = "QMT_TICK_CALLBACK_INVALID";
    await start();
    expect(wrapper!.text()).toContain("系统安全暂停");
    expect(wrapper!.text()).toContain("连续收到异常行情");
    expect(wrapper!.find('[role="alert"]').exists()).toBe(true);
  });
  it("explains missing formal picks on the disabled start button", async () => {
    current.components.engine.data!.plan_loaded = false;
    await start();
    expect(button("等待正式名单").attributes("disabled")).toBeDefined();
    expect(button("等待正式名单").attributes("title")).toBe("等待后台正式选股结果");
    expect(calls.some((call) => call.path === "preview")).toBe(false);
  });
  it("shows recovery success beside its controls without granting trading", async () => {
    current.components.engine.data!.state = "REVIEW_REQUIRED";
    await start();
    await click("交易设置");
    expect(wrapper!.text()).toContain("账户核验已通过");
    expect(wrapper!.text()).toContain("请确认核验结果");
    expect(calls.some((call) => call.path === "confirm")).toBe(false);
  });
  it("does not display a zero trading budget for a recovery-only confirmation", async () => {
    proposal.request.action = "CONFIRM_RECOVERY";
    proposal.request.grant = null;
    await start();
    await click("交易设置");
    await click("确认核验结果");
    const dialog = wrapper!.get('[role="dialog"]');
    expect(dialog.text()).toContain("本次仅确认账户核验，不会提交买卖委托");
    expect(dialog.text()).not.toContain("当日上限");
    expect(calls.some((call) => call.path === "confirm")).toBe(false);
  });
  it("warns before closing during trading or unknown state and removes listener on unmount", async () => {
    await start();
    const close = () => {
      const event = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    };
    expect(close()).toBe(false);
    current.components.engine.data!.state = "ARMED";
    current.components.engine.data!.halted = false;
    TestEvents.instances.at(-1)!.emit(current);
    await flushPromises();
    expect(close()).toBe(true);
    TestEvents.instances.at(-1)!.onerror?.();
    await flushPromises();
    expect(close()).toBe(true);
    wrapper!.unmount();
    wrapper = undefined;
    expect(close()).toBe(false);
  });
  it("changes password using CSRF and returns to login after success", async () => {
    await start();
    await click("系统设置");
    const inputs = wrapper!.findAll('.system-password input[type="password"]');
    await inputs[0]!.setValue("old-password-1234");
    await inputs[1]!.setValue("new-password-1234");
    await inputs[2]!.setValue("new-password-1234");
    await wrapper!.get(".system-password form").trigger("submit");
    await flushPromises();
    expect(wrapper!.text()).toContain("密码已修改");
    expect(wrapper!.find(".login-strip").exists()).toBe(true);
    const call = calls.find((call) => call.path === "password")!;
    expect(call.init.headers).toMatchObject({ "X-CSRF-Token": session.csrf_token });
  });
  it("separates trading settings from system settings without submitting trading changes", async () => {
    await start();
    await click("交易设置");
    expect(wrapper!.get("h1").text()).toBe("交易设置");
    expect(wrapper!.find('[aria-label="每日最多买入股票数量"]').exists()).toBe(true);
    expect(wrapper!.find('a[href="/connect-qmt"]').exists()).toBe(false);
    expect(wrapper!.text()).not.toContain("修改登录密码");
    await click("系统设置");
    expect(wrapper!.get("h1").text()).toBe("系统设置");
    expect(wrapper!.get('a[href="/connect-qmt"]').text()).toBe("检查连接设置");
    expect(wrapper!.find('[aria-label="每日最多买入股票数量"]').exists()).toBe(false);
    expect(wrapper!.findAll("button").some((b) => b.text() === "停止全部自动发单")).toBe(false);
    expect(wrapper!.text()).toContain("修改登录密码");
    expect(wrapper!.text()).toContain("标有“待接入”的功能");
    expect(calls.filter((call) => call.init.method === "POST")).toHaveLength(0);
  });
  it("shows the selected UI and real fill columns without demo or backend-status clutter", async () => {
    await start();
    expect(wrapper!.text()).toContain("自动交易系统");
    expect(wrapper!.text()).toContain("000001.SZ");
    expect(wrapper!.text()).toContain("平安银行");
    expect(wrapper!.text()).toContain("10.0100");
    expect(wrapper!.text()).toContain("1,001.00");
    for (const text of ["演示数据", "首次识别", "后台已连接", "尾盘助手"])
      expect(wrapper!.text()).not.toContain(text);
    expect(wrapper!.findAll("th").map((th) => th.text())).toEqual([
      "股票",
      "日期 / 来源",
      "状态",
      "买入时间",
      "买入均价",
      "已成交金额",
      "自动卖出",
      "操作",
      "详情",
    ]);
  });
  it("shows actual account values and marks unrelated positions conservatively", async () => {
    dashboard.buys.rows = [];
    await start();
    await click("策略持仓");
    expect(wrapper!.text()).toContain("50,000.00");
    expect(wrapper!.text()).toContain("12,345.67");
    expect(wrapper!.text()).toContain("账户持仓，归属待核对");
  });
  it("uses quant history instead of account current-day fills", async () => {
    override = async (path) =>
      path.startsWith("history?") ? response({ items: [], has_more: false }) : undefined;
    await start();
    await click("历史记录");
    expect(wrapper!.text()).toContain("历史交易");
    expect(wrapper!.text()).not.toContain("账户当日成交");
    expect(calls.some((call) => call.path.startsWith("history?"))).toBe(true);
  });
  it("never displays MOCK account money in the real account UI", async () => {
    current.broker = "MOCK";
    await start();
    await click("策略持仓");
    expect(wrapper!.text()).not.toContain("50,000.00");
    expect(calls.filter((call) => call.path === "dashboard")).toHaveLength(0);
  });
  it("keeps read-only accounts unable to arm", async () => {
    current.broker = "QMT_READ_ONLY";
    await start();
    expect(button("启动自动买入").attributes("disabled")).toBeDefined();
    expect(wrapper!.text()).toContain("真实账户只读查看");
  });
  it("requires login, clears entered password, and does not persist credentials", async () => {
    loggedIn = false;
    await start();
    await wrapper!.get("#operator-password").setValue("operator-password-123");
    await wrapper!.get(".login-strip form").trigger("submit");
    await flushPromises();
    const login = calls.find((call) => call.path === "session" && call.init.method === "POST")!;
    expect(JSON.parse(String(login.init.body))).toEqual({
      password: "operator-password-123",
      start_board_sell: true,
    });
    expect(wrapper!.find("#operator-password").exists()).toBe(false);
    expect(sessionStorage.length).toBe(0);
    await click("系统设置");
    await click("退出登录");
    expect(wrapper!.text()).toContain("登录本机操作员");
    expect(wrapper!.text()).not.toContain("operator-password-123");
  });
  it("saves independent daily budget and exposure with no sell rule", async () => {
    await start();
    await click("交易设置");
    await wrapper!.get('[aria-label="每日最多买入股票数量"]').setValue("5");
    await wrapper!.get('[aria-label="单只股票买入金额上限"]').setValue("1234.56");
    await wrapper!.get('[aria-label="每日买入总预算"]').setValue("600000");
    await wrapper!.get('[aria-label="账户总仓位上限"]').setValue("70");
    await wrapper!.get('[aria-label="买入未成交撤单时效"]').setValue("5");
    await wrapper!.get('[aria-label="卖出未成交撤单时效"]').setValue("9");
    await wrapper!.get(".settings-form").trigger("submit");
    await flushPromises();
    const saved = calls.find(
      (call) => call.path === "trading-settings" && call.init.method === "POST",
    )!;
    const payload = JSON.parse(String(saved.init.body));
    expect(payload.settings.sell).toBeNull();
    expect(payload.settings.buy.max_daily_buy_value).toBe("600000");
    expect(payload.settings.safety.max_total_position_pct).toBe("70");
    expect(payload.settings.buy.max_daily_symbols).toBe(5);
    expect(payload.settings.buy.cancel_after_seconds).toBe(5);
    expect(payload.settings.sell_execution.cancel_after_seconds).toBe(9);
  });
  it("defaults new settings to 600000 and 70 percent", async () => {
    settings.draft = null;
    await start();
    await click("交易设置");
    expect(
      (wrapper!.get('[aria-label="买入未成交撤单时效"]').element as HTMLInputElement).value,
    ).toBe("5");
    expect(
      (wrapper!.get('[aria-label="卖出未成交撤单时效"]').element as HTMLInputElement).value,
    ).toBe("5");
    expect((wrapper!.get('[aria-label="每日买入总预算"]').element as HTMLInputElement).value).toBe(
      "600000",
    );
    expect((wrapper!.get('[aria-label="账户总仓位上限"]').element as HTMLInputElement).value).toBe(
      "70",
    );
  });
  it("does not enable overwriting settings after a read failure", async () => {
    override = async (path) =>
      path === "trading-settings"
        ? response({ code: "TRADING_SETTINGS_STORE_INVALID" }, 400)
        : undefined;
    await start();
    await click("交易设置");
    expect(wrapper!.get("fieldset").attributes("disabled")).toBeDefined();
  });
  it("reports applied-setting restart requirements explicitly", async () => {
    settings.restart_required = true;
    await start();
    expect(wrapper!.text()).toContain("新设置待加载");
    expect(button("启动自动买入").attributes("disabled")).toBeDefined();
  });
  it("separates full and partial fills in the visible progress", async () => {
    dashboard.buys.completed = 0;
    dashboard.buys.partial = 1;
    dashboard.buys.rows[0]!.state = "PARTIAL";
    await start();
    expect(wrapper!.get(".buy-progress").text()).toContain("部分成交 1 只");
    expect(wrapper!.get("tbody").text()).toContain("部分成交");
  });
  it("expands an execution row without causing any order", async () => {
    dashboard.buys.rows[0]!.signal_state = "FINISHED";
    await start();
    await wrapper!.get('[aria-label="查看000001.SZ详情"]').trigger("click");
    expect(wrapper!.text()).toContain("实际成交 100 股");
    expect(wrapper!.get(".detail-row").text()).toContain("本批执行结束");
    expect(wrapper!.get(".detail-row").text()).not.toContain("等待执行");
    expect(calls.some((call) => call.path === "control" || call.path === "confirm")).toBe(false);
  });
  it("retains uncertain confirmation IDs and never submits the confirmation again", async () => {
    override = async (path) =>
      path === "confirm" ? response({ code: "PIPE_UNAVAILABLE" }, 503) : undefined;
    await start();
    await click("启动自动买入");
    await wrapper!.get('[aria-label="操作确认文字"]').setValue(proposal.confirmation_phrase);
    await click("确认执行");
    expect(sessionStorage.getItem(key)).toBe(identity);
    expect(wrapper!.text()).toContain("操作结果待确认");
    await click("查询操作结果");
    expect(sessionStorage.getItem(key)).toBeNull();
    expect(calls.filter((call) => call.path === "confirm")).toHaveLength(1);
  });
  it("refuses mismatched confirmation phrases and expires previews", async () => {
    await start();
    await click("启动自动买入");
    await wrapper!.get('[aria-label="操作确认文字"]').setValue("wrong");
    expect(button("确认执行").attributes("disabled")).toBeDefined();
    await wrapper!.get('[aria-label="操作确认文字"]').setValue(proposal.confirmation_phrase);
    await vi.advanceTimersByTimeAsync(21000);
    expect(button("确认执行").attributes("disabled")).toBeDefined();
    await click("取消");
    expect(calls.some((call) => call.path === "confirm")).toBe(false);
  });
  it("restores pending IDs only for the matching account and only queries that request", async () => {
    writePending(key, identity);
    await start();
    expect(button("启动自动买入").attributes("disabled")).toBeDefined();
    await click("查询操作结果");
    expect(
      JSON.parse(String(calls.find((call) => call.path === "result")!.init.body)).request_id,
    ).toBe(identity);
  });
  it("keeps keyboard focus in confirmation and returns it on Escape without executing", async () => {
    wrapper = mount(NodeConsole, { attachTo: document.body });
    await flushPromises();
    (button("启动自动买入").element as HTMLButtonElement).focus();
    await click("启动自动买入");
    const input = wrapper!.get('[aria-label="操作确认文字"]');
    expect(document.activeElement).toBe(input.element);
    await input.trigger("keydown", { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(button("取消").element);
    await button("取消").trigger("keydown", { key: "Tab" });
    expect(document.activeElement).toBe(input.element);
    await input.trigger("keydown", { key: "Escape" });
    await flushPromises();
    expect(wrapper!.find('[role="dialog"]').exists()).toBe(false);
    expect(document.activeElement).toBe(button("启动自动买入").element);
    expect(calls.some((call) => call.path === "confirm")).toBe(false);
  });
  it("keeps stop available after a lost state stream and does not auto-reconnect", async () => {
    current.components.engine.data!.state = "ARMED";
    current.components.engine.data!.halted = false;
    await start();
    TestEvents.instances[0]!.onerror?.();
    await flushPromises();
    expect(wrapper!.text()).toContain("页面状态连接已中断");
    await click("交易设置");
    await click("停止全部自动发单");
    expect(calls.some((call) => call.path === "control")).toBe(true);
    await vi.advanceTimersByTimeAsync(2000);
    expect(TestEvents.instances).toHaveLength(1);
    await click("重新连接状态");
    expect(TestEvents.instances).toHaveLength(2);
  });
  it("refreshes account snapshots on a bounded timer without repeated status polling", async () => {
    await start();
    await vi.advanceTimersByTimeAsync(10000);
    expect(calls.filter((call) => call.path === "status")).toHaveLength(1);
    expect(calls.filter((call) => call.path === "dashboard")).toHaveLength(3);
  });
  it("clears private account details when the operator session expires", async () => {
    await start();
    override = async (path) =>
      path === "dashboard" ? response({ code: "OPERATOR_SESSION_EXPIRED" }, 401) : undefined;
    await click("刷新");
    expect(wrapper!.text()).toContain("登录本机操作员");
    expect(wrapper!.text()).not.toContain("平安银行");
  });
  it("does not turn an unavailable account into zero buys or empty holdings", async () => {
    dashboard.account.available = false;
    dashboard.buys.available = false;
    dashboard.buys.completed = null;
    dashboard.buys.rows = [];
    await start();
    expect(wrapper!.get(".summary").text()).not.toContain("0只");
    expect(wrapper!.text()).toContain("QMT账户数据暂不可用");
  });
  it("clears account information when the runtime account identity changes", async () => {
    await start();
    current.node_id = "node-b";
    TestEvents.instances[0]!.emit(current);
    await flushPromises();
    expect(wrapper!.text()).toContain("登录本机操作员");
    expect(wrapper!.text()).not.toContain("平安银行");
  });
  it("discards a delayed start preview after the operator halts", async () => {
    await start();
    let finish: (value: Response) => void = () => {
      throw new Error("preview not requested");
    };
    override = async (path) =>
      path === "preview"
        ? new Promise<Response>((resolve) => {
            finish = resolve;
          })
        : undefined;
    await button("启动自动买入").trigger("click");
    await flushPromises();
    await click("交易设置");
    await click("停止全部自动发单");
    finish(response(proposal));
    await flushPromises();
    expect(wrapper!.find('[aria-label="操作确认文字"]').exists()).toBe(false);
    expect(calls.filter((call) => call.path === "confirm")).toHaveLength(0);
  });
  it("shows received phases from the displayed period and keeps total holdings separate", async () => {
    dashboard.buys.decision_phases = ["1440", "1445"];
    current.components.engine.data = {
      ...current.components.engine.data,
      plan: { decision_phases: ["1440", "1445"] },
    };
    settings.draft!.settings.safety.max_positions = 8;
    await start();
    expect(wrapper!.get('[aria-label="三批选股结果"]').text()).toContain("已收到结果");
    await click("交易设置");
    await wrapper!.get('[aria-label="每日最多买入股票数量"]').setValue("2");
    await wrapper!.get("form.settings-form").trigger("submit");
    await flushPromises();
    const saved = calls.find(
      (call) => call.path === "trading-settings" && call.init.method === "POST",
    );
    const body = JSON.parse(String(saved!.init.body)) as {
      settings: { safety: { max_positions: number }; buy: { max_daily_symbols: number } };
    };
    expect(body.settings.safety.max_positions).toBe(8);
    expect(body.settings.buy.max_daily_symbols).toBe(2);
  });

  it("clears rows, counts and phases at the next trading day boundary while requests are pending", async () => {
    const boundary = Date.parse("2026-10-26T14:30:00+08:00");
    vi.setSystemTime(boundary - 1000);
    dashboard.account.observed_at = new Date(boundary - 1000).toISOString();
    dashboard.buys.display_state = "SNAPSHOT";
    dashboard.buys.decision_phases = ["1440", "1445", "1450"];
    await start();
    expect(wrapper!.get(".stock-table").text()).toContain("平安银行");
    override = async (path) => (path === "dashboard" ? new Promise<Response>(() => {}) : undefined);
    await vi.advanceTimersByTimeAsync(1000);
    expect(wrapper!.get(".stock-table").text()).not.toContain("平安银行");
    expect(wrapper!.get(".stock-table").text()).toContain("暂无本周期交易记录");
    expect(wrapper!.get('[aria-label="三批选股结果"]').text()).not.toContain("已收到结果");
    expect(wrapper!.get('[aria-label="买入进度"]').text()).not.toContain("1");
  });

  it("shows saved fill facts with their selection date when the current account is unavailable", async () => {
    vi.setSystemTime(Date.parse("2026-10-26T09:30:00+08:00"));
    dashboard.buys.display_state = "SNAPSHOT";
    dashboard.buys.decision_phases = ["1440"];
    dashboard.account.available = false;
    dashboard.account.trades = [];
    dashboard.account.orders = [];
    await start();
    expect(wrapper!.get(".stock-table").text()).toContain("已完成买入");
    expect(wrapper!.get(".stock-table").text()).toContain("10.0100");
    expect(wrapper!.get(".stock-section").text()).toContain("当前选股日期：2026-10-23");
    expect(wrapper!.get(".stock-section").text()).toContain("上次保存的交易快照");
    expect(wrapper!.get('[aria-label="三批选股结果"]').text()).toContain("已收到结果");
  });

  it("keeps today's results visible while the calendar refresh is pending", async () => {
    dashboard.buys.calendar_pending = true;
    await start();
    expect(wrapper!.get(".stock-table").text()).toContain("平安银行");
    expect(wrapper!.get(".stock-section").text()).toContain("正在自动获取交易日历");
    expect(wrapper!.text()).not.toContain("交易日历范围不足，请更新日历");
  });
});
