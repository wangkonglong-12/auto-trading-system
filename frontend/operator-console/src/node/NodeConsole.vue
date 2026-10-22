<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { errorText, nodeRequest, NodeApiError } from "./api";
import { pendingKey, readPending, writePending } from "./pending";
import { validateSettings } from "./settingsValidation";
import type { ComponentName, NodeStatus, Preview, PreviewAction, Receipt, Session } from "./api";

type View = "home" | "account" | "rules" | "test" | "trades" | "messages" | "settings";
interface RuleDraft {
  version: number;
  saved_at: string;
  rules: { take_profit_pct: string; stop_loss_pct: string; sell_by: string };
}
interface RuleResponse {
  draft: RuleDraft | null;
  simulation_ready: boolean;
  active: boolean;
  active_version: number | null;
}
interface RuleTestResult {
  reasons: string[];
  should_sell: boolean;
  change_pct: string;
  actual_order_sent: false;
}
interface TradingSettingsDraft {
  version: number;
  saved_at: string;
  settings: {
    buy: {
      order_method: "ASK1_PROTECTED_LIMIT";
      max_daily_buy_value: string;
      max_symbol_buy_value: string;
      max_order_value: string;
      cancel_after_seconds: number;
      max_retries: number;
    };
    sell: { take_profit_pct: string; stop_loss_pct: string; sell_by: string };
    safety: {
      max_daily_loss: string;
      max_positions: number;
      emergency_liquidation_enabled: boolean;
    };
    advanced: {
      max_order_quantity: number;
      max_chase_bps: number;
      max_spread_bps: number;
      quote_max_age_ms: number;
    };
  };
}
interface TradingSettingsResponse {
  draft: TradingSettingsDraft | null;
  buy_configured: boolean;
  simulation_ready: boolean;
  live_ready?: boolean;
  restart_required?: boolean;
  active: boolean;
  active_version: number | null;
}
interface TradeDetails {
  available: boolean;
  observed_at?: string;
  cash?: string;
  total_equity?: string;
  positions: { symbol: string; quantity: number; sellable: number; frozen: number | null }[];
  orders: {
    client_order_id: string;
    symbol: string;
    side: string;
    status: string;
    quantity: number;
    filled: number;
    remaining: number;
    reason: string;
    owned_by_system: boolean;
    cancelable: boolean;
  }[];
  trades: {
    trade_id: string;
    symbol: string;
    side: string;
    quantity: number;
    price: string;
    traded_at: string;
  }[];
  truncated?: boolean;
}
interface LiquidationPreview {
  status: string;
  confirmation_phrase: string;
  positions: { lot_id: string; symbol: string; cost: string }[];
}
interface EntryProgress {
  symbol: string;
  state: string;
}
interface EntryTarget {
  symbol: string;
  target_quantity: number;
  reason: string;
  decided_at: string;
}
interface ActivityEvent {
  occurred_at: string;
  category: string;
  state: string;
  symbol?: string;
  detail?: string;
}
const view = ref<View>("home");
const ruleDraft = ref<RuleDraft | null>(null);
const settingsDraft = ref<TradingSettingsDraft | null>(null);
const settingsChecked = ref(false);
const settingsLoading = ref(false);
const settingsLoadError = ref("");
let settingsLoadGeneration = -1;
const simulationReady = ref(false);
const restartRequired = ref(false);
const activeRuleVersion = ref<number | null>(null);
const takeProfit = ref("");
const stopLoss = ref("");
const sellBy = ref("14:30");
const maxDailyBuy = ref("0");
const maxSymbolBuy = ref("0");
const maxOrderValue = ref("0");
const cancelAfterSeconds = ref("10");
const maxRetries = ref("3");
const maxDailyLoss = ref("0");
const maxPositions = ref("0");
const emergencyLiquidationEnabled = ref(false);
const maxOrderQuantity = ref("10000");
const maxChaseBps = ref("30");
const maxSpreadBps = ref("20");
const quoteMaxAgeMs = ref("1000");
const ruleMessage = ref("");
const attemptedSettingsSave = ref(false);
const testCost = ref("10.00");
const testPrice = ref("10.00");
const testTime = ref("10:00");
const testResult = ref<RuleTestResult | null>(null);
const tradeDetails = ref<TradeDetails | null>(null);
const tradeError = ref("");
const pendingCancel = ref("");
const liquidationPreview = ref<LiquidationPreview | null>(null);
const liquidationPhrase = ref("");
const views: { key: View; title: string }[] = [
  { key: "home", title: "首页" },
  { key: "account", title: "QMT账户" },
  { key: "rules", title: "尾盘策略" },
  { key: "trades", title: "交易监控" },
  { key: "messages", title: "消息与日志" },
  { key: "settings", title: "系统设置" },
];
const enteredRules = computed(() => ({
  take_profit_pct: String(takeProfit.value),
  stop_loss_pct: String(stopLoss.value),
  sell_by: sellBy.value,
}));
function inputNumber(value: unknown): number {
  return String(value).trim() === "" ? Number.NaN : Number(value);
}
const enteredSettings = computed(() => ({
  buy: {
    order_method: "ASK1_PROTECTED_LIMIT" as const,
    max_daily_buy_value: String(maxDailyBuy.value),
    max_symbol_buy_value: String(maxSymbolBuy.value),
    max_order_value: String(maxOrderValue.value),
    cancel_after_seconds: inputNumber(cancelAfterSeconds.value),
    max_retries: inputNumber(maxRetries.value),
  },
  sell: enteredRules.value,
  safety: {
    max_daily_loss: String(maxDailyLoss.value),
    max_positions: inputNumber(maxPositions.value),
    emergency_liquidation_enabled: emergencyLiquidationEnabled.value,
  },
  advanced: {
    max_order_quantity: inputNumber(maxOrderQuantity.value),
    max_chase_bps: inputNumber(maxChaseBps.value),
    max_spread_bps: inputNumber(maxSpreadBps.value),
    quote_max_age_ms: inputNumber(quoteMaxAgeMs.value),
  },
}));
const settingsIssues = computed(() => validateSettings(enteredSettings.value));
const sellTimeError = computed(() =>
  (sellBy.value >= "09:30" && sellBy.value <= "11:25") ||
  (sellBy.value >= "13:00" && sellBy.value <= "14:50")
    ? ""
    : "请选择 09:30–11:25 或 13:00–14:50，午休和临近收盘时间不能设为最晚卖出。",
);
const reasonText: Record<string, string> = {
  TAKE_PROFIT: "达到止盈幅度",
  STOP_LOSS: "达到止损幅度",
  SELL_BY_TIME: "到了最晚卖出时间",
};
const executionText: Record<string, string> = {
  WAITING_WINDOW: "等待买入时间窗",
  WAITING_MARKET: "等待有效行情",
  WAITING_CAPACITY: "已产生目标，等待额度与流动性",
  ORDER_ACTIVE: "委托已发出，等待券商结果",
  FINISHED: "计划数量已完成",
  WINDOW_CLOSED: "买入时间窗已结束",
  CHILD_LIMIT: "已达到重试次数上限",
  MISSED_ENTRY_WINDOW: "收到信号时已错过执行时间",
  POSITION_CAPACITY: "已达到持仓数量上限",
  SKIPPED_MIN_ORDER: "可买数量不足最小申报单位",
  ELIGIBLE: "买入目标已确认",
  BUY_TARGET_READY: "买入目标已确认",
  BUY_TARGET_SKIPPED: "本次不买入",
  SUBMITTING: "正在报单",
  ACKED: "券商已受理",
  CANCEL_PENDING: "正在撤单",
  PARTIAL_FILL_CANCEL_PENDING: "部分成交，正在撤单",
  PARTIALLY_FILLED_CANCELLED: "部分成交后已撤单",
  CANCELLED: "已撤单",
  PARTIALLY_FILLED: "部分成交",
  FILLED: "全部成交",
  REJECTED: "券商拒单",
  UNKNOWN: "券商状态待核对",
  REQUESTED: "已请求紧急清仓",
};

const status = ref<NodeStatus | null>(null);
const session = ref<Session | null>(null);
const password = ref("");
const error = ref("");
const disconnected = ref(false);
const busy = ref(false);
const halting = ref(false);
const pauseAcknowledgement = ref<{
  nodeId: string;
  bootId: unknown;
  stateVersion: number;
} | null>(null);
const preview = ref<Preview | null>(null);
const phrase = ref("");
const pendingId = ref("");
const trackingUnavailable = ref(false);
let trackingKey = "";
const receipt = ref<Receipt | null>(null);
const clock = ref(Date.now());
const receivedAt = ref(0);
let timer: ReturnType<typeof setTimeout> | undefined;
let clockTimer: ReturnType<typeof setInterval> | undefined;
let alive = true;
let generation = 0;
const lifecycle = new AbortController();

