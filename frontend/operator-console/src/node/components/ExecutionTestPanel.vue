<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { errorText, nodeRequest, NodeApiError } from "../api";
import type { Session } from "../api";
import { money, timeText } from "../consoleTypes";

interface Execution {
  execution_id: string;
  buy_status: string;
  sell_status: string;
  bought_quantity: number;
  sold_quantity: number;
  buy_average_price: string | null;
  buy_amount: string;
  first_buy_at: string | null;
}
interface TestEvent {
  seq: number;
  occurred_at: string;
  fact: {
    kind: string;
    side?: string;
    status?: string;
    quantity?: number;
    price?: string;
    reason_code?: string;
  };
}
interface TestReport {
  state: string;
  busy: boolean;
  error: string | null;
  selection_date: string | null;
  maximum: number | null;
  amount: string | null;
  verified_at: string | null;
  uploaded_events: number;
  phases: string[];
  rows: { symbol: string; name: string | null; execution: Execution | null; events: TestEvent[] }[];
}
const props = defineProps<{ session: Session | null }>();
const emit = defineEmits<{ expired: [] }>();
const report = ref<TestReport | null>(null);
const error = ref("");
const submitting = ref(false);
const expanded = ref<string | null>(null);
const lifecycle = new AbortController();
let timer: ReturnType<typeof setTimeout> | undefined;
let alive = true;
let epoch = 0;
let revision = 0;
const rows = computed(() => report.value?.rows ?? []);
const busy = computed(() => submitting.value || !!report.value?.busy);
const completed = computed(
  () => rows.value.filter((row) => row.execution?.buy_status === "FILLED").length,
);
const notBought = computed(
  () => rows.value.filter((row) => !row.execution?.bought_quantity).length,
);
const messages: Record<string, string> = {
  IDLE: "先获取最近有数据的真实历史选股结果。",
  FETCHING: "正在查找最近有数据的选股日期…",
  READY: "股票已获取，可以运行模拟买卖和回传测试。",
  EMPTY: "最近366天没有可用的优评级选股结果。",
  SIMULATING: "正在生成明确标记为TEST的模拟交易过程…",
  UPLOADING: "正在回传，并核对后台主表和事件明细…",
  SYNCED: "回传已核验：后台主表、事件明细和本机一致。",
  ERROR: "测试尚未完成，已保存的事件会保留，重试不会重新买卖。",
};
const errors: Record<string, string> = {
  TEST_HTTP_CREDENTIALS_REQUIRED: "请在系统设置保存接口访问令牌，测试与正式共用。",
  TEST_HTTP_ORIGIN_REQUIRED: "HTTP后台地址尚未配置，请检查系统设置。",
  TEST_CONNECTION_REQUIRED: "测试连接尚未配置，请完成本机测试接入。",
  TEST_PENDING_UPLOAD: "有尚未确认的测试事件，请先重试回传。",
  TEST_TOO_MANY_STOCKS: "该日期超过200只股票，请使用专用批量验收工具；名单未截断。",
  TEST_FETCH_REQUIRED: "请先获取测试股票。",
  TEST_RUN_REQUIRED: "请先运行模拟。",
  TEST_STORAGE_INVALID: "本机测试记录无法读取，请检查文件后重试。",
  TEST_REQUEST_FAILED: "测试请求或回查未通过，请检查服务器连接后重试。",
};
function label(value: string | undefined): string {
  return (
    (
      {
        CANDIDATE: "收到股票",
        PROGRESS: "执行进度",
        SUBMISSION: "模拟提交",
        ORDER: "模拟委托",
        TRADE: "模拟成交",
        CANCEL_ERROR: "模拟撤单失败",
        REQUEST_ERROR: "模拟报单失败",
        BUY: "买入",
        SELL: "卖出",
        SDK_ACCEPTED: "提交已接受",
        ACTIVE: "委托中",
        PARTIAL: "部分成交",
        FILLED: "已成交",
        REJECTED: "失败",
        READY: "等待模拟",
        POSITION_CAPACITY: "名额已满",
        SKIPPED_MIN_ORDER: "金额不足",
        TEST_BUY_REJECTED: "模拟买入失败",
        TEST_SELL_REJECTED: "模拟卖出失败",
        TEST_CANCEL_FAILED: "模拟撤单失败",
      } as Record<string, string>
    )[value ?? ""] ??
    value ??
    "—"
  );
}
async function load() {
  if (!alive || !props.session) return;
  const current = epoch;
  const sequence = ++revision;
  try {
    const value = await nodeRequest<TestReport>(
      "execution-test",
      "GET",
      undefined,
      props.session.csrf_token,
      lifecycle.signal,
    );
    if (!alive || current !== epoch || sequence !== revision) return;
    report.value = value;
    error.value = "";
    clearTimeout(timer);
    if (value.busy) timer = setTimeout(() => void load(), 1000);
  } catch (reason) {
    if (!alive || current !== epoch || sequence !== revision) return;
    error.value = errorText(reason);
    if (reason instanceof NodeApiError && reason.status === 401) emit("expired");
    else timer = setTimeout(() => void load(), 3000);
  }
}
async function act(action: "FETCH" | "RUN" | "RETRY") {
  if (!props.session || busy.value) return;
  const current = epoch;
  const sequence = ++revision;
  clearTimeout(timer);
  submitting.value = true;
  error.value = "";
  try {
    const value = await nodeRequest<TestReport>(
      "execution-test",
      "POST",
      { action },
      props.session.csrf_token,
      lifecycle.signal,
    );
    if (alive && current === epoch && sequence === revision) report.value = value;
  } catch (reason) {
    if (alive && current === epoch && sequence === revision) {
      error.value = errorText(reason);
      if (reason instanceof NodeApiError && reason.status === 401) emit("expired");
    }
  } finally {
    submitting.value = false;
    if (alive && current === epoch) await load();
  }
}
watch(
  () => props.session,
  () => {
    epoch++;
    report.value = null;
    expanded.value = null;
    clearTimeout(timer);
    if (props.session) void load();
  },
  { immediate: true },
);
onBeforeUnmount(() => {
  alive = false;
  lifecycle.abort();
  clearTimeout(timer);
});
</script>

