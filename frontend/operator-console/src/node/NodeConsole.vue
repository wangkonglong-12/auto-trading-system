<script setup lang="ts">
import DebugPickDialog from "./components/DebugPickDialog.vue";
import ManualBuyDialog from "./components/ManualBuyDialog.vue";
import ManualSellDialog from "./components/ManualSellDialog.vue";
import SellStockAction from "./components/SellStockAction.vue";
import { computed, nextTick, reactive, ref, watch } from "vue";
import { NIcon } from "naive-ui";
import {
  GridOutline,
  LayersOutline,
  TimeOutline,
  SettingsOutline,
  OptionsOutline,
  Pause,
  Play,
  ChevronForward,
  DocumentTextOutline,
  StatsChartOutline,
  RefreshOutline,
} from "@vicons/ionicons5";
import ConsoleSettings from "./components/ConsoleSettings.vue";
import BoardSellConfiguration from "./components/BoardSellConfiguration.vue";
import SystemSettings from "./components/SystemSettings.vue";
import ExecutionTestPanel from "./components/ExecutionTestPanel.vue";
import HistoryPanel from "./components/HistoryPanel.vue";
import DailyAutoPanel from "./components/DailyAutoPanel.vue";
import TradingProcessPanel from "./components/TradingProcessPanel.vue";
import { useTradingConsole } from "./useTradingConsole";
import { money, stateText, timeText } from "./consoleTypes";
import { errorText, NodeApiError } from "./api";
import type { AccountOrder, BuyRow, ConsoleView, SellPosition } from "./consoleTypes";
const c = reactive(useTradingConsole());
const expanded = ref<string | null>(null);
const testMode = ref(false);
const confirmationDialog = ref<HTMLElement | null>(null);
let previousFocus: HTMLElement | null = null;
watch(
  () => c.preview,
  async (value) => {
    if (value) {
      previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      await nextTick();
      confirmationDialog.value?.querySelector<HTMLInputElement>("input")?.focus();
    } else previousFocus?.focus();
  },
);
function dialogKey(event: KeyboardEvent) {
  if (event.key === "Escape") {
    event.preventDefault();
    c.preview = null;
  } else if (event.key === "Tab") {
    const items = confirmationDialog.value?.querySelectorAll<HTMLElement>(
      "input, button:not(:disabled)",
    );
    if (!items?.length) return;
    const first = items[0]!;
    const last = items[items.length - 1]!;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }
}
const navigation = [
  { key: "today" as ConsoleView, title: "今日交易", icon: GridOutline },
  { key: "positions" as ConsoleView, title: "策略持仓", icon: LayersOutline },
  { key: "history" as ConsoleView, title: "历史记录", icon: TimeOutline },
  { key: "trading-settings" as ConsoleView, title: "交易设置", icon: OptionsOutline },
  { key: "system-settings" as ConsoleView, title: "系统设置", icon: SettingsOutline },
];
const title = computed(() => navigation.find((item) => item.key === c.view)?.title ?? "今日交易");
const date = computed(() =>
  new Date(c.now).toLocaleDateString("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "long",
    day: "numeric",
    weekday: "long",
  }),
);
const recoveryFailed = computed(
  () => !!c.engine?.recovery_error || c.engine?.pause_reason === "RECOVERY_FAILED",
);
const recoveryFailureText = computed(() =>
  errorText(new NodeApiError(String(c.engine?.recovery_error ?? "RECOVERY_FAILED"))),
);
const connection = computed(() =>
  !c.status || c.disconnected
    ? "QMT状态待核对"
    : !c.realAccount || c.unconfigured
      ? "QMT未连接"
      : recoveryFailed.value
        ? "QMT账户核验失败"
        : !c.accountFresh
          ? "QMT数据待读取"
          : "QMT已连接",
);
const headline = computed(() =>
  !c.status
    ? "正在读取系统状态"
    : c.unconfigured || !c.realAccount
      ? "等待连接QMT"
      : !c.fresh
        ? "运行状态待核对"
        : c.running
          ? "自动买入运行中"
          : c.dailyAuto?.enabled
            ? "持续自动买入已开启"
            : c.status.broker === "QMT_READ_ONLY"
              ? "真实账户只读查看"
              : "自动买入已暂停",
);
const visibleBuys = computed(() =>
  (c.displayBuys?.rows ?? []).filter(
    (row) => !row.expires_at || c.now < Date.parse(row.expires_at),
  ),
);
const manualBuyOpen = ref(false);
const debugPickOpen = ref(false);
const settingsPage = ref<"buy" | "sell">("buy");
const selectedSale = ref<SellPosition | null>(null);
const salePosition = (symbol: string) =>
  c.account?.sell_positions?.find((p) => p.symbol === symbol);