const engine = computed(() => status.value?.components.engine.data);
const installedLimits = computed(
  () => engine.value?.installed_limits as Record<string, unknown> | undefined,
);
const previewDirections = computed(() => {
  const directions = preview.value?.request.grant?.directions;
  return Array.isArray(directions) ? directions.join(" / ") : "未提供";
});
const plan = computed(() => engine.value?.plan as Record<string, unknown> | null | undefined);
const brokerAccount = computed(
  () => engine.value?.broker_account as Record<string, unknown> | null | undefined,
);
const marketSession = computed(
  () => engine.value?.market_session as Record<string, unknown> | null | undefined,
);
const entryProgress = computed<EntryProgress[]>(() =>
  Array.isArray(marketSession.value?.entry_progress)
    ? (marketSession.value.entry_progress as EntryProgress[])
    : [],
);
const entryTargets = computed<EntryTarget[]>(() =>
  Array.isArray(marketSession.value?.entry_targets)
    ? (marketSession.value.entry_targets as EntryTarget[])
    : [],
);
const recentActivity = computed<ActivityEvent[]>(() =>
  Array.isArray(marketSession.value?.recent_activity)
    ? (marketSession.value.recent_activity as ActivityEvent[])
    : [],
);
const subscriptionCount = computed(() => Number(marketSession.value?.subscription_count ?? 0));
const signalState = computed(() => text(marketSession.value?.driver_state ?? "WAITING_MARKET"));
const isUnconfigured = computed(() => status.value?.node_id.startsWith("unconfigured-") === true);
const isQmt = computed(() => !!status.value && status.value.broker !== "MOCK");
const isTradingProfile = computed(() => status.value?.broker === "QMT_TRADING");
const modeLabel = computed(() =>
  isUnconfigured.value
    ? "尚未连接 QMT"
    : status.value?.broker === "QMT_TRADING"
      ? "QMT 实盘环境"
      : status.value?.broker === "QMT_READ_ONLY"
        ? "QMT 真实只读"
        : "MOCK 模拟环境",
);
const agentResult = computed(
  () => status.value?.components.agent.data?.last_result as Record<string, unknown> | undefined,
);
const noStatusErrors = computed(
  () =>
    !!status.value &&
    (["engine", "agent", "supervisor"] as const).every(
      (key) => !status.value?.components[key].error,
    ),
);
const planSymbols = computed(() =>
  Array.isArray(plan.value?.symbols) ? plan.value.symbols.join("、") : "—",
);
const planSymbolCount = computed(() =>
  Array.isArray(plan.value?.symbols) ? plan.value.symbols.length : 0,
);
const positionCount = computed(
  () => tradeDetails.value?.positions.length ?? Number(brokerAccount.value?.position_count ?? 0),
);
const orderCount = computed(
  () => tradeDetails.value?.orders.length ?? Number(brokerAccount.value?.order_count ?? 0),
);
const tradeCount = computed(
  () => tradeDetails.value?.trades.length ?? Number(brokerAccount.value?.trade_count ?? 0),
);
const accountConnectionLabel = computed(() => {
  if (!fresh.value) return "连接待检查";
  if (isUnconfigured.value) return "QMT 尚未连接";
  if (status.value?.broker === "MOCK") return "模拟账户已连接";
  return brokerAccount.value ? "QMT账户已连接" : "QMT账户等待快照";
});
function observed(key: ComponentName): string {
  const value = status.value?.components[key].observed_at;
  return value ? new Date(value).toLocaleTimeString("zh-CN", { hour12: false }) : "尚无";
}
function observedAccount(): string {
  const value = tradeDetails.value?.observed_at ?? brokerAccount.value?.observed_at;
  return value ? new Date(String(value)).toLocaleString("zh-CN", { hour12: false }) : "—";
}
const fresh = computed(
  () =>
    !disconnected.value &&
    clock.value - receivedAt.value < 5000 &&
    status.value?.components.engine.availability === "CURRENT",
);
const automaticTradingPaused = computed(() => {
  if (!fresh.value) return false;
  if (engine.value?.halted === true || engine.value?.state === "HALTED") return true;
  const acknowledged = pauseAcknowledgement.value;
  return (
    acknowledged !== null &&
    acknowledged.nodeId === status.value?.node_id &&
    acknowledged.bootId === engine.value?.boot_id &&
    Number(engine.value?.state_version ?? 0) <= acknowledged.stateVersion
  );
});
const pauseButtonLabel = computed(() =>
  halting.value
    ? "正在暂停自动交易…"
    : automaticTradingPaused.value
      ? "自动交易已暂停"
      : "立即急停",
);
const receiptLabel = computed(() =>
  receipt.value?.action === "HALT" && receipt.value.status === "APPLIED"
    ? "暂停操作已完成：自动交易已暂停"
    : text(receipt.value?.status),
);
const runningRuleVersion = computed(() =>
  fresh.value && !automaticTradingPaused.value && engine.value?.state === "ARMED"
    ? activeRuleVersion.value
    : null,
);
const canPreview = computed(
  () =>
    !isUnconfigured.value &&
    status.value?.broker !== "QMT_READ_ONLY" &&
    !!session.value &&
    fresh.value &&
    !busy.value &&
    !pendingId.value &&
    !trackingUnavailable.value,
);
const canStartTrading = computed(
  () => canPreview.value && settingsChecked.value && settingsDraft.value !== null,
);
const tradingSettingsSummary = computed(() => {
  if (isUnconfigured.value) return "尚未连接 QMT，自动交易已锁定";
  if (!session.value) return "登录后检查交易设置";
  if (!settingsChecked.value) return "正在检查交易设置";
  if (!settingsDraft.value) return "请到“尾盘策略 → 交易设置”填写买入金额、卖出条件和风险上限";
  if (automaticTradingPaused.value) return "自动交易已暂停，交易设置已保存";
  if (runningRuleVersion.value)
    return `${isTradingProfile.value ? "实盘" : "模拟"}运行第 ${runningRuleVersion.value} 版`;
  return `已保存草稿第 ${settingsDraft.value.version} 版，尚未启用`;
});
async function openTradingSettings(): Promise<void> {
  view.value = "rules";
  await nextTick();
  document.getElementById("trading-settings-heading")?.focus();
}
const liquidationSymbolSummary = computed(
  () =>
    liquidationPreview.value?.positions.map((item) => item.symbol).join("、") ||
    "当前没有可清仓持仓",
);
const expired = computed(
  () => !preview.value || Date.parse(preview.value.request.deadline) <= clock.value,
);
const components: { key: ComponentName; title: string; note: string }[] = [
  { key: "engine", title: "交易引擎", note: "检查主循环是否持续推进" },
  { key: "supervisor", title: "进程监护", note: "启动、异常恢复与失败熔断" },
  { key: "agent", title: "同步代理", note: "计划拉取与每日数据回传" },
];
const labels: Record<string, string> = {
  CURRENT: "状态已更新",
  STALE: "状态过期",
  UNAVAILABLE: "进程未连接",
  IDLE: "等待任务",
  RUNNING: "任务执行中",
  SYNCED: "最近任务已同步",
  PENDING: "等待重试",
  NEEDS_ATTENTION: "需要人工检查",
  SKIPPED: "本次任务已跳过",
  HEALTHY: "监护中",
  RECOVERING: "恢复核验中",
  REVIEW_REQUIRED: "等待恢复确认",
  RECOVERY_BLOCKED: "恢复受阻",
  READY_PAUSED: "就绪，保持暂停",
  ARMED: "自动交易已启动",
  HALTED: "已急停",
  APPLIED: "已生效",
  REQUESTED: "已提交核验请求",
  REJECTED: "已拒绝",
  SUBMITTING: "正在提交",
  ACKED: "券商已受理",
  CANCEL_PENDING: "正在撤单",
  PARTIAL_FILL_CANCEL_PENDING: "部分成交，正在撤单",
  PARTIALLY_FILLED_CANCELLED: "部分成交后已撤单",
  CANCELLED: "已撤单",
  PARTIALLY_FILLED: "部分成交",
  FILLED: "已全部成交",
  UNKNOWN: "券商状态待核对",
  WAITING_MARKET: "等待有效行情",
  WAITING_PLAN: "等待选股清单",
  SUBSCRIBED: "行情已订阅",
  READY: "策略已就绪",
  FAILED: "策略已停止",
};
function text(value: unknown): string {
  if (value == null) return "—";
  const raw = String(value);
  if (raw === "ARMED") return isTradingProfile.value ? "实盘自动交易已启动" : "模拟交易已启动";
  return labels[raw] ?? raw;
}
function executionState(value: string): string {
  const base = value.split(":", 1)[0] ?? value;
  return executionText[base] ?? value;
}
function targetFor(symbol: string): EntryTarget | undefined {
  return entryTargets.value.find((item) => item.symbol === symbol);
}
function targetSummary(symbol: string): string {
  const target = targetFor(symbol);
  if (!target) return "尚未生成买入目标，也没有向券商发送委托。";
  return target.target_quantity > 0
    ? `已产生买入目标 ${target.target_quantity} 股；是否下单及成交请继续看委托和成交。`
    : `未生成买单：${executionState(target.reason)}`;
}
function availability(key: ComponentName): string {
  if (disconnected.value || clock.value - receivedAt.value >= 5000) return "页面连接中断";
  return text(status.value?.components[key].availability);
}
function failed(reason: unknown): void {
  error.value = errorText(reason);
  if (reason instanceof NodeApiError && reason.status === 401) {
    session.value = null;
    settingsChecked.value = false;
    settingsDraft.value = null;
    simulationReady.value = false;
    restartRequired.value = false;
    activeRuleVersion.value = null;
    preview.value = null;
  }
}
async function loadRules(): Promise<void> {
  if (!session.value) return;
  try {
    const result = await nodeRequest<RuleResponse>(
      "sell-rules",
      "GET",
      undefined,
      undefined,
      lifecycle.signal,
    );
    applyRuleResponse(result);
    if (result.draft) {
      takeProfit.value = result.draft.rules.take_profit_pct;
      stopLoss.value = result.draft.rules.stop_loss_pct;
      sellBy.value = result.draft.rules.sell_by.slice(0, 5);
    }
  } catch (reason) {
    failed(reason);
  }
}
async function loadTradingSettings(): Promise<void> {
  if (!session.value || (settingsLoading.value && settingsLoadGeneration === generation)) return;
  const expected = generation;
  settingsLoadGeneration = expected;
  settingsLoading.value = true;
  settingsLoadError.value = "";
  try {
    const result = await nodeRequest<TradingSettingsResponse>(
      "trading-settings",
      "GET",
      undefined,
      undefined,
      lifecycle.signal,
    );
    if (!alive || expected !== generation) return;
    applyTradingSettingsResponse(result);
    if (!result.draft) {
      await loadRules();
      return;
    }
    const current = result.draft.settings;
    maxDailyBuy.value = current.buy.max_daily_buy_value;
    maxSymbolBuy.value = current.buy.max_symbol_buy_value;
    maxOrderValue.value = current.buy.max_order_value;
    cancelAfterSeconds.value = String(current.buy.cancel_after_seconds);
    maxRetries.value = String(current.buy.max_retries);
    takeProfit.value = current.sell.take_profit_pct;
    stopLoss.value = current.sell.stop_loss_pct;
    sellBy.value = current.sell.sell_by.slice(0, 5);
    maxDailyLoss.value = current.safety.max_daily_loss;
    maxPositions.value = String(current.safety.max_positions);
    emergencyLiquidationEnabled.value = current.safety.emergency_liquidation_enabled;
    maxOrderQuantity.value = String(current.advanced.max_order_quantity);
    maxChaseBps.value = String(current.advanced.max_chase_bps);
    maxSpreadBps.value = String(current.advanced.max_spread_bps);
    quoteMaxAgeMs.value = String(current.advanced.quote_max_age_ms);
  } catch (reason) {
    if (alive && expected === generation) {
      settingsChecked.value = false;
      settingsLoadError.value = "读取已保存设置失败，请重试。读取成功前不能修改或覆盖原设置。";
      failed(reason);
    }
  } finally {
    if (expected === settingsLoadGeneration) settingsLoading.value = false;
  }
}
function applyRuleResponse(result: RuleResponse): void {
  ruleDraft.value = result.draft;
  simulationReady.value = result.simulation_ready;
  activeRuleVersion.value = result.active ? result.active_version : null;
}
function applyTradingSettingsResponse(result: TradingSettingsResponse): void {
  settingsDraft.value = result.draft;
  settingsChecked.value = true;
  simulationReady.value = result.simulation_ready || result.live_ready === true;
  restartRequired.value = result.restart_required === true;
  activeRuleVersion.value = result.active ? result.active_version : null;
}
async function saveRules(): Promise<void> {
  if (!session.value || !settingsChecked.value || busy.value) return;
  ruleMessage.value = "";
  error.value = "";
  attemptedSettingsSave.value = true;
  if (settingsIssues.value.length) {
    await nextTick();
    document.getElementById("settings-errors")?.focus();
    return;
  }
  await operation(
    () =>
      nodeRequest<TradingSettingsResponse>(
        "trading-settings",
        "POST",
        {
          expected_version: settingsDraft.value?.version ?? 0,
          settings: enteredSettings.value,
        },
        session.value?.csrf_token,
        lifecycle.signal,
      ),
    (result) => {
      attemptedSettingsSave.value = false;
      applyTradingSettingsResponse(result);
      ruleMessage.value = isUnconfigured.value
        ? "交易设置已保存。连接 QMT 后才能启用自动交易。"
        : result.restart_required
          ? "交易设置已安全保存。请关闭自动交易系统主窗口并重新打开，重启后新设置才会生效。"
          : result.simulation_ready
            ? "交易设置已保存，可进行模拟启用确认。"
            : "交易设置草稿已保存。保存不会自动启动实盘。";
    },
  );
}
async function testRules(): Promise<void> {
  if (!session.value || busy.value) return;
  testResult.value = null;
  const today = new Date().toLocaleDateString("sv-SE", { timeZone: "Asia/Shanghai" });
  await operation(
    () =>
      nodeRequest<RuleTestResult>(
        "sell-rules/test",
        "POST",
        {
          rules: enteredRules.value,
          buy_cost: testCost.value,
          current_price: testPrice.value,
          observed_at: `${today}T${testTime.value}:00+08:00`,
        },
        session.value?.csrf_token,
        lifecycle.signal,
      ),
    (result) => {
      testResult.value = result;
    },
  );
}
async function loadTrades(): Promise<void> {
  if (!session.value) return;
  tradeError.value = "";
  try {
    tradeDetails.value = await nodeRequest<TradeDetails>(
      "trades",
      "GET",
      undefined,
      undefined,
      lifecycle.signal,
    );
  } catch (reason) {
    tradeDetails.value = null;
    tradeError.value = errorText(reason);
  }
}
async function cancelOrder(clientOrderId: string): Promise<void> {
  if (!session.value || busy.value || pendingCancel.value !== clientOrderId) return;
  pendingCancel.value = "";
  await operation(
    () =>
      nodeRequest(
        "orders/cancel",
        "POST",
        { client_order_id: clientOrderId },
        session.value?.csrf_token,
        lifecycle.signal,
      ),
    () => {
      ruleMessage.value = "撤单请求已提交，正在等待券商确认。";
      void loadTrades();
    },
  );
}
async function previewLiquidation(): Promise<void> {
  if (!session.value || busy.value) return;
  await operation(
    () =>
      nodeRequest<LiquidationPreview>(
        "emergency-liquidation",
        "GET",
        undefined,
        undefined,
        lifecycle.signal,
      ),
    (result) => {
      liquidationPreview.value = result;
      liquidationPhrase.value = "";
    },
  );
}
async function executeLiquidation(): Promise<void> {
  if (
    !session.value ||
    busy.value ||
    !liquidationPreview.value ||
    liquidationPhrase.value !== liquidationPreview.value.confirmation_phrase
  )
    return;
  await operation(
    () =>
      nodeRequest<LiquidationPreview>(
        "emergency-liquidation",
        "POST",
        { phrase: liquidationPhrase.value },
        session.value?.csrf_token,
        lifecycle.signal,
      ),
    () => {
      liquidationPreview.value = null;
      liquidationPhrase.value = "";
      ruleMessage.value =
        "紧急清仓请求已提交；系统会先撤销未完成委托，再按最新买一价卖出策略持仓。";
      void loadTrades();
    },
  );
}
async function refresh(): Promise<void> {
  try {
    const value = await nodeRequest<NodeStatus>(
      "status",
      "GET",
      undefined,
      undefined,
      lifecycle.signal,
    );
    if (!alive) return;
    status.value = value;
    const key = pendingKey(value.node_id, value.account_id);
    if (key !== trackingKey) {
      try {
        pendingId.value = readPending(key);
        trackingKey = key;
        trackingUnavailable.value = false;
      } catch {
        trackingUnavailable.value = true;
      }
    }
    receivedAt.value = Date.now();
    disconnected.value = false;
  } catch {
    if (alive) disconnected.value = true;
  } finally {
    // Schedule after completion: no overlapping polls or unbounded request queue.
    if (alive) timer = setTimeout(() => void refresh(), 1000);
  }
}
async function restore(): Promise<void> {
  const expected = generation;
  try {
    const value = await nodeRequest<Session>(
      "session",
      "GET",
      undefined,
      undefined,
      lifecycle.signal,
    );
    if (alive && expected === generation) {
      session.value = value;
      void loadTradingSettings();
      void loadTrades();
    }
  } catch {
    /* A fresh page is legitimately logged out. */
  }
}
async function operation<T>(work: () => Promise<T>, apply: (value: T) => void): Promise<void> {
  busy.value = true;
  error.value = "";
  const expected = generation;
  try {
    const value = await work();
    if (alive && expected === generation) apply(value);
  } catch (reason) {
    if (alive && expected === generation) failed(reason);
  } finally {
    if (alive && expected === generation) busy.value = false;
  }
}
async function login(): Promise<void> {
  if (busy.value || !status.value?.operator_access_configured) return;
  generation++;
  const secret = password.value;
  password.value = "";
  await operation(
    () =>
      nodeRequest<Session>("session", "POST", { password: secret }, undefined, lifecycle.signal),
    (value) => {
      session.value = value;
      void loadTradingSettings();
      void loadTrades();
    },
  );
}
async function logout(): Promise<void> {
  if (busy.value || halting.value) return;
  await operation(
    () => nodeRequest("session", "DELETE", undefined, session.value?.csrf_token, lifecycle.signal),
    () => {
      generation++;
      session.value = null;
      settingsChecked.value = false;
      settingsLoadError.value = "";
      settingsDraft.value = null;
      simulationReady.value = false;
      activeRuleVersion.value = null;
      tradeDetails.value = null;
      preview.value = null;
      phrase.value = "";
      busy.value = false;
    },
  );
}
async function halt(): Promise<void> {
  if (!session.value || halting.value || automaticTradingPaused.value) return;
  const acknowledgement = {
    nodeId: status.value?.node_id ?? "",
    bootId: engine.value?.boot_id,
    stateVersion: Number(engine.value?.state_version ?? 0),
  };
  halting.value = true;
  // Invalidate an in-flight preview/confirmation UI callback immediately. A
  // pending confirmed request still retains its lookup identity below.
  generation++;
  preview.value = null;
  phrase.value = "";
  busy.value = false;
  error.value = "";
  try {
    const value = await nodeRequest<Receipt>(
      "control",
      "POST",
      { action: "HALT" },
      session.value.csrf_token,
      lifecycle.signal,
    );
    if (alive) {
      receipt.value = value;
      if (value.status === "APPLIED" && value.action === "HALT") {
        pauseAcknowledgement.value = acknowledgement;
      }
    }
  } catch (reason) {
    if (alive) failed(reason);
  } finally {
    if (alive) halting.value = false;
  }
}
async function recover(): Promise<void> {
  if (!canPreview.value || halting.value) return;
  preview.value = null;
  await operation(
    () =>
      nodeRequest<Receipt>(
        "control",
        "POST",
        { action: "RECOVER" },
        session.value?.csrf_token,
        lifecycle.signal,
      ),
    (value) => {
      receipt.value = value;
    },
  );
}
async function prepare(action: PreviewAction): Promise<void> {
  if (!canPreview.value || halting.value) return;
  preview.value = null;
  phrase.value = "";
  receipt.value = null;
  await operation(
    () =>
      nodeRequest<Preview>(
        "preview",
        "POST",
        { action },
        session.value?.csrf_token,
        lifecycle.signal,
      ),
    (value) => {
      preview.value = value;
      view.value = "settings";
    },
  );
}
async function confirm(): Promise<void> {
  const current = preview.value;
  if (
    !canPreview.value ||
    halting.value ||
    !current ||
    expired.value ||
    phrase.value !== current.confirmation_phrase
  )
    return;
  // Set before sending. A lost response is uncertain, never a reason to repeat.
  try {
    writePending(trackingKey, current.request.request_id);
  } catch {
    trackingUnavailable.value = true;
    return;
  }
  pendingId.value = current.request.request_id;
  preview.value = null;
  phrase.value = "";
  await operation(
    () =>
      nodeRequest<Receipt>(
        "confirm",
        "POST",
        { preview_id: current.preview_id, phrase: current.confirmation_phrase },
        session.value?.csrf_token,
        lifecycle.signal,
      ),
    acceptReceipt,
  );
}
function acceptReceipt(value: Receipt): void {
  if (value.request_id !== pendingId.value) throw new NodeApiError("CONTROL_RESULT_UNCERTAIN");
  receipt.value = value;
  if (value.status === "APPLIED" || value.status === "REJECTED") {
    writePending(trackingKey, "");
    pendingId.value = "";
    if (value.status === "APPLIED") setTimeout(() => void loadTradingSettings(), 300);
  }
}
async function lookup(): Promise<void> {
  if (!session.value || busy.value || !pendingId.value) return;
  await operation(
    () =>
      nodeRequest<Receipt>(
        "result",
        "POST",
        { request_id: pendingId.value },
        session.value?.csrf_token,
        lifecycle.signal,
      ),
    acceptReceipt,
  );
}
onMounted(() => {
  void refresh();
  void restore();
  clockTimer = setInterval(() => {
    clock.value = Date.now();
  }, 500);
});
watch(view, (current) => {
  if (current === "account" || current === "trades" || current === "messages") void loadTrades();
});
onBeforeUnmount(() => {
  alive = false;
  lifecycle.abort();
  clearTimeout(timer);
  clearInterval(clockTimer);
});
</script>

