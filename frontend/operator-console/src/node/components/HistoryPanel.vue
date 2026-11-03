<script setup lang="ts">
import { onBeforeUnmount, reactive, ref, watch } from "vue";
import { nodeRequest, NodeApiError, errorText } from "../api";
import type { Session } from "../api";
import { money } from "../consoleTypes";

interface RecordRow {
  execution_id: string;
  identity: { symbol: string };
  name: string | null;
  first_buy_at: string | null;
  last_sell_at: string | null;
  bought_quantity: number;
  sold_quantity: number;
  remaining_quantity: number;
  buy_average_price: string | null;
  sell_average_price: string | null;
  buy_amount: string;
  sell_amount: string;
  buy_status: string;
  reason_code?: string | null;
  sell_status: string;
}
const props = defineProps<{ session: Session | null }>();
const emit = defineEmits<{ expired: [] }>();
const filters = reactive({ symbol: "", buy_from: "", buy_to: "", sell_from: "", sell_to: "" });
const applied = ref("");
const busy = ref(false);
const error = ref("");
const rows = ref<RecordRow[]>([]);
const offset = ref(0);
const hasMore = ref(false);
const queried = ref(false);
const controller = new AbortController();
let epoch = 0;
const messages: Record<string, string> = {
  QUANT_ACCESS_REQUIRED: "请先在系统设置中配置量化后台接口令牌。",
  HISTORY_ACCESS_REJECTED: "量化后台接口认证失败，请核对系统设置中的接口令牌。",
  HISTORY_UNAVAILABLE: "量化后台查询未完成，请检查连接或服务是否已更新。",
  INVALID_DATE_RANGE: "开始日期不能晚于结束日期。",
  EXPORT_TOO_LARGE: "匹配超过一万条，请缩小筛选范围后导出。",
};
function failed(reason: unknown) {
  const code = reason instanceof NodeApiError ? reason.code : "HISTORY_UNAVAILABLE";
  error.value = messages[code] ?? errorText(reason);
  if (reason instanceof NodeApiError && reason.status === 401) emit("expired");
}
function parameters() {
  const symbol = filters.symbol.trim().toUpperCase();
  if (symbol && !/^\d{6}(\.(SH|SZ))?$/.test(symbol))
    throw new Error("股票代码请填写六位数字，可带.SH或.SZ。");
  if (
    (filters.buy_from && filters.buy_to && filters.buy_from > filters.buy_to) ||
    (filters.sell_from && filters.sell_to && filters.sell_from > filters.sell_to)
  )
    throw new Error(messages.INVALID_DATE_RANGE);
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries({ ...filters, symbol }))
    if (value) params.set(key, value);
  return params.toString();
}
async function query(next = 0, reuse = false) {
  if (!props.session || busy.value) return;
  let params: string;
  try {
    params = reuse ? applied.value : parameters();
  } catch (reason) {
    error.value = (reason as Error).message;
    return;
  }
  busy.value = true;
  error.value = "";
  rows.value = [];
  queried.value = false;
  const revision = epoch;
  try {
    const result = await nodeRequest<{ items: RecordRow[]; has_more: boolean }>(
      `history?${params}&offset=${next}&limit=50`,
      "GET",
      undefined,
      undefined,
      controller.signal,
    );
    if (revision !== epoch) return;
    rows.value = result.items;
    hasMore.value = result.has_more;
    offset.value = next;
    applied.value = params;
    queried.value = true;
  } catch (reason) {
    if (revision === epoch) failed(reason);
  } finally {
    if (revision === epoch) busy.value = false;
  }
}
async function exportExcel() {
  if (!props.session || busy.value || !queried.value) return;
  const params = applied.value;
  busy.value = true;
  error.value = "";
  const revision = epoch;
  const timeout = new AbortController();
  const abort = () => timeout.abort();
  controller.signal.addEventListener("abort", abort);
  const timer = setTimeout(abort, 18000);
  try {
    const response = await fetch(`/api/v2/node/history?${params}&export=true`, {
      credentials: "same-origin",
      cache: "no-store",
      signal: timeout.signal,
    });
    if (!response.ok) {
      const value = await response.json();
      throw new NodeApiError(value.code ?? "HISTORY_UNAVAILABLE", response.status);
    }
    const blob = await response.blob();
    if (revision !== epoch) return;
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "历史交易.xlsx";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (reason) {
    if (revision === epoch) failed(reason);
  } finally {
    clearTimeout(timer);
    controller.signal.removeEventListener("abort", abort);
    if (revision === epoch) busy.value = false;
  }
}
function dateTime(value: string | null) {
  return value
    ? new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false })
    : "—";
}
function status(value: string) {
  return (
    (
      {
        WAITING: "等待买入",
        NOT_STARTED: "未开始",
        ORDERING: "委托中",
        PARTIAL: "部分成交",
        FILLED: "全部成交",
        FAILED: "失败",
        CANCELLED: "已撤单",
        SKIPPED: "已跳过",
        UNKNOWN: "待核对",
      } as Record<string, string>
    )[value] ?? value
  );
}
watch(
  () => props.session,
  () => {
    epoch++;
    rows.value = [];
    queried.value = false;
    busy.value = false;
    error.value = "";
    if (props.session) void query();
  },
  { immediate: true },
);
onBeforeUnmount(() => {
  epoch++;
  controller.abort();
});
</script>

