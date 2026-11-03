<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import type { Session } from "../api";
import { errorText, nodeRequest, NodeApiError } from "../api";
import type { SellPosition } from "../consoleTypes";
import { money } from "../consoleTypes";

interface Preview {
  operation_id: string;
  symbol: string;
  name: string;
  quantity: number;
  limit_price: string;
  amount: string;
  child_count: number;
  expires_at: string;
}
interface Result {
  operation_id: string;
  status: string;
  children: { client_order_id: string; submitted: boolean; status: string }[];
}
const props = defineProps<{
  session: Session;
  accountId: string;
  position: SellPosition;
  ready: boolean;
}>();
const emit = defineEmits<{ close: []; submitted: [status: string]; expired: [] }>();
const price = ref("");
const quantity = ref(String(Math.min(100, props.position.sellable)));
const preview = ref<Preview | null>(null);
const busy = ref(false);
const error = ref("");
const notice = ref("");
const pending = ref("");
const storageReady = ref(true);
const now = ref(Date.now());
const dialog = ref<HTMLElement>();
const storageKey = `ats:manual-sell:${props.accountId}:${props.position.symbol}`;
const lifecycle = new AbortController();
let alive = true;
let timer: ReturnType<typeof setInterval> | undefined;
let previousFocus: HTMLElement | null = null;
const valid = computed(
  () =>
    /^(0|[1-9]\d{0,7})(\.\d{1,4})?$/.test(price.value) &&
    Number(price.value) > 0 &&
    /^[1-9]\d{0,6}$/.test(quantity.value) &&
    Number(quantity.value) <= Math.min(1_000_000, props.position.sellable),
);
const expired = computed(
  () => !!preview.value && now.value >= Date.parse(preview.value.expires_at),
);
function call<T>(action: string, body: object): Promise<T> {
  return nodeRequest<T>(
    `orders/manual-sell/${action}`,
    "POST",
    body,
    props.session.csrf_token,
    lifecycle.signal,
  );
}
function failed(reason: unknown) {
  error.value = errorText(reason);
  if (typeof reason === "object" && reason !== null && "status" in reason && reason.status === 401)
    emit("expired");
}
function writePending(value: string) {
  try {
    if (value) sessionStorage.setItem(storageKey, value);
    else sessionStorage.removeItem(storageKey);
    pending.value = value;
    return true;
  } catch {
    storageReady.value = false;
    error.value = "浏览器会话存储不可用，暂不能提交卖出。";
    return false;
  }
}
async function prepare() {
  if (!valid.value || busy.value || !props.ready || !storageReady.value || pending.value) return;
  busy.value = true;
  error.value = "";
  try {
    const result = await call<Preview>("preview", {
      symbol: props.position.symbol,
      limit_price: price.value,
      quantity: Number(quantity.value),
    });
    if (alive) preview.value = result;
  } catch (reason) {
    if (alive) failed(reason);
  } finally {
    busy.value = false;
  }
}
function accept(result: Result, closing: boolean) {
  if (result.operation_id !== pending.value) {
    error.value = "卖出操作编号不一致，请核对原操作，勿重复提交。";
    return;
  }
  if (
    ["SUBMITTED", "PARTIAL_SUBMITTED", "UNKNOWN", "NOT_SUBMITTED", "ARCHIVED"].includes(
      result.status,
    )
  ) {
    if (!writePending("")) return;
    preview.value = null;
  }
  if (result.status === "NOT_SUBMITTED") {
    notice.value = "已核对本次没有提交委托，可以重新填写。";
    return;
  }
  notice.value =
    result.status === "PARTIAL_SUBMITTED"
      ? "部分子委托已提交，其余未提交，请查看列表。"
      : result.status === "UNKNOWN"
        ? "委托结果待核对，请查看原委托。"
        : "卖出申请已接收，实际成交以列表核对结果为准。";
  if (closing) emit("submitted", result.status);
}
async function confirm() {
  const value = preview.value;
  if (!value || busy.value || expired.value || !props.ready || !storageReady.value) return;
  if (!writePending(value.operation_id)) return;
  busy.value = true;
  error.value = "";
  try {
    const result = await call<Result>("confirm", { operation_id: value.operation_id });
    if (alive) accept(result, true);
  } catch (reason) {
    if (alive) {
      failed(reason);
      if (
        reason instanceof NodeApiError &&
        [
          "MANUAL_SELL_NOT_READY",
          "MANUAL_SELL_BUSY",
          "MANUAL_SELL_ORDER_CONFLICT",
          "MANUAL_SELL_INSUFFICIENT",
          "MANUAL_SELL_INSTRUMENT_UNAVAILABLE",
          "MANUAL_SELL_PREVIEW_NOT_FOUND",
          "MANUAL_SELL_PREVIEW_STALE",
          "MANUAL_SELL_PREVIEW_EXPIRED",
          "MANUAL_SELL_SETTINGS_CHANGED",
          "MANUAL_SELL_OUTSIDE_WINDOW",
        ].includes(reason.code)
      ) {
        writePending("");
        preview.value = null;
      } else error.value += " 请先查询本次结果，勿重复提交。";
    }
  } finally {
    busy.value = false;
  }
}
async function lookup(closing = false) {
  if (!pending.value || busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    const result = await call<Result>("result", { operation_id: pending.value });
    if (alive) accept(result, closing);
  } catch (reason) {
    if (alive) failed(reason);
  } finally {
    busy.value = false;
  }
}
function close() {
  if (!busy.value) emit("close");
}
function keydown(event: KeyboardEvent) {
  if (event.key === "Escape") {
    event.preventDefault();
    close();
  }
  if (event.key !== "Tab") return;
  const nodes = dialog.value?.querySelectorAll<HTMLElement>(
    "button:not(:disabled), input:not(:disabled)",
  );
  if (!nodes?.length) {
    event.preventDefault();
    return;
  }
  const first = nodes[0]!,
    last = nodes[nodes.length - 1]!;
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}
onMounted(async () => {
  previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  try {
    pending.value = sessionStorage.getItem(storageKey) ?? "";
  } catch {
    storageReady.value = false;
    error.value = "浏览器会话存储不可用，暂不能提交卖出。";
  }
  timer = setInterval(() => (now.value = Date.now()), 1000);
  await nextTick();
  dialog.value?.querySelector<HTMLElement>("input, button")?.focus();
  if (pending.value) await lookup();
});
onBeforeUnmount(() => {
  alive = false;
  lifecycle.abort();
  clearInterval(timer);
  previousFocus?.focus();
});
</script>