<template>
  <div class="node-console">
    <header class="node-header">
      <a class="node-brand" href="#overview">
        <span class="node-mark">AT</span>
        <span>
          自动交易
          <small>个人交易操作台</small>
        </span>
      </a>
      <span class="mode-badge">{{ modeLabel }}</span>
    </header>
    <main id="overview">
      <nav class="simple-nav" aria-label="主要页面">
        <button
          v-for="item in views"
          :key="item.key"
          :class="{ selected: view === item.key }"
          @click="view = item.key"
        >
          {{ item.title }}
        </button>
      </nav>
      <section v-if="view === 'home'" class="simple-page">
        <div class="page-heading">
          <div>
            <p class="eyebrow">今日概况</p>
            <h1>自动交易首页</h1>
            <p>先看账户和尾盘策略是否正常，需要处理的事情会直接显示在这里。</p>
          </div>
          <span class="status-badge" :class="{ good: fresh }">{{ accountConnectionLabel }}</span>
        </div>
        <div class="panel hero-status">
          <div>
            <strong>
              {{
                !fresh
                  ? "连接或数据待核对"
                  : isUnconfigured
                    ? "尚未连接 QMT，自动交易已锁定"
                    : automaticTradingPaused
                      ? "自动交易已暂停"
                      : session && settingsChecked && !settingsDraft
                        ? "交易设置未完成，自动交易已锁定"
                        : status?.broker === "QMT_READ_ONLY"
                          ? "真实账户只读观察"
                          : status?.broker === "QMT_TRADING"
                            ? engine?.state === "ARMED"
                              ? "实盘自动交易运行中"
                              : "QMT实盘账户已连接，自动交易等待本机启动"
                            : engine?.state === "ARMED"
                              ? "模拟自动交易运行中"
                              : "模拟环境已连接，自动交易未启用"
              }}
            </strong>
            <p>账户 {{ status?.account_id ?? "读取中" }} · 最近更新 {{ observed("engine") }}</p>
            <p>
              量化后台在 14:30–14:55 提供当日入选股票；本机核验后负责订阅行情和买入。{{
                tradingSettingsSummary
              }}。
            </p>
            <p v-if="automaticTradingPaused">
              已停止自动发单。已有持仓仍保留，已提交的委托请到“交易监控”核对。
            </p>
            <button v-if="session" @click="openTradingSettings">前往交易设置</button>
          </div>
          <button
            v-if="session"
            class="danger"
            :disabled="halting || automaticTradingPaused"
            @click="halt"
          >
            {{ pauseButtonLabel }}
          </button>
          <button v-else class="primary" @click="view = 'settings'">登录后操作</button>
        </div>
        <div class="summary-grid" aria-label="账户和交易概况">
          <article class="panel summary-card">
            <span>{{ isQmt ? "QMT返回总资产" : "总资产" }}</span>
            <strong>
              {{ tradeDetails?.total_equity ? `${tradeDetails.total_equity} 元` : "—" }}
            </strong>
          </article>
          <article class="panel summary-card">
            <span>{{ isQmt ? "QMT返回可用资金" : "可用资金" }}</span>
            <strong>{{ tradeDetails?.cash ? `${tradeDetails.cash} 元` : "—" }}</strong>
          </article>
          <article class="panel summary-card">
            <span>当前持仓</span>
            <strong>{{ positionCount }} 只</strong>
          </article>
          <article class="panel summary-card">
            <span>今日成交</span>
            <strong>{{ tradeCount }} 笔</strong>
          </article>
        </div>
        <div class="panel workflow-panel">
          <div class="card-top">
            <div>
              <p class="eyebrow">尾盘策略</p>
              <h2>今天执行到哪一步</h2>
            </div>
            <button @click="view = 'trades'">查看详细进度</button>
          </div>
          <div class="workflow-strip">
            <div :class="{ done: !!plan }">
              <b>1</b>
              <span>获取选股</span>
              <small>{{ plan ? `${planSymbolCount} 只` : "等待 14:30–14:55" }}</small>
            </div>
            <div :class="{ done: !!plan && fresh }">
              <b>2</b>
              <span>订阅行情</span>
              <small>{{ subscriptionCount ? `已订阅 ${subscriptionCount} 只` : "等待选股" }}</small>
            </div>
            <div :class="{ done: orderCount > 0 }">
              <b>3</b>
              <span>买入与撤单</span>
              <small>{{ orderCount ? `${orderCount} 笔委托` : "暂无委托" }}</small>
            </div>
            <div :class="{ done: positionCount > 0 || tradeCount > 0 }">
              <b>4</b>
              <span>持仓与卖出</span>
              <small>{{ positionCount ? `${positionCount} 只持仓` : "暂无持仓" }}</small>
            </div>
          </div>
        </div>
        <div class="quick-grid">
          <button @click="view = 'account'">查看QMT账户</button>
          <button @click="view = 'rules'">配置尾盘策略</button>
          <button @click="view = 'trades'">查看交易监控</button>
          <button @click="view = 'messages'">查看消息日志</button>
        </div>
      </section>
      <section v-if="view === 'account'" class="simple-page">
        <div class="page-heading">
          <div>
            <p class="eyebrow">账户</p>
            <h1>QMT账户</h1>
            <p>查看QMT返回的账户数据；完成柜台归属和函数交易权限验证前，不作为自动交易额度。</p>
          </div>
          <button :disabled="!session" @click="loadTrades">重新检查账户</button>
        </div>
        <div class="summary-grid">
          <article class="panel summary-card">
            <span>当前环境</span>
            <strong>{{ modeLabel }}</strong>
          </article>
          <article class="panel summary-card">
            <span>连接状态</span>
            <strong>{{ accountConnectionLabel }}</strong>
          </article>
          <article class="panel summary-card">
            <span>{{ isQmt ? "QMT返回可用资金" : "可用资金" }}</span>
            <strong>{{ tradeDetails?.cash ? `${tradeDetails.cash} 元` : "—" }}</strong>
          </article>
          <article class="panel summary-card">
            <span>{{ isQmt ? "QMT返回总资产" : "总资产" }}</span>
            <strong>
              {{ tradeDetails?.total_equity ? `${tradeDetails.total_equity} 元` : "—" }}
            </strong>
          </article>
        </div>
        <div class="panel">
          <div class="card-top">
            <h2>持仓</h2>
            <span>最近更新 {{ observedAccount() }}</span>
          </div>
          <p v-if="!session" class="notice">请先到“系统设置”登录，再读取账户资金和逐笔持仓。</p>
          <p v-else-if="!tradeDetails?.available">
            暂时没有可核对的账户快照，请确认MiniQMT已经启动并登录。
          </p>
          <p v-else-if="!tradeDetails.positions.length">当前账户没有持仓。</p>
          <div
            v-for="position in tradeDetails?.positions ?? []"
            :key="position.symbol"
            class="trade-row"
          >
            <strong>{{ position.symbol }}</strong>
            <span>
              持有 {{ position.quantity }} 股 · 可卖 {{ position.sellable }} 股 · 冻结
              {{ position.frozen ?? 0 }} 股
            </span>
          </div>
        </div>
      </section>
      <section v-if="view === 'rules'" class="simple-page panel">
        <p class="eyebrow">唯一策略</p>
        <h1 id="trading-settings-heading" tabindex="-1">尾盘策略 · 交易设置</h1>
        <p>量化后台在14:30–14:55提供入选股票，本机订阅行情并按这里的规则买入、撤单重报和卖出。</p>
        <p>依次填写买入金额、止盈止损、最晚卖出时间和风险上限，最后点击“保存交易设置”。</p>
        <p v-if="!session" class="notice warning">
          尚未登录，已保存的交易设置暂未读取。登录后会显示原来的设置。
        </p>
        <form v-if="!session" class="login-form" @submit.prevent="login">
          <label for="settings-password">操作员口令</label>
          <input
            id="settings-password"
            v-model="password"
            type="password"
            maxlength="128"
            autocomplete="current-password"
            :disabled="busy || !status?.operator_access_configured"
          />
          <button
            class="primary"
            :disabled="busy || !password || !status?.operator_access_configured"
          >
            登录并读取已保存设置
          </button>
        </form>
        <div v-else-if="!settingsChecked" class="notice" role="status">
          <p>{{ settingsLoadError || "正在读取已保存的交易设置…" }}</p>
          <button v-if="settingsLoadError" :disabled="settingsLoading" @click="loadTradingSettings">
            重新读取设置
          </button>
        </div>
        <div class="workflow-strip compact">
          <div :class="{ done: !!plan }">
            <b>1</b>
            <span>接收选股</span>
            <small>{{ plan ? `${planSymbolCount} 只` : "等待清单" }}</small>
          </div>
          <div :class="{ done: !!plan && fresh }">
            <b>2</b>
            <span>订阅行情</span>
            <small>{{ subscriptionCount ? `已订阅 ${subscriptionCount} 只` : "连接待检查" }}</small>
          </div>
          <div :class="{ done: orderCount > 0 }">
            <b>3</b>
            <span>买入执行</span>
            <small>{{ orderCount ? `${orderCount} 笔委托` : "等待信号" }}</small>
          </div>
          <div :class="{ done: positionCount > 0 }">
            <b>4</b>
            <span>持仓卖出</span>
            <small>{{ positionCount ? `${positionCount} 只持仓` : "等待持仓" }}</small>
          </div>
        </div>
        <p v-if="settingsChecked && !settingsDraft" class="notice warning" role="alert">
          当前没有交易设置，系统不会自动买入或启用策略。已有持仓不会套用默认卖出规则，请先完成下面的设置或人工处理持仓。
        </p>
        <p v-else-if="restartRequired" class="notice warning" role="alert">
          新交易设置已经保存，但当前引擎仍使用启动时的旧设置。请关闭自动交易系统主窗口并重新打开；重启后系统仍保持暂停，不会自行下单。
        </p>
        <p v-else-if="runningRuleVersion" class="notice good">
          第 {{ runningRuleVersion }} 版正在{{ isTradingProfile ? "实盘" : "模拟" }}运行。
        </p>
        <p v-else-if="automaticTradingPaused && settingsDraft" class="notice">
          自动交易已暂停，已保存的交易设置仍保留。保存或修改设置不会自动恢复交易。
        </p>
        <p v-else-if="session && settingsChecked" class="notice warning">
          保存只生成本机草稿，不会启动自动交易。三个买入金额都填 0 时，自动买入保持关闭。
        </p>
        <form
          v-if="session && settingsChecked"
          class="settings-form"
          novalidate
          @submit.prevent="saveRules"
        >
          <div
            v-if="attemptedSettingsSave && settingsIssues.length"
            id="settings-errors"
            class="notice warning"
            role="alert"
            tabindex="-1"
          >
            <strong>尚未保存，请修改以下设置：</strong>
            <ul>
              <li v-for="issue in settingsIssues" :key="issue.field">{{ issue.message }}</li>
            </ul>
          </div>
          <fieldset class="settings-group">
            <legend>买入设置</legend>
            <p class="settings-note">
              固定采用最新卖一价保护限价单。未成交后先确认撤单，再按新行情下单。 三个金额须满足“单笔
              ≤ 单只股票 ≤ 当日总额”；关闭自动买入时全部填0。
            </p>
            <div class="settings-grid">
              <label>
                当日最多买入（元）
                <input v-model="maxDailyBuy" type="number" min="0" step="0.01" required />
              </label>
              <label>
                单只股票最多买入（元）
                <input v-model="maxSymbolBuy" type="number" min="0" step="0.01" required />
              </label>
              <label>
                单笔委托最多（元）
                <input v-model="maxOrderValue" type="number" min="0" step="0.01" required />
              </label>
              <label>
                未成交等待（秒）
                <input
                  v-model="cancelAfterSeconds"
                  type="number"
                  min="1"
                  max="300"
                  step="1"
                  required
                />
              </label>
              <label>
                最多重新下单次数
                <input v-model="maxRetries" type="number" min="0" max="3" step="1" required />
              </label>
            </div>
          </fieldset>

          <fieldset class="settings-group">
            <legend>卖出设置</legend>
            <p class="settings-note">涨跌幅均按这笔持仓的买入成本价计算。</p>
            <div class="settings-grid">
              <label>
                涨幅达到多少卖出（%）
                <input
                  v-model="takeProfit"
                  type="number"
                  min="0.01"
                  max="100"
                  step="0.01"
                  required
                />
              </label>
              <label>
                跌幅达到多少必须卖出（%）
                <input
                  v-model="stopLoss"
                  type="number"
                  min="0.01"
                  max="99.99"
                  step="0.01"
                  required
                />
              </label>
              <label>
                最晚卖出时间
                <input v-model="sellBy" type="time" min="09:30" max="14:50" step="60" required />
              </label>
            </div>
            <p v-if="sellTimeError" class="notice warning" role="alert">{{ sellTimeError }}</p>
          </fieldset>

          <fieldset class="settings-group">
            <legend>安全设置</legend>
            <p class="settings-note">启用自动买入时，下面两项都必须大于0；最多持有15只股票。</p>
            <div class="settings-grid">
              <label>
                当日最大亏损（元）
                <input v-model="maxDailyLoss" type="number" min="0" step="0.01" required />
              </label>
              <label>
                最多持有股票数量
                <input v-model="maxPositions" type="number" min="0" max="15" step="1" required />
              </label>
            </div>
            <label class="check-line">
              <input v-model="emergencyLiquidationEnabled" type="checkbox" />
              允许使用紧急清仓按钮（执行时仍需再次确认）
            </label>
          </fieldset>

          <details class="advanced-settings">
            <summary>高级设置</summary>
            <p class="settings-note">
              以下是下单保护条件，不需要时可先保留默认值。价格或价差暂不合格时，系统会继续等新行情，直到买入时间窗结束。
            </p>
            <div class="settings-grid">
              <label>
                单笔最大股数
                <input
                  v-model="maxOrderQuantity"
                  type="number"
                  min="100"
                  max="1000000"
                  step="100"
                  required
                />
                <small>每笔委托的股数上限，还会同时受单笔金额上限限制。</small>
              </label>
              <label>
                最大追价（基点）
                <input v-model="maxChaseBps" type="number" min="0" max="1000" step="1" required />
                <small>
                  30基点=0.3%。参考价10元时，最高买入价10.03元；超出后继续等待回落。参考价取接收清单后建立规则时的QMT最新成交价。
                </small>
              </label>
              <label>
                最大买卖价差（基点）
                <input v-model="maxSpreadBps" type="number" min="0" max="1000" step="1" required />
                <small>
                  20基点=0.2%。买一10元、卖一10.01元符合；卖一10.05元时暂不买入，避免付出过大价差。
                </small>
              </label>
              <label>
                行情有效期（毫秒）
                <input
                  v-model="quoteMaxAgeMs"
                  type="number"
                  min="100"
                  max="5000"
                  step="1"
                  required
                />
                <small>
                  1000毫秒=1秒。从行情自身时间计算有效期；旧行情过期后订阅继续，新行情到达后重新检查。
                </small>
              </label>
            </div>
          </details>

          <div class="button-row">
            <button class="primary" :disabled="!session || busy || !!sellTimeError">
              {{ busy ? "正在保存…" : "保存交易设置" }}
            </button>
            <button type="button" @click="view = 'test'">试算卖出规则</button>
            <button
              type="button"
              :disabled="!simulationReady || !!runningRuleVersion || !canPreview || halting"
              @click="prepare('ARM')"
            >
              {{
                runningRuleVersion
                  ? "自动交易已启用"
                  : isUnconfigured
                    ? "连接 QMT 后启用"
                    : status?.broker === "QMT_READ_ONLY"
                      ? "当前仅查看账户，交易未启用"
                      : isTradingProfile
                        ? "启动实盘自动交易…"
                        : "启用模拟…"
              }}
            </button>
          </div>
        </form>
        <p v-if="settingsDraft">
          已保存第 {{ settingsDraft.version }} 版 ·
          {{ new Date(settingsDraft.saved_at).toLocaleString("zh-CN") }}
        </p>
        <p v-if="ruleMessage" role="status">{{ ruleMessage }}</p>
        <p v-if="settingsDraft && !simulationReady" class="hint">
          当前运行会话尚未安装这版设置；保存草稿不会改变正在运行的交易权限。
        </p>
        <p v-if="error" class="notice warning" role="alert">{{ error }}</p>
      </section>
      <section v-if="view === 'test'" class="simple-page panel">
        <button class="back-link" @click="view = 'rules'">← 返回尾盘策略</button>
        <h1>卖出规则试算</h1>
        <p>输入一笔买入成本和当前价格，检查止盈、止损和最晚卖出时间。试算不会发单。</p>
        <form class="rule-form" @submit.prevent="testRules">
          <label>
            这笔持仓的买入成本价（元）
            <input v-model="testCost" type="number" min="0.0001" step="0.0001" required />
          </label>
          <label>
            当前价格（元）
            <input v-model="testPrice" type="number" min="0.0001" step="0.0001" required />
          </label>
          <label>
            行情时间（北京时间）
            <input v-model="testTime" type="time" min="09:30" max="14:55" step="60" required />
          </label>
          <button class="primary" :disabled="!session || busy || !!sellTimeError">运行测试</button>
        </form>
        <div v-if="testResult" class="receipt" role="status">
          <strong>{{ testResult.should_sell ? "应申请卖出" : "尚未触发卖出" }}</strong>
          <p>相对这笔买入成本涨跌 {{ Number(testResult.change_pct).toFixed(2) }}%</p>
          <p>
            触发原因：{{
              testResult.reasons.map((reason) => reasonText[reason] ?? reason).join("、") || "无"
            }}
          </p>
          <p>未发出真实委托。</p>
        </div>
        <p v-if="error" class="notice warning" role="alert">{{ error }}</p>
      </section>
      <section v-if="view === 'trades'" class="simple-page panel">
        <p class="eyebrow">执行过程</p>
        <h1>交易监控</h1>
        <p>按顺序核对选股、行情、信号、委托和成交。产生信号不代表已经成功下单。</p>
        <div class="workflow-strip compact">
          <div :class="{ done: !!plan }">
            <b>1</b>
            <span>选股清单</span>
            <small>{{ plan ? `${planSymbolCount} 只` : "未收到" }}</small>
          </div>
          <div :class="{ done: !!plan && fresh }">
            <b>2</b>
            <span>行情订阅</span>
            <small>{{ plan ? "核验中" : "未开始" }}</small>
          </div>
          <div>
            <b>3</b>
            <span>交易信号</span>
            <small>{{ signalState }}</small>
          </div>
          <div :class="{ done: orderCount > 0 }">
            <b>4</b>
            <span>券商委托</span>
            <small>{{ orderCount }} 笔</small>
          </div>
          <div :class="{ done: tradeCount > 0 }">
            <b>5</b>
            <span>实际成交</span>
            <small>{{ tradeCount }} 笔</small>
          </div>
        </div>
        <section class="signal-detail" aria-labelledby="signal-detail-heading">
          <h2 id="signal-detail-heading">逐只股票信号</h2>
          <p v-if="!plan">尚未收到今天的选股清单，因此没有交易信号。</p>
          <p v-else-if="!entryProgress.length && !entryTargets.length">
            已收到选股清单，正在等待有效行情和本机交易条件核验。
          </p>
          <div v-for="item in entryProgress" :key="item.symbol" class="trade-row signal-row">
            <strong>{{ item.symbol }}</strong>
            <span>{{ executionState(item.state) }}</span>
            <small>{{ targetSummary(item.symbol) }}</small>
          </div>
        </section>
        <div class="button-row">
          <button :disabled="!session" @click="loadTrades">刷新交易数据</button>
          <button
            v-if="isTradingProfile"
            class="danger"
            :disabled="!session || busy || !emergencyLiquidationEnabled"
            @click="previewLiquidation"
          >
            紧急清仓策略持仓
          </button>
        </div>
        <div v-if="liquidationPreview" class="receipt warning" role="alert">
          <strong>即将清仓 {{ liquidationPreview.positions.length }} 笔策略持仓</strong>
          <p>{{ liquidationSymbolSummary }}</p>
          <p>系统会先撤销未完成委托，等待券商确认后再卖出。原账户手工持仓不会处理。</p>
          <label>
            输入“{{ liquidationPreview.confirmation_phrase }}”后执行
            <input v-model="liquidationPhrase" type="text" autocomplete="off" />
          </label>
          <div class="button-row">
            <button
              class="danger"
              :disabled="busy || liquidationPhrase !== liquidationPreview.confirmation_phrase"
              @click="executeLiquidation"
            >
              确认清仓
            </button>
            <button :disabled="busy" @click="liquidationPreview = null">取消</button>
          </div>
        </div>
        <p v-if="tradeError" class="notice warning" role="alert">{{ tradeError }}</p>
        <p v-if="!session">请先在“系统设置”中登录，才能查看逐笔交易。</p>
        <p v-if="brokerAccount">
          持仓 {{ text(brokerAccount.position_count) }} 条 · 委托
          {{ text(brokerAccount.order_count) }} 条 · 成交 {{ text(brokerAccount.trade_count) }} 条 ·
          更新时间 {{ observedAccount() }}
        </p>
        <template v-if="tradeDetails?.available">
          <p>
            查询时间
            {{
              tradeDetails.observed_at
                ? new Date(tradeDetails.observed_at).toLocaleString("zh-CN")
                : "—"
            }}
          </p>
          <p>
            可用资金 {{ tradeDetails.cash ?? "—" }} 元 · 总资产
            {{ tradeDetails.total_equity ?? "—" }} 元
          </p>
          <h2>持仓</h2>
          <p v-if="!tradeDetails.positions.length">当前查询没有持仓。</p>
          <div v-for="position in tradeDetails.positions" :key="position.symbol" class="trade-row">
            <strong>{{ position.symbol }}</strong>
            <span>
              持有 {{ position.quantity }} · 可卖 {{ position.sellable }} · 冻结
              {{ position.frozen ?? "—" }}
            </span>
          </div>
          <h2>委托</h2>
          <p v-if="!tradeDetails.orders.length">当前查询没有委托。</p>
          <div v-for="order in tradeDetails.orders" :key="order.client_order_id" class="trade-row">
            <strong>{{ order.symbol }} {{ order.side === "BUY" ? "买入" : "卖出" }}</strong>
            <span>
              {{ text(order.status) }} · {{ order.filled }}/{{ order.quantity }} 成交 · 剩余
              {{ order.remaining }}
            </span>
            <small>
              {{ order.cancelable ? "可撤单" : order.owned_by_system ? "当前不可撤" : "外部委托" }}
            </small>
            <button
              v-if="order.cancelable && pendingCancel !== order.client_order_id"
              :disabled="busy"
              @click="pendingCancel = order.client_order_id"
            >
              撤单
            </button>
            <span v-if="pendingCancel === order.client_order_id" class="button-row">
              <button class="danger" :disabled="busy" @click="cancelOrder(order.client_order_id)">
                确认撤单
              </button>
              <button :disabled="busy" @click="pendingCancel = ''">取消</button>
            </span>
          </div>
          <h2>成交</h2>
          <p v-if="!tradeDetails.trades.length">当前查询没有成交。</p>
          <div v-for="trade in tradeDetails.trades" :key="trade.trade_id" class="trade-row">
            <strong>{{ trade.symbol }} {{ trade.side === "BUY" ? "买入" : "卖出" }}</strong>
            <span>
              {{ trade.quantity }} 股 · {{ trade.price }} 元 ·
              {{ new Date(trade.traded_at).toLocaleString("zh-CN") }}
            </span>
          </div>
          <p v-if="tradeDetails.truncated" class="notice warning">
            数据较多，每类只展示前 100 条；完整记录请查券商终端。
          </p>
        </template>
        <p v-else-if="session && !tradeError">
          尚无可核对的券商交易快照，请检查账户连接与恢复状态。
        </p>
        <p v-if="ruleMessage" role="status">{{ ruleMessage }}</p>
        <p>紧急清仓只处理本系统记录的策略持仓，执行前会展示清单并要求再次确认。</p>
      </section>
      <section v-if="view === 'messages'" class="simple-page panel">
        <p class="eyebrow">需要关注</p>
        <h1>消息与日志</h1>
        <p>集中查看连接异常、拒单、撤单、成交和策略停止信息。</p>
        <p v-if="status?.components.engine.error" class="notice warning">
          交易引擎：{{ status.components.engine.error }}
        </p>
        <p v-if="marketSession?.failure_code" class="notice warning">
          行情或策略已停止：{{ text(marketSession.failure_code) }}
        </p>
        <p v-if="status?.components.agent.error" class="notice warning">
          同步代理：{{ status.components.agent.error }}
        </p>
        <p v-if="status?.components.supervisor.error" class="notice warning">
          进程监护：{{ status.components.supervisor.error }}
        </p>
        <h2>本次启动活动记录</h2>
        <p v-if="!recentActivity.length">本次启动暂时没有新的策略或委托状态变化。</p>
        <div
          v-for="(item, index) in recentActivity"
          :key="`${item.occurred_at}-${item.category}-${item.symbol ?? ''}-${index}`"
          class="trade-row activity-row"
        >
          <strong>
            {{ item.symbol ? `${item.symbol} · ` : "" }}{{ executionState(item.state) }}
          </strong>
          <span>{{ new Date(item.occurred_at).toLocaleString("zh-CN") }}</span>
          <small v-if="item.detail">{{ item.detail }}</small>
        </div>
        <template v-if="tradeDetails?.available">
          <h2>最近委托</h2>
          <p v-if="!tradeDetails.orders.length">当前没有委托记录。</p>
          <div
            v-for="order in tradeDetails.orders.slice(0, 10)"
            :key="order.client_order_id"
            class="trade-row"
          >
            <strong>{{ order.symbol }} {{ order.side === "BUY" ? "买入" : "卖出" }}</strong>
            <span>{{ text(order.status) }} · 已成交 {{ order.filled }}/{{ order.quantity }}</span>
          </div>
          <h2>最近成交</h2>
          <p v-if="!tradeDetails.trades.length">当前没有成交记录。</p>
          <div
            v-for="trade in tradeDetails.trades.slice(0, 10)"
            :key="trade.trade_id"
            class="trade-row"
          >
            <strong>{{ trade.symbol }} {{ trade.side === "BUY" ? "买入" : "卖出" }}</strong>
            <span>
              {{ trade.quantity }} 股 · {{ trade.price }} 元 ·
              {{ new Date(trade.traded_at).toLocaleString("zh-CN") }}
            </span>
          </div>
        </template>
        <p v-if="noStatusErrors && !tradeDetails?.available">目前没有从本机状态发现新异常。</p>
      </section>
      <section v-show="view === 'settings'" class="intro">
        <div>
          <p class="eyebrow">本机设置</p>
          <h1>系统设置</h1>
          <p>在这里登录、暂停自动交易和检查系统连接。日常使用不需要查看技术信息。</p>
        </div>
        <div class="strategy-note">
          <strong>{{ session ? "操作员已登录" : "需要登录" }}</strong>
          <span>{{ modeLabel }} · {{ fresh ? "系统在线" : "连接待检查" }}</span>
        </div>
      </section>
      <section
        v-if="isUnconfigured && view === 'settings'"
        class="panel qmt-connection-card"
        aria-labelledby="qmt-connection-heading"
      >
        <div>
          <p class="eyebrow">首次接入</p>
          <h2 id="qmt-connection-heading">连接本机 QMT</h2>
          <p>
            系统已经正常打开。完成连接前不会订阅行情或发送委托，你可以先查看页面和配置尾盘策略。
          </p>
        </div>
        <a class="primary setup-link" href="/connect-qmt">开始连接 QMT</a>
      </section>
      <div v-if="disconnected || (status && !fresh)" class="notice warning" role="alert">
        状态未保持最新。以下内容可能是上一次观测，授权操作已锁定。已登录时仍可尝试急停。
      </div>
      <div v-if="!status" class="notice">
        {{ disconnected ? "无法连接本机操作台服务，请检查启动情况。" : "正在读取进程状态…" }}
      </div>
      <details v-show="view === 'settings'" class="advanced-settings system-technical">
        <summary>高级运行信息（一般不用打开）</summary>
        <section class="status-grid" aria-label="进程状态">
          <article v-for="item in components" :key="item.key" class="panel status-card">
            <div class="card-top">
              <h2>{{ item.title }}</h2>
              <span
                class="status-badge"
                :class="{ good: fresh && status?.components[item.key].availability === 'CURRENT' }"
              >
                {{ availability(item.key) }}
              </span>
            </div>
            <p>{{ item.note }}</p>
            <strong class="state-value">
              {{
                text(
                  status?.components[item.key].data?.[
                    item.key === "engine"
                      ? "state"
                      : item.key === "supervisor"
                        ? "action"
                        : "status"
                  ],
                )
              }}
            </strong>
            <dl v-if="item.key === 'engine'">
              <dt>恢复核验</dt>
              <dd>{{ text(engine?.recovery) }}</dd>
              <dt>暂停原因</dt>
              <dd>{{ text(engine?.pause_reason) }}</dd>
              <dt>已安装买入上限</dt>
              <dd>{{ text(installedLimits?.max_buy_value) }} 元</dd>
              <dt>已安装单笔上限</dt>
              <dd>{{ text(installedLimits?.max_order_value) }} 元</dd>
              <dt>已安装持仓只数上限</dt>
              <dd>{{ text(installedLimits?.max_positions) }}</dd>
              <dt>关键恢复存储</dt>
              <dd>
                {{
                  engine?.journal_healthy === true
                    ? "可用"
                    : engine?.journal_healthy === false
                      ? "异常"
                      : "—"
                }}
              </dd>
            </dl>
            <dl v-else-if="item.key === 'supervisor'">
              <dt>累计失败</dt>
              <dd>{{ text(status?.components.supervisor.data?.failures) }}</dd>
              <dt>监护原因</dt>
              <dd>{{ text(status?.components.supervisor.data?.reason) }}</dd>
            </dl>
            <dl v-else>
              <dt>最近任务结果</dt>
              <dd>{{ text(agentResult?.status) }}</dd>
              <dt>量化服务连通性</dt>
              <dd>以具体任务结果为准</dd>
            </dl>
            <p class="observation">
              最近观测 {{ observed(item.key) }}
              <span v-if="status?.components[item.key].error">
                · {{ status.components[item.key].error }}
              </span>
            </p>
          </article>
        </section>
      </details>
      <div v-show="view === 'settings'" class="detail-grid">
        <details class="panel advanced-settings plan-details">
          <summary>查看计划与同步详情</summary>
          <section aria-labelledby="plan-heading">
            <h2 id="plan-heading">当前计划与同步</h2>
            <dl>
              <dt>节点</dt>
              <dd>{{ status?.node_id ?? "—" }}</dd>
              <dt>账户别名</dt>
              <dd>{{ status?.account_id ?? "—" }}</dd>
              <dt>交易日</dt>
              <dd>{{ text(engine?.trade_date) }}</dd>
              <dt>计划编号</dt>
              <dd>{{ plan ? text(plan.plan_id) : "尚未加载计划" }}</dd>
              <dt>标的</dt>
              <dd>{{ plan ? planSymbols || "空计划" : "—" }}</dd>
              <dt>策略标识</dt>
              <dd>
                {{ text(plan?.strategy_id) }}
                <small>（技术标识，非策略定稿）</small>
              </dd>
              <dt>QMT</dt>
              <dd>
                {{
                  status?.broker === "QMT_TRADING"
                    ? "QMT实盘账户已连接；实际报单结果以券商回报为准"
                    : status?.broker === "QMT_READ_ONLY"
                      ? "真实QMT账户，只读调试"
                      : isUnconfigured
                        ? "尚未连接 QMT，交易功能保持锁定"
                        : "Mock 模拟适配器"
                }}
              </dd>
              <template v-if="isQmt">
                <dt>账户快照</dt>
                <dd>
                  {{
                    brokerAccount
                      ? `已读取，持仓 ${brokerAccount.position_count} 条`
                      : "正在核验或读取失败"
                  }}
                </dd>
                <dt>快照时间</dt>
                <dd>{{ observedAccount() }}</dd>
                <dt>委托 / 成交</dt>
                <dd>
                  {{
                    brokerAccount
                      ? `${brokerAccount.order_count} / ${brokerAccount.trade_count}`
                      : "—"
                  }}
                </dd>
                <dt>恢复一致性</dt>
                <dd>
                  {{
                    brokerAccount?.coherent_for_recovery === true ? "已核验" : "待核验，保持暂停"
                  }}
                </dd>
              </template>
            </dl>
            <div class="inset">
              Windows
              定时任务通过同步代理拉取计划、回传每日数据。本页显示进程观测；任务已注册、远端已接收和实盘可用需要分别验收。
            </div>
          </section>
        </details>
        <section class="panel primary-settings" aria-labelledby="control-heading">
          <p class="eyebrow">登录与安全</p>
          <h2 id="control-heading">操作员与总开关</h2>
          <form v-if="!session" class="login-form" @submit.prevent="login">
            <label for="operator-password">操作员口令</label>
            <input
              id="operator-password"
              v-model="password"
              type="password"
              maxlength="128"
              autocomplete="current-password"
              :disabled="busy || !status?.operator_access_configured"
            />
            <button
              class="primary"
              :disabled="busy || !password || !status?.operator_access_configured"
            >
              登录本机操作员
            </button>
            <p>
              {{
                status?.operator_access_configured
                  ? "凭据由引擎验证，登录不授予交易权限。"
                  : "当前配置未启用操作员访问，本页保持只读。"
              }}
            </p>
          </form>
          <template v-else>
            <div class="session-line">
              <span>已登录 · {{ session.actor_id }}</span>
              <button :disabled="busy || halting" @click="logout">退出登录</button>
            </div>
            <button class="danger halt" :disabled="halting || automaticTradingPaused" @click="halt">
              {{ pauseButtonLabel }}
            </button>
            <p v-if="automaticTradingPaused" class="notice good" role="status">
              自动交易已暂停，不会自动恢复。已有持仓仍保留，已提交的委托请到“交易监控”核对。
            </p>
            <p v-else class="hint">立即停止自动发单；已有持仓和已提交的委托请到“交易监控”核对。</p>
            <div class="control-grid">
              <button :disabled="!canStartTrading || halting" @click="prepare('ARM')">
                {{
                  isUnconfigured
                    ? "QMT 未连接"
                    : status?.broker === "QMT_READ_ONLY"
                      ? "只读连接，未启用交易"
                      : isTradingProfile
                        ? "启动实盘自动交易…"
                        : "模拟授权…"
                }}
              </button>
              <button :disabled="!canStartTrading || halting" @click="prepare('RESUME')">
                {{
                  isUnconfigured
                    ? "QMT 未连接"
                    : status?.broker === "QMT_READ_ONLY"
                      ? "只读连接，未启用交易"
                      : isTradingProfile
                        ? "恢复实盘自动交易…"
                        : "模拟恢复授权…"
                }}
              </button>
            </div>
            <details class="advanced-settings">
              <summary>异常恢复（需要处理连接或对账问题时使用）</summary>
              <div class="control-grid">
                <button :disabled="!canPreview || halting" @click="recover">重新核验恢复</button>
                <button :disabled="!canPreview || halting" @click="prepare('CONFIRM_RECOVERY')">
                  确认恢复…
                </button>
              </div>
            </details>
            <div v-if="settingsChecked && !settingsDraft" class="notice warning">
              <p>
                还没有保存交易设置。请先在“尾盘策略 → 交易设置”填写买入金额、卖出条件和风险上限。
              </p>
              <button class="primary" @click="openTradingSettings">前往交易设置</button>
            </div>
            <button v-else @click="openTradingSettings">查看或修改交易设置</button>
          </template>
          <p v-if="error" class="notice warning" role="alert">{{ error }}</p>
          <p v-if="trackingUnavailable" class="notice warning" role="alert">
            浏览器无法保留原请求编号，授权确认已锁定。请检查浏览器存储设置。急停仍可使用。
          </p>
          <div v-if="pendingId" class="notice warning">
            <strong>请求结果待确认</strong>
            <p class="identifier">{{ pendingId }}</p>
            <p>本标签页刷新后继续查询原请求；关闭页面前请保留编号。结果未确认时不要重复授权。</p>
            <button :disabled="!session || busy" @click="lookup">查询原请求结果</button>
          </div>
          <div v-if="receipt" class="receipt" role="status">
            <strong>{{ receiptLabel }}</strong>
            <span v-if="receipt.reason_code">· {{ receipt.reason_code }}</span>
            <p v-if="receipt.request_id" class="identifier">{{ receipt.request_id }}</p>
          </div>
        </section>
      </div>
      <section
        v-if="preview && view === 'settings'"
        class="panel preview-panel"
        aria-labelledby="preview-heading"
      >
        <p class="eyebrow">REVIEW BEFORE CONFIRMING</p>
        <h2 id="preview-heading">核对本次操作</h2>
        <p>
          仅对当前引擎、恢复报告与状态版本有效。确认启动后，系统只会在已签名计划、已配置额度和有效交易时段内自动报单。
        </p>
        <dl>
          <dt>动作 / 账户</dt>
          <dd>{{ preview.request.action }} / {{ preview.account_id }}</dd>
          <dt>引擎启动编号</dt>
          <dd>{{ preview.request.engine_boot_id }}</dd>
          <dt>状态版本 / 权限代际</dt>
          <dd>{{ preview.request.expected_state_version }} / {{ preview.permission_epoch }}</dd>
          <dt>有效期至</dt>
          <dd>{{ preview.request.deadline }}</dd>
          <dt>恢复报告</dt>
          <dd>{{ text(preview.recovery.payload_hash) }}</dd>
          <dt>执行范围</dt>
          <dd>
            {{ text(preview.execution_scope?.kind) }} /
            {{ text(preview.execution_scope?.payload_hash) }}
          </dd>
          <dt>本次授权</dt>
          <dd>
            <template v-if="preview.request.grant">
              {{ text(preview.request.grant.mode) }} · {{ previewDirections }} · 买入总额上限
              {{ text(preview.request.grant.max_buy_value) }} 元 / 单笔上限
              {{ text(preview.request.grant.max_order_value) }} 元 / 持仓
              {{ text(preview.request.grant.max_positions) }} · 有效至
              {{ text(preview.request.grant.valid_until) }}
            </template>
            <template v-else>只确认恢复核验</template>
          </dd>
        </dl>
        <form @submit.prevent="confirm">
          <label for="confirm-phrase">
            请输入：
            <code>{{ preview.confirmation_phrase }}</code>
          </label>
          <input id="confirm-phrase" v-model="phrase" autocomplete="off" spellcheck="false" />
          <div class="button-row">
            <button
              class="primary"
              :disabled="
                !canPreview || halting || expired || phrase !== preview.confirmation_phrase
              "
            >
              {{ expired ? "预览已过期，请重新获取" : "确认本次操作" }}
            </button>
            <button
              type="button"
              @click="
                preview = null;
                phrase = '';
              "
            >
              取消预览
            </button>
          </div>
        </form>
      </section>
      <footer>本机地址 127.0.0.1 · 页面关闭不停止引擎 · 实盘委托由后台交易引擎统一执行</footer>
    </main>
  </div>
</template>