const saleReady = computed(
  () =>
    !!c.session &&
    c.fresh &&
    c.accountFresh &&
    !c.halting &&
    !c.busy &&
    !c.settings?.restart_required,
);
const normalPause = computed(() =>
  ["DAILY_AUTO_OPERATOR_DISABLED", "DAILY_AUTO_SETTINGS_CHANGED"].includes(
    String(c.engine?.pause_reason),
  ),
);
const automaticWaiting = computed(
  () => c.fresh && normalPause.value && c.dailyAuto?.enabled && c.engine?.recovery === "CLEAR",
);
const saleBlockedReason = computed(() =>
  c.engine?.state === "HALTED"
    ? automaticWaiting.value
      ? "持续自动买入已开启，正在等待正式名单或自动核验账户。"
      : `${normalPause.value ? "" : "系统安全暂停："}${errorText(new NodeApiError(String(c.engine?.pause_reason)))}`
    : "",
);
function openSale(position: SellPosition) {
  if (saleReady.value && position.can_sell) selectedSale.value = position;
}
async function saleSubmitted(status: string) {
  selectedSale.value = null;
  c.message =
    status === "UNKNOWN"
      ? "卖出结果待核对，请勿重复提交。"
      : status === "PARTIAL_SUBMITTED"
        ? "部分卖出子委托已提交，其余未提交，请查看列表。"
        : "卖出申请已接收，请在列表核对委托和实际成交。";
  await c.refreshAccount();
}
watch(
  () => c.session?.csrf_token,
  () => (selectedSale.value = null),
);
const manualDisabledReason = computed(() => {
  if (!c.session) return "登录后可手动买入";
  if (!c.fresh) return "运行状态待核对，请先恢复状态连接";
  if (c.status?.broker !== "QMT_TRADING") return "当前连接不支持下单，需要QMT交易连接";
  if (recoveryFailed.value) return "账户核验失败，请先重新核验账户";
  if (c.busy) return "正在处理上一项操作";
  if (c.storageFailed) return "浏览器无法保存操作编号，买入暂不可用";
  if (c.pendingId) return "请先查询上一项操作的结果";
  if (!c.settingsLoaded) return "正在读取交易设置";
  if (c.settings?.restart_required) return "交易设置尚未加载，请重新打开主程序";
  if (!c.settings?.buy_configured) return "请先配置买入限额并重新打开主程序";
  if (
    c.settings?.draft?.settings.sell != null ||
    (c.engine?.legacy_exit_context_loaded ?? c.engine?.exit_context_loaded)
  )
    return "旧卖出规则仍在运行，请先按交易设置提示处理";
  if (c.engine?.recovery !== "CLEAR") return "账户核验尚未完成";
  if (!["REVIEW_REQUIRED", "READY_PAUSED", "ARMED", "EXECUTING"].includes(String(c.engine?.state)))
    return saleBlockedReason.value || "系统处于安全暂停，请先重新核验账户";
  return "";
});
const manualReady = computed(() => !manualDisabledReason.value);
const rowKey = (row: BuyRow) => `${row.trade_date ?? ""}:${row.symbol}`;
const rowSnapshot = (row: BuyRow) => row.is_snapshot ?? c.displaySnapshot;
const rowAvailable = (row: BuyRow) =>
  row.state !== "UNAVAILABLE" && (rowSnapshot(row) || (c.fresh && c.accountFresh));
function rowSaleState(row: BuyRow): "SOLD" | "PARTIAL_SOLD" | null {
  if (
    !c.fresh ||
    !c.accountFresh ||
    c.account?.coherent !== true ||
    c.account.truncated ||
    !rowAvailable(row) ||
    (row.quantity ?? 0) <= 0
  )
    return null;
  const position = salePosition(row.symbol);
  if (!position || position.sold_today <= 0) return null;
  if (position.state === "SOLD" && position.remaining === 0) return "SOLD";
  if (position.remaining > 0) return "PARTIAL_SOLD";
  return null;
}
function rowStateText(row: BuyRow): string {
  if (!rowAvailable(row)) return "待核对";
  const sale = rowSaleState(row);
  return sale === "SOLD"
    ? "已全部卖出"
    : sale === "PARTIAL_SOLD"
      ? "当前持仓部分卖出"
      : stateText(row.state);
}
async function manualSubmitted(status: string) {
  manualBuyOpen.value = false;
  c.message = ["PERSISTING", "DISPATCHING", "SDK_ACCEPTED", "RECOVERED"].includes(status)
    ? "手动委托已提交处理，请在列表核对委托和实际成交结果。"
    : "本次委托结果待核对，已保留记录，请勿重复提交。";
  await c.refreshAccount();
}
const count = (value: number | null | undefined) => (value == null ? "—" : String(value));
const name = (symbol: string) => c.account?.names?.[symbol] ?? "名称待获取";
const allPositions = computed(() => c.account?.positions ?? []);
const managedSymbols = computed(
  () =>
    new Set(
      (c.account?.managed_lots ?? []).filter((lot) => lot.quantity > 0).map((lot) => lot.symbol),
    ),
);
const selectedOrders = (row: BuyRow) =>
  rowSnapshot(row) || (row.trade_date != null && row.trade_date !== c.engine?.trade_date)
    ? []
    : (c.account?.orders ?? []).filter(
        (order) => order.symbol === row.symbol && order.side === "BUY" && order.owned_by_system,
      );