<template>
  <section class="execution-test-panel stock-section" aria-label="测试交易">
    <div class="section-heading">
      <h2>
        测试交易
        <span class="test-badge">TEST · 模拟</span>
      </h2>
      <button class="text-button" :disabled="!session || busy" @click="load">刷新测试结果</button>
    </div>
    <p class="notice">
      股票来自真实历史选股，买卖为模拟。不会向QMT发单，也不占正式交易名额。模拟买价10.00元，卖价11.00元；这些价格不是历史行情或真实成交。
    </p>
    <p v-if="!session">登录本机操作员后可获取测试股票。</p>
    <div class="actions test-actions">
      <button class="button" :disabled="!session || busy" @click="act('FETCH')">
        获取测试股票
      </button>
      <button
        class="button primary"
        :disabled="!session || busy || !rows.length"
        @click="act('RUN')"
      >
        {{ report?.verified_at ? "再次核验回传" : "运行模拟并回传" }}
      </button>
      <button
        v-if="report?.state === 'ERROR' && rows.some((row) => row.events.length)"
        class="button"
        :disabled="!session || busy"
        @click="act('RETRY')"
      >
        重试回传
      </button>
    </div>
    <p role="status" aria-live="polite">
      {{ messages[report?.state ?? "IDLE"] }}
      <template v-if="report?.state === 'UPLOADING'">
        已确认{{ report.uploaded_events }}条事件。
      </template>
    </p>
    <p v-if="error || report?.error" class="notice warning" role="alert">
      {{ error || errors[report?.error ?? ""] || "测试未完成" }}
    </p>
    <p v-if="report?.selection_date" class="muted">
      原选股日期：{{ report.selection_date }} · {{ rows.length }}只股票 ·
      {{ report.phases.map((phase) => `${phase.slice(0, 2)}:${phase.slice(2)}`).join(" / ") }}
      <br />
      本次测试最多买入{{ report.maximum }}只，单只上限¥{{
        money(report.amount)
      }}。参数在首次获取时保存，重复核验沿用原记录。
    </p>
    <div v-if="rows.length" class="buy-progress">
      <strong>模拟已完成买入 {{ completed }}只</strong>
      · 未买入 {{ notBought }}只
      <span v-if="report?.verified_at">· 上次核验 {{ report.verified_at }}</span>
    </div>
    <div class="table-scroll">
      <table class="stock-table">
        <thead>
          <tr>
            <th>股票</th>
            <th>模拟买入</th>
            <th>模拟卖出</th>
            <th>买入时间</th>
            <th class="numeric">买入均价</th>
            <th class="numeric">买入金额</th>
            <th>回传</th>
            <th><span class="sr-only">事件详情</span></th>
          </tr>
        </thead>
        <tbody>
          <template v-for="row in rows" :key="row.symbol">
            <tr>
              <td>
                <strong>{{ row.symbol }}</strong>
                <span class="stock-name">{{ row.name ?? "名称待获取" }}</span>
              </td>
              <td>{{ row.execution ? label(row.execution.buy_status) : "等待模拟" }}</td>
              <td>{{ row.execution ? label(row.execution.sell_status) : "—" }}</td>
              <td>{{ timeText(row.execution?.first_buy_at) }}</td>
              <td class="numeric">{{ row.execution?.buy_average_price ?? "—" }}</td>
              <td class="numeric">{{ money(row.execution?.buy_amount) }}</td>
              <td>
                {{
                  report?.state === "SYNCED" ? "已核验" : row.events.length ? "待核验" : "未开始"
                }}
              </td>
              <td>
                <button
                  class="text-button"
                  :aria-label="`查看${row.symbol}测试事件`"
                  :aria-expanded="expanded === row.symbol"
                  @click="expanded = expanded === row.symbol ? null : row.symbol"
                >
                  详情
                </button>
              </td>
            </tr>
            <tr v-if="expanded === row.symbol" class="detail-row">
              <td colspan="8">
                <p>
                  模拟买入{{ row.execution?.bought_quantity ?? 0 }}股，模拟卖出{{
                    row.execution?.sold_quantity ?? 0
                  }}股。失败及部分成交过程保留在下面。
                </p>
                <ol v-if="row.events.length" class="test-events">
                  <li v-for="event in row.events" :key="event.seq">
                    {{ event.occurred_at }} · {{ label(event.fact.kind) }}
                    {{ label(event.fact.side) }} ·
                    {{ label(event.fact.reason_code ?? event.fact.status) }}
                    <template v-if="event.fact.quantity">· {{ event.fact.quantity }}股</template>
                    <template v-if="event.fact.price">· ¥{{ event.fact.price }}</template>
                  </li>
                </ol>
                <p v-else>已收到真实选股，尚未生成模拟交易。</p>
              </td>
            </tr>
          </template>
          <tr v-if="!rows.length">
            <td colspan="8" class="empty-cell">点击“获取测试股票”，成功后股票会显示在这里。</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
.test-badge {
  display: inline-block;
  margin-left: 8px;
  padding: 4px 8px;
  border-radius: 6px;
  background: #fff0d4;
  color: #854d0e;
  font-size: 12px;
  vertical-align: middle;
}
.test-actions {
  flex-wrap: wrap;
  margin: 18px 0;
}
.test-events {
  max-height: 360px;
  overflow: auto;
  padding-left: 24px;
  line-height: 1.8;
}
</style>