<template>
  <section class="settings-section">
    <h2>历史交易</h2>
    <p class="muted">
      来自量化后台的当前账户正式记录。日期按北京时间，包含起止当天；买入按首次成交、卖出按最近成交筛选。
    </p>
    <p v-if="!session">请先登录本机操作员。</p>
    <form class="history-filters" @submit.prevent="query()">
      <label>
        股票代码
        <input
          v-model="filters.symbol"
          aria-label="股票代码"
          placeholder="如 000001"
          maxlength="9"
        />
      </label>
      <label>
        买入开始日期
        <input v-model="filters.buy_from" aria-label="买入开始日期" type="date" />
      </label>
      <label>
        买入结束日期
        <input v-model="filters.buy_to" aria-label="买入结束日期" type="date" />
      </label>
      <label>
        卖出开始日期
        <input v-model="filters.sell_from" aria-label="卖出开始日期" type="date" />
      </label>
      <label>
        卖出结束日期
        <input v-model="filters.sell_to" aria-label="卖出结束日期" type="date" />
      </label>
      <button class="button primary" :disabled="!session || busy">查询</button>
      <button
        type="button"
        class="button"
        :disabled="!session || busy || !queried"
        @click="exportExcel"
      >
        导出 Excel
      </button>
    </form>
    <p v-if="error" role="alert" class="notice warning">{{ error }}</p>
    <p v-if="busy" role="status">正在读取量化后台…</p>
    <p class="muted">导出已查询条件下的全部记录，最多一万条。</p>
    <div class="table-scroll">
      <table>
        <thead>
          <tr>
            <th>股票</th>
            <th>首次买入时间</th>
            <th>买入数量</th>
            <th>买入均价</th>
            <th>买入金额</th>
            <th>买入状态</th>
            <th>最近卖出时间</th>
            <th>卖出数量</th>
            <th>卖出均价</th>
            <th>卖出金额</th>
            <th>卖出状态</th>
            <th>剩余数量</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.execution_id">
            <td>
              {{ row.identity.symbol }}
              <span class="stock-name">{{ row.name }}</span>
            </td>
            <td>{{ dateTime(row.first_buy_at) }}</td>
            <td>{{ row.bought_quantity }}</td>
            <td>{{ row.buy_average_price ?? "—" }}</td>
            <td>{{ money(row.buy_amount) }}</td>
            <td>
              {{
                row.buy_status === "SKIPPED" && row.reason_code === "BOARD_NOT_ALLOWED"
                  ? "不符合板块选择，已剔除"
                  : status(row.buy_status)
              }}
            </td>
            <td>{{ dateTime(row.last_sell_at) }}</td>
            <td>{{ row.sold_quantity }}</td>
            <td>{{ row.sell_average_price ?? "—" }}</td>
            <td>{{ money(row.sell_amount) }}</td>
            <td>{{ status(row.sell_status) }}</td>
            <td>{{ row.remaining_quantity }}</td>
          </tr>
          <tr v-if="!rows.length">
            <td colspan="12" class="empty-cell">
              {{ queried ? "没有符合条件的历史记录" : "历史记录待读取" }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-if="queried" class="actions">
      <button class="button" :disabled="busy || offset === 0" @click="query(offset - 50, true)">
        上一页
      </button>
      <span>第 {{ offset / 50 + 1 }} 页</span>
      <button class="button" :disabled="busy || !hasMore" @click="query(offset + 50, true)">
        下一页
      </button>
    </div>
  </section>
</template>

<style scoped>
.history-filters {
  display: flex;
  flex-wrap: wrap;
  align-items: end;
  gap: 12px;
  margin: 20px 0;
}
.history-filters label {
  display: grid;
  gap: 6px;
}
.history-filters input {
  max-width: 180px;
}
</style>