const cancelPending = (order: AccountOrder) =>
  order.cancel_requested ||
  ["CANCEL_PENDING", "PARTIAL_FILL_CANCEL_PENDING"].includes(order.status);
const cancelFailureText = (order: AccountOrder) =>
  order.cancel_reason === "CANCEL_NOT_DISPATCHED"
    ? "本次撤单未发出，可重新申请"
    : `券商拒绝本次撤单${order.cancel_reason ? `（${order.cancel_reason}）` : ""}`;
const cancelOrders = (row: BuyRow) =>
  selectedOrders(row).filter(
    (order) =>
      order.remaining > 0 &&
      (order.cancelable || cancelPending(order) || order.cancel_state === "FAILED"),
  );
const cancelDisabled = computed(
  () => !c.canControl || !c.accountFresh || c.loadingAccount || c.account?.truncated === true,
);
const receivedPhases = computed(() => c.displayBuys?.decision_phases ?? []);
const confirmDisabled = computed(
  () =>
    c.busy ||
    !c.preview ||
    c.phrase !== c.preview.confirmation_phrase ||
    Date.parse(c.preview.request.deadline) <= c.now,
);
</script>
<template>
  <div class="trading-app">
    <aside class="sidebar">
      <a class="brand" href="#today" @click.prevent="c.view = 'today'">
        <span class="brand-icon"><NIcon :component="StatsChartOutline" /></span>
        <span>自动交易系统</span>
      </a>
      <nav aria-label="主导航">
        <button
          v-for="item in navigation"
          :key="item.key"
          :class="['nav-item', { selected: c.view === item.key }]"
          :aria-current="c.view === item.key ? 'page' : undefined"
          @click="c.view = item.key"
        >
          <NIcon :component="item.icon" />
          <span>{{ item.title }}</span>
        </button>
      </nav>
      <p class="sidebar-note">
        尾盘自动买入
        <br />
        14:40 · 14:45 · 14:50
      </p>
    </aside>
    <div class="app-content">
      <header class="topbar">
        <div class="title-line">
          <h1>{{ title }}</h1>
          <span class="date-label">{{ date }}</span>
        </div>
        <div class="connection">
          <span :class="['status-dot', { good: c.accountFresh && !recoveryFailed }]" />
          {{ connection }}
          <span class="updated">
            更新 {{ c.accountFresh ? timeText(c.account?.observed_at) : "—" }}
          </span>
        </div>
      </header>
      <main>
        <div
          v-if="
            c.session && (recoveryFailed || (c.engine?.state === 'HALTED' && !automaticWaiting))
          "
          :class="['notice', { warning: recoveryFailed || !normalPause }]"
          :role="recoveryFailed || !normalPause ? 'alert' : 'status'"
        >
          {{ recoveryFailed ? recoveryFailureText : saleBlockedReason }}
          <button class="text-button" :disabled="c.busy || !c.fresh" @click="c.control('RECOVER')">
            重新核验账户
          </button>
          <span>仅核对账户、持仓和委托，不开启自动买入。</span>
        </div>
        <div v-if="c.error || c.accountError" class="notice warning" role="alert">
          {{ c.error || c.accountError }}
          <button class="text-button" :disabled="c.loadingAccount" @click="c.refreshAccount">
            重新读取
          </button>
        </div>
        <div v-if="c.streamFailed || c.disconnected" class="notice warning" role="alert">
          页面状态连接已中断，交易状态待核对。
          <button class="text-button" @click="c.retry">重新连接状态</button>
        </div>
        <div v-if="c.message" class="notice" role="status">{{ c.message }}</div>
        <div v-if="c.pendingId" class="notice warning">
          操作结果待确认，请查询原请求。
          <button class="text-button" :disabled="c.busy || !c.session" @click="c.lookup">
            查询操作结果
          </button>
        </div>
        <div v-if="c.storageFailed" class="notice warning">
          浏览器无法保存操作请求编号，启动操作已锁定。停止自动发单仍可使用。
        </div>
        <section v-if="!c.session" class="login-strip" aria-labelledby="login-title">
          <div>
            <h2 id="login-title">登录本机操作员</h2>
            <p>登录后按已保存配置分别核验并启动主板、创业板自动卖出；全局停止仍有效。</p>
          </div>
          <form @submit.prevent="c.login">
            <label class="sr-only" for="operator-password">操作员口令</label>
            <input
              id="operator-password"
              v-model="c.password"
              type="password"
              autocomplete="current-password"
              placeholder="操作员口令"
              required
              maxlength="128"
            />
            <button
              class="button primary"
              :disabled="c.busy || !c.status?.operator_access_configured"
            >
              登录
            </button>
          </form>
        </section>
        <div v-if="c.view === 'today'" class="actions today-mode" aria-label="交易数据范围">
          <button class="button" :aria-pressed="!testMode" @click="testMode = false">
            正式交易
          </button>
          <button class="button" :aria-pressed="testMode" @click="testMode = true">测试交易</button>
        </div>
        <ExecutionTestPanel
          v-if="c.view === 'today' && testMode"
          :session="c.session"
          @expired="c.logout"
        />
        <template v-if="c.view === 'today' && !testMode">
          <section class="overview" aria-label="自动买入状态">
            <div class="hero-row">
              <h2>
                <span :class="['status-dot large', { good: c.running }]" />
                {{ headline }}
              </h2>
              <div class="actions">
                <button
                  v-if="c.running || c.dailyAuto?.enabled"
                  class="button primary"
                  :disabled="!c.session"
                  @click="c.dailyAuto?.enabled ? c.disableDailyAuto() : c.control('HALT')"
                >
                  <NIcon :component="Pause" />
                  {{
                    c.dailyAuto?.enabled
                      ? "关闭持续自动买入"
                      : c.engine?.exit_context_loaded
                        ? "停止自动发单"
                        : "暂停自动买入"
                  }}
                </button>
                <button
                  v-else-if="c.dailyAuto?.supported"
                  class="button primary"
                  :disabled="!c.canControl || c.settings?.restart_required"
                  @click="c.previewDailyAuto()"
                >
                  <NIcon :component="Play" />
                  开启持续自动买入
                </button>
                <button
                  v-else
                  class="button primary"
                  :disabled="!c.canControl || !!c.startReason"
                  :title="c.startReason || '核对额度后启动自动买入'"
                  @click="c.prepare('ARM')"
                >
                  <NIcon :component="Play" />
                  {{ c.startReason === "等待后台正式选股结果" ? "等待正式名单" : "启动自动买入" }}
                </button>
                <button class="button" @click="c.view = 'trading-settings'">
                  <NIcon :component="OptionsOutline" />
                  交易设置
                </button>
              </div>
            </div>
            <p v-if="c.dailyAuto?.supported" class="muted start-reason">
              {{
                c.dailyAuto.enabled
                  ? "持续模式已开启，系统每天自动核验并等待正式名单。"
                  : "只需确认开启一次，程序运行期间每天按保存的限额自动买入。"
              }}
            </p>
            <p v-else-if="!c.running" class="muted start-reason">{{ c.startReason }}</p>
            <div class="summary">
              <div>
                <span>已完成买入</span>
                <strong>
                  {{ count(c.buys?.completed) }}
                  <small>只</small>
                </strong>
              </div>
              <div>
                <span>选股未买入</span>
                <strong>
                  {{ count(c.buys?.not_bought) }}
                  <small>只</small>
                </strong>
              </div>
              <div>
                <span>剩余名额</span>
                <strong>
                  {{ count(c.buys?.remaining) }}
                  <small>只</small>
                </strong>
              </div>
              <div class="amount-cap">
                <span>单只金额上限</span>
                <strong>
                  {{
                    c.settings?.draft
                      ? `¥ ${money(c.settings.draft.settings.buy.max_symbol_buy_value)}`
                      : "—"
                  }}
                </strong>
              </div>
            </div>
          </section>
          <section class="buy-progress" aria-label="买入进度">
            <h2>买入进度</h2>
            <p>
              已完成买入
              <strong>{{ count(c.buys?.completed) }}</strong>
              只
              <span class="divider">·</span>
              选股未买入
              <strong>{{ count(c.buys?.not_bought) }}</strong>
              只
              <span class="divider">·</span>
              部分成交
              <strong>{{ count(c.buys?.partial) }}</strong>
              只
              <span class="divider">·</span>
              委托中
              <strong>{{ count(c.buys?.pending) }}</strong>
              只
              <span class="divider">·</span>
              共用 {{ c.buys?.maximum ?? "—" }} 个名额
            </p>
            <small>
              部分成交与委托中均占名额，同一股票当天不重复买入。选股未买入包含已跳过候选，列表仅列委托或成交记录。
            </small>
          </section>
          <section class="phase-strip" aria-label="三批选股结果">
            <div v-for="phase in ['1440', '1445', '1450']" :key="phase">
              <NIcon :component="TimeOutline" />
              <strong>{{ phase.slice(0, 2) }}:{{ phase.slice(2) }}</strong>
              <span>
                {{
                  !c.fresh && !c.displaySnapshot
                    ? "状态待核对"
                    : receivedPhases.includes(phase)
                      ? "已收到结果"
                      : "等待结果"
                }}
              </span>
            </div>
          </section>
          <section class="stock-section" aria-labelledby="stocks-heading">
            <div class="section-heading">
              <h2 id="stocks-heading">今日股票</h2>
              <button class="button" :disabled="!c.session" @click="debugPickOpen = true">
                调试选股买入
              </button>
              <button
                class="button primary"
                :disabled="!manualReady"
                :title="manualDisabledReason || '填写代码、限价和数量'"
                @click="manualBuyOpen = true"
              >
                手动买入
              </button>
              <button
                class="text-button"
                :disabled="!c.session || c.loadingAccount"
                @click="c.refreshAccount"
              >
                <NIcon :component="RefreshOutline" />
                {{ c.loadingAccount ? "读取中…" : "刷新" }}
              </button>
            </div>
            <p v-if="c.session && !manualReady" class="muted">
              手动买入不可用：{{ manualDisabledReason }}。
            </p>
            <p class="muted">
              展示周期：选股日 14:30 至下一交易日 14:30，到期清空并等待新选股结果。
              手动买入当天即时显示，保留至下一交易日14:30；顶部统计仅计当天。
              <template v-if="c.displayBuys?.selection_date">
                当前选股日期：{{ c.displayBuys.selection_date }}。
              </template>
              <template v-if="c.displaySnapshot">
                上次保存的交易快照，更新于 {{ c.displayBuys?.observed_at }}。
              </template>
              <template v-if="c.displayBuys?.snapshot_saved === false">
                展示快照保存失败，重启后可能无法保留。
              </template>
              <template v-if="c.displayBuys?.calendar_pending">
                正在自动获取交易日历；当天结果可查看，跨日保留时间待确认。
              </template>
            </p>
            <div class="table-scroll">
              <table class="stock-table">
                <thead>
                  <tr>
                    <th>股票</th>
                    <th>日期 / 来源</th>
                    <th>状态</th>
                    <th>买入时间</th>
                    <th class="numeric">买入均价</th>
                    <th class="numeric">已成交金额</th>
                    <th>自动卖出</th>
                    <th>操作</th>
                    <th class="row-action"><span class="sr-only">详情</span></th>
                  </tr>
                </thead>
                <tbody>
                  <template v-for="row in visibleBuys" :key="rowKey(row)">
                    <tr :class="{ expanded: expanded === rowKey(row) }">
                      <td>
                        <strong>{{ row.symbol }}</strong>
                        <span class="stock-name">{{ row.name ?? name(row.symbol) }}</span>
                      </td>
                      <td>
                        {{ row.trade_date ?? c.displayBuys?.selection_date ?? "—" }}
                        <span class="stock-name">
                          {{
                            row.source === "MANUAL"
                              ? "手动买入"
                              : row.source === "DEBUG"
                                ? "调试选股 · 真实交易"
                                : "自动买入"
                          }}{{ rowSnapshot(row) ? " · 已保存快照" : "" }}
                        </span>
                      </td>
                      <td>
                        <span
                          :class="[
                            'execution-state',
                            rowSaleState(row) === 'PARTIAL_SOLD'
                              ? 'partial'
                              : row.state.toLowerCase(),
                          ]"
                        >
                          <span class="status-dot" />
                          {{ rowStateText(row) }}
                        </span>
                        <span v-if="rowSaleState(row)" class="stock-name">
                          买入结果：{{ stateText(row.state) }}
                        </span>
                      </td>
                      <td>{{ rowAvailable(row) ? timeText(row.bought_at) : "—" }}</td>
                      <td class="numeric">
                        {{ rowAvailable(row) ? (row.average_price ?? "—") : "—" }}
                      </td>
                      <td class="numeric">{{ rowAvailable(row) ? money(row.amount) : "—" }}</td>
                      <td>{{ row.sell_state ? stateText(row.sell_state) : "—" }}</td>
                      <td>
                        <div v-for="order in cancelOrders(row)" :key="order.client_order_id">
                          <span v-if="cancelPending(order)" class="muted">
                            {{ order.cancel_state === "UNKNOWN" ? "撤单结果待核对" : "撤单处理中" }}
                          </span>
                          <button
                            v-else
                            class="button"
                            :aria-label="`撤销${row.symbol}委托，未成交${order.remaining}股，委托价${order.limit_price}元`"
                            :disabled="cancelDisabled || !order.cancelable"
                            @click="c.cancelOrder(order.client_order_id)"
                          >
                            {{ order.cancel_state === "FAILED" ? "重试撤单" : "撤单" }}
                          </button>
                          <span v-if="order.cancel_state === 'FAILED'" class="stock-name">
                            {{ cancelFailureText(order) }}
                          </span>
                          <span class="stock-name">
                            未成交 {{ order.remaining }} 股 · {{ order.limit_price }} 元
                          </span>
                        </div>
                        <span v-if="!cancelOrders(row).length" class="muted">—</span>
                        <SellStockAction
                          :symbol="row.symbol"
                          :position="salePosition(row.symbol)"
                          :supported="c.account?.sell_positions !== undefined"
                          :orders="c.account?.orders ?? []"
                          :ready="saleReady"
                          :blocked-reason="saleBlockedReason"
                          :cancel-disabled="cancelDisabled"
                          @sell="openSale"
                          @cancel="c.cancelOrder"
                        />
                      </td>
                      <td>
                        <button
                          class="icon-button"
                          :aria-label="`查看${row.symbol}详情`"
                          :aria-expanded="expanded === rowKey(row)"
                          @click="expanded = expanded === rowKey(row) ? null : rowKey(row)"
                        >
                          <NIcon :component="ChevronForward" />
                        </button>
                      </td>
                    </tr>
                    <tr v-if="expanded === rowKey(row)" class="detail-row">
                      <td colspan="9">
                        <p>
                          当前进度：{{
                            row.source === "MANUAL" ? "手动委托" : stateText(row.signal_state)
                          }}
                          · 实际成交 {{ rowAvailable(row) ? (row.quantity ?? "—") : "—" }} 股
                        </p>
                        <p v-for="order in selectedOrders(row)" :key="order.client_order_id">
                          {{ stateText(order.status) }} · 已成交 {{ order.filled }}/{{
                            order.quantity
                          }}
                          股 · 委托价 {{ order.limit_price }} 元
                          <span v-if="order.reason">· {{ order.reason }}</span>
                          <span v-if="order.cancel_state === 'FAILED'">
                            · {{ cancelFailureText(order) }}
                          </span>
                          <span v-else-if="order.cancel_state === 'UNKNOWN'">· 撤单结果待核对</span>
                          <span v-else-if="order.cancel_requested">· 撤单处理中</span>
                        </p>
                        <p v-if="rowSnapshot(row)">
                          已保存快照，更新于
                          {{ row.observed_at ?? c.displayBuys?.observed_at }}；当前委托请刷新核对。
                        </p>
                        <p v-else-if="!selectedOrders(row).length">尚无本系统委托记录。</p>
                      </td>
                    </tr>
                  </template>
                  <tr v-if="!visibleBuys.length">
                    <td colspan="9" class="empty-cell">
                      <NIcon :component="DocumentTextOutline" />
                      <strong>
                        {{
                          !c.session
                            ? "登录后读取真实账户"
                            : !c.realAccount
                              ? "连接QMT后查看真实交易"
                              : c.accountError
                                ? "账户读取失败，请重试"
                                : c.loadingAccount
                                  ? "正在读取真实QMT数据"
                                  : "暂无本周期交易记录"
                        }}
                      </strong>
                      <span>
                        仅显示本系统已提交委托或已有成交的股票；候选及跳过原因见交易过程。
                      </span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>
        </template>
        <TradingProcessPanel
          v-if="c.view === 'today'"
          :process="c.dashboard?.process"
          :logged-in="!!c.session"
          :state-label="headline"
        />
        <template v-if="c.view === 'positions'">
          <section class="section-heading">
            <div>
              <h2>QMT真实持仓</h2>
              <p class="muted">券商账户全部持仓均可查看；卖出规则未配置。</p>
            </div>
            <button
              class="button"
              :disabled="!c.session || c.loadingAccount"
              @click="c.refreshAccount"
            >
              刷新持仓
            </button>
          </section>
          <div class="account-summary">
            <div>
              <span>QMT总资产</span>
              <strong>{{ c.accountFresh ? money(c.account?.total_equity) : "—" }}</strong>
            </div>
            <div>
              <span>可用资金</span>
              <strong>{{ c.accountFresh ? money(c.account?.cash) : "—" }}</strong>
            </div>
            <div>
              <span>证券市值</span>
              <strong>{{ c.accountFresh ? money(c.account?.market_value) : "—" }}</strong>
            </div>
          </div>
          <div class="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>股票</th>
                  <th>管理范围</th>
                  <th class="numeric">持有数量</th>
                  <th class="numeric">可卖数量</th>
                  <th class="numeric">市值</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="position in allPositions" :key="position.symbol">
                  <td>
                    <strong>{{ position.symbol }}</strong>
                    <span class="stock-name">{{ name(position.symbol) }}</span>
                  </td>
                  <td>
                    {{
                      managedSymbols.has(position.symbol)
                        ? "含本系统托管持仓"
                        : "账户持仓，归属待核对"
                    }}
                  </td>
                  <td class="numeric">{{ c.accountFresh ? position.quantity : "—" }}</td>
                  <td class="numeric">{{ c.accountFresh ? position.sellable : "—" }}</td>
                  <td class="numeric">{{ c.accountFresh ? money(position.market_value) : "—" }}</td>
                  <td>
                    <SellStockAction
                      :symbol="position.symbol"
                      :position="salePosition(position.symbol)"
                      :supported="c.account?.sell_positions !== undefined"
                      :orders="c.account?.orders ?? []"
                      :ready="saleReady"
                      :blocked-reason="saleBlockedReason"
                      :cancel-disabled="cancelDisabled"
                      @sell="openSale"
                      @cancel="c.cancelOrder"
                    />
                  </td>
                </tr>
                <tr v-if="!allPositions.length">
                  <td colspan="6" class="empty-cell">
                    <strong>{{ c.accountFresh ? "QMT返回当前无持仓" : "持仓数据待读取" }}</strong>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <section v-if="c.account?.managed_lots?.length" class="stock-section">
            <h2>系统托管批次</h2>
            <p class="muted">仅管理本系统成交的数量；卖出规则待配置，当前不自动卖出。</p>
            <div class="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>股票</th>
                    <th>买入日期</th>
                    <th class="numeric">剩余股数</th>
                    <th class="numeric">买入成本</th>
                    <th>最早可卖日期</th>
                    <th>自动卖出</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="lot in c.account.managed_lots" :key="lot.lot_id">
                    <td>{{ lot.symbol }}</td>
                    <td>{{ lot.acquired_on }}</td>
                    <td class="numeric">{{ c.accountFresh ? lot.quantity : "—" }}</td>
                    <td class="numeric">{{ c.accountFresh ? (lot.cost_price ?? "—") : "—" }}</td>
                    <td>{{ lot.eligible_sell_date }}</td>
                    <td>待配置卖出规则</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section>
          <section
            v-if="c.account?.sell_positions?.some((p) => p.sold_today > 0 || p.reserved > 0)"
            class="stock-section"
          >
            <h2>本系统卖出记录</h2>
            <div
              v-for="position in c.account.sell_positions.filter(
                (p) => p.sold_today > 0 || p.reserved > 0,
              )"
              :key="position.symbol"
            >
              <strong>{{ position.symbol }} · {{ position.name ?? name(position.symbol) }}</strong>
              <p>
                今日已卖 {{ position.sold_today }} 股 · 成交均价
                {{ position.average_sell_price ?? "—" }} 元 · 成交金额
                {{ money(position.sell_amount) }} 元
              </p>
              <SellStockAction
                :symbol="position.symbol"
                :position="position"
                :orders="c.account?.orders ?? []"
                :ready="saleReady"
                :blocked-reason="saleBlockedReason"
                :cancel-disabled="cancelDisabled"
                @sell="openSale"
                @cancel="c.cancelOrder"
              />
            </div>
          </section>
        </template>
        <HistoryPanel v-if="c.view === 'history'" :session="c.session" @expired="c.logout" />
        <template v-if="c.view === 'trading-settings'">
          <div v-if="c.session" class="actions" aria-label="交易配置类别">
            <button
              class="button"
              :aria-pressed="settingsPage === 'buy'"
              @click="settingsPage = 'buy'"
            >
              买入配置
            </button>
            <button
              class="button"
              :aria-pressed="settingsPage === 'sell'"
              @click="settingsPage = 'sell'"
            >
              卖出配置
            </button>
          </div>
          <BoardSellConfiguration
            v-if="c.session && settingsPage === 'sell'"
            :session="c.session"
            :blocked="c.busy || c.halting"
            @expired="c.logout"
          />
          <ConsoleSettings
            v-if="c.session && settingsPage === 'buy'"
            :value="c.settings"
            :busy="c.busy"
            :connected="c.realAccount && !c.unconfigured && c.settingsLoaded"
            @save="c.saveSettings"
          />
          <DailyAutoPanel
            v-if="c.session && settingsPage === 'buy' && c.status?.broker === 'QMT_TRADING'"
            :state="c.dailyAuto"
            :preview="c.dailyAutoPreview"
            :busy="c.busy || c.halting"
            :ready="c.canControl && !c.settings?.restart_required"
            :can-stop="!!c.session && !c.halting"
            :now="c.now"
            @preview="c.previewDailyAuto"
            @confirm="c.confirmDailyAuto"
            @disable="c.disableDailyAuto"
            @cancel="c.dailyAutoPreview = null"
          />
          <section
            v-if="
              c.session &&
                settingsPage === 'buy' &&
                c.status?.broker === 'QMT_TRADING' &&
                !c.dailyAuto?.supported
            "
            class="settings-section"
          >
            <h2>账户核验与运行控制</h2>
            <p class="muted">核验通过后，启动操作仍需要确认。重新启动程序不会自动恢复下单。</p>
            <p v-if="c.engine?.state === 'RECOVERING'" class="notice" role="status">
              正在核验账户，请等待结果。
            </p>
            <p v-else-if="c.engine?.recovery === 'CLEAR'" class="notice" role="status">
              账户核验已通过。
              {{
                c.engine?.state === "REVIEW_REQUIRED"
                  ? "请确认核验结果。"
                  : c.engine?.halted === false
                    ? "自动交易已启动。"
                    : "自动交易仍保持暂停，启动需另行确认。"
              }}
            </p>
            <p
              v-else-if="c.engine?.state === 'RECOVERY_BLOCKED'"
              class="notice warning"
              role="status"
            >
              账户核验未通过，自动买入已阻断。重新核验不会跳过未核清的系统委托。
            </p>
            <div class="actions">
              <button class="button" :disabled="c.busy || !c.fresh" @click="c.control('RECOVER')">
                核验账户
              </button>
              <button
                class="button"
                :disabled="!c.canControl || c.engine?.recovery !== 'CLEAR'"
                @click="c.prepare('CONFIRM_RECOVERY')"
              >
                确认核验结果
              </button>
              <button class="button danger" @click="c.control('HALT')">停止全部自动发单</button>
            </div>
          </section>
        </template>
        <SystemSettings
          v-if="c.view === 'system-settings'"
          :connection="connection"
          :broker="c.status?.broker"
          :session="c.session"
          :busy="c.busy"
          :fresh="c.fresh"
          :running="c.running"
          :halted="c.engine?.halted === true"
          :settings="c.systemSettings"
          @save="c.saveSystemSettings"
          @save-access-token="c.saveAccessToken"
          @change-password="c.changePassword"
          @logout="c.logout"
        />
        <footer class="app-footer">
          <span v-if="c.engine?.legacy_exit_context_loaded ?? c.engine?.exit_context_loaded">
            旧版卖出规则仍在运行，请先停止并保存新版设置。
          </span>
          <span v-else-if="c.view === 'trading-settings' && settingsPage === 'sell'">
            保存配置后，可分别预览开启主板、创业板卖出引擎。
          </span>
          <span v-else-if="c.engine?.exit_context_loaded">主板、创业板卖出引擎独立管理。</span>
          <span v-else>卖出规则未配置；支持自动买入和个股手动卖出。</span>
          <span v-if="c.account?.truncated">账户记录超过本页展示上限，买入统计暂不可用。</span>
        </footer>
      </main>
    </div>
    <ManualSellDialog
      v-if="selectedSale && c.session"
      :session="c.session"
      :account-id="c.status?.account_id ?? ''"
      :position="salePosition(selectedSale.symbol) ?? selectedSale"
      :ready="saleReady && !!salePosition(selectedSale.symbol)?.can_sell"
      @close="selectedSale = null"
      @submitted="saleSubmitted"
      @expired="c.logout"
    />
    <DebugPickDialog
      v-if="debugPickOpen && c.session"
      :session="c.session"
      @close="debugPickOpen = false"
      @submitted="c.refreshAccount()"
    />
    <ManualBuyDialog
      v-if="manualBuyOpen && c.session"
      :session="c.session"
      :account-id="c.status?.account_id ?? ''"
      :ready="manualReady"
      :cash="c.accountFresh ? c.account?.cash : undefined"
      @close="manualBuyOpen = false"
      @submitted="manualSubmitted"
      @expired="c.logout"
    />
    <div v-if="c.preview" class="modal-backdrop" @click.self="c.preview = null">
      <section
        ref="confirmationDialog"
        class="confirmation"
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-title"
        @keydown="dialogKey"
      >
        <h2 id="confirm-title">核对本次操作</h2>
        <p>
          账户 {{ c.status?.account_id }} ·
          {{
            c.preview.request.action === "CONFIRM_RECOVERY" ? "确认恢复核验" : "启动真实自动交易"
          }}
        </p>
        <p v-if="c.preview.request.grant">
          当日上限 {{ money(String(c.preview.request.grant.max_buy_value)) }} 元
        </p>
        <p v-else>本次仅确认账户核验，不会提交买卖委托。</p>
        <p>
          请输入：
          <strong>{{ c.preview.confirmation_phrase }}</strong>
        </p>
        <input v-model="c.phrase" aria-label="操作确认文字" autocomplete="off" />
        <div class="actions">
          <button class="button" @click="c.preview = null">取消</button>
          <button class="button primary" :disabled="confirmDisabled" @click="c.confirm">
            确认执行
          </button>
        </div>
      </section>
    </div>
  </div>
</template>