<template>
  <div class="modal-backdrop" @click.self="close">
    <section
      ref="dialog"
      class="confirmation manual-buy-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="manual-sell-title"
      @keydown="keydown"
    >
      <h2 id="manual-sell-title">手动卖出</h2>
      <p>
        <strong>
          {{ preview?.symbol ?? position.symbol }} ·
          {{ preview?.name ?? position.name ?? "名称待读取" }}
        </strong>
      </p>
      <p>本系统剩余 {{ position.remaining }} 股，当前可卖 {{ position.sellable }} 股。</p>
      <p class="muted">本次仅卖出这只股票，不开启自动交易。成交后更新系统持仓。</p>
      <p v-if="error" class="form-error" role="alert">{{ error }}</p>
      <p v-if="!ready" role="status">账户或可卖状态已变化，请关闭后刷新并重新核对。</p>
      <p v-if="notice" role="status">{{ notice }}</p>
      <template v-if="pending">
        <p role="status">已有卖出操作待核对，请查询原操作，勿重复提交。</p>
        <div class="actions">
          <button class="button" :disabled="busy" @click="close">关闭</button>
          <button class="button primary" :disabled="busy" @click="lookup(true)">
            查询本次结果
          </button>
        </div>
      </template>
      <template v-else-if="preview">
        <p>限价 {{ preview.limit_price }} 元/股 × {{ preview.quantity }} 股</p>
        <p>预计委托金额 ¥ {{ money(preview.amount) }}，手续费另计。</p>
        <p v-if="preview.child_count > 1">
          本次按持仓来源拆为 {{ preview.child_count }} 笔委托，逐笔显示结果；异常时停止剩余委托。
        </p>
        <p v-if="expired" role="status">本次核对已过期，请返回重新核对。</p>
        <div class="actions">
          <button class="button" :disabled="busy" @click="preview = null">返回修改</button>
          <button
            class="button primary"
            :disabled="busy || expired || !ready || !storageReady"
            @click="confirm"
          >
            确认卖出
          </button>
        </div>
      </template>
      <form v-else @submit.prevent="prepare">
        <label>
          卖出价格（元/股）
          <input
            v-model="price"
            aria-label="卖出价格"
            inputmode="decimal"
            autocomplete="off"
            required
          />
        </label>
        <label>
          卖出数量（股）
          <input
            v-model="quantity"
            aria-label="卖出数量"
            inputmode="numeric"
            autocomplete="off"
            required
          />
        </label>
        <button
          class="button"
          type="button"
          :disabled="busy || !ready"
          @click="quantity = String(position.sellable)"
        >
          全部可卖
        </button>
        <div class="actions">
          <button class="button" type="button" :disabled="busy" @click="close">取消</button>
          <button
            class="button primary"
            type="submit"
            :disabled="busy || !valid || !ready || !storageReady"
          >
            下一步
          </button>
        </div>
      </form>
    </section>
  </div>
</template>

<style scoped>
.manual-buy-dialog {
  width: min(460px, calc(100vw - 40px));
  max-height: 85vh;
  overflow: auto;
}
label {
  display: grid;
  gap: 8px;
  margin: 22px 0;
}
input {
  width: 100%;
  box-sizing: border-box;
  padding: 11px 12px;
  border: 1px solid #cbd8eb;
  border-radius: 6px;
}
</style>
