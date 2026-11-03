<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import type { Session } from "../api";
import { errorText, nodeRequest, NodeApiError } from "../api";
import { money } from "../consoleTypes";

interface Preview {
  preview_id: string;
  client_order_id: string;
  symbol: string;
  name: string;
  limit_price: string;
  quantity: number;
  amount: string;
  reserved_amount: string;
  cash: string;
  expires_at: string;
}
const props = defineProps<{
  session: Session;
  accountId: string;
  ready: boolean;
  cash?: string | undefined;
}>();
const emit = defineEmits<{ close: []; submitted: [status: string]; expired: [] }>();
const symbol = ref("");
const price = ref("");
const quantity = ref("");
const preview = ref<Preview | null>(null);
const busy = ref(false);
const error = ref("");
const pending = ref("");
const now = ref(Date.now());
const dialog = ref<HTMLElement>();
const storageKey = `ats:manual-buy:${props.accountId}`;
const lifecycle = new AbortController();
let alive = true;
let timer: ReturnType<typeof setInterval> | undefined;
let previousFocus: HTMLElement | null = null;
const estimated = computed(() => {
  const value = Number(price.value) * Number(quantity.value);
  return Number.isFinite(value) && value > 0 ? value.toFixed(2) : "—";
});
const valid = computed(
  () =>
    /^\d{6}(\.(SH|SZ))?$/i.test(symbol.value.trim()) &&
    /^(0|[1-9]\d{0,7})(\.\d{1,4})?$/.test(price.value) &&
    Number(price.value) > 0 &&
    /^[1-9]\d{0,6}$/.test(quantity.value) &&
    Number(quantity.value) <= 1_000_000,
);
const expired = computed(
  () => !!preview.value && now.value >= Date.parse(preview.value.expires_at),
);
async function call<T>(path: string, body: object): Promise<T> {
  return nodeRequest<T>(
    `orders/manual-buy/${path}`,
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
async function prepare() {
  if (!valid.value || busy.value || !props.ready || pending.value) return;
  busy.value = true;
  error.value = "";
  try {
    const value = await call<Preview>("preview", {
      symbol: symbol.value.trim().toUpperCase(),
      limit_price: price.value,
      quantity: Number(quantity.value),
    });
    if (alive) preview.value = value;
  } catch (reason) {
    if (alive) failed(reason);
  } finally {
    busy.value = false;
  }
}
function complete(status: string) {
  if (status === "NOT_SUBMITTED") {
    sessionStorage.removeItem(storageKey);
    pending.value = "";
    preview.value = null;
    error.value = "已核对本次没有提交委托，可以重新填写。";
    return;
  }
  if (
    [
      "PERSISTING",
      "DISPATCHING",
      "SDK_ACCEPTED",
      "RECOVERED",
      "UNCERTAIN",
      "PERSISTENCE_UNCERTAIN",
      "PERSISTENCE_FAILED",
      "NOT_DISPATCHED",
      "RECOVERY_REQUIRED",
      "BEGIN_FAILED",
      "EVIDENCE_MISMATCH",
    ].includes(status)
  ) {
    sessionStorage.removeItem(storageKey);
    pending.value = "";
    emit("submitted", status);
  }
}
async function confirm() {
  const value = preview.value;
  if (!value || expired.value || busy.value || !props.ready || pending.value) return;
  try {
    sessionStorage.setItem(storageKey, value.client_order_id);
  } catch {
    error.value = "无法保存本次委托查询编号，请允许浏览器会话存储后重试。";
    return;
  }
  pending.value = value.client_order_id;
  busy.value = true;
  error.value = "";
  try {
    const result = await call<{ status: string }>("confirm", { preview_id: value.preview_id });
    if (alive) complete(result.status);
  } catch (reason) {
    if (alive) {
      failed(reason);
      if (reason instanceof NodeApiError && reason.code === "MANUAL_BUY_BUSY") {
        sessionStorage.removeItem(storageKey);
        pending.value = "";
        return;
      }
      if (
        reason instanceof NodeApiError &&
        [
          "MANUAL_BUY_REJECTED",
          "MANUAL_BUY_PREVIEW_EXPIRED",
          "MANUAL_BUY_PREVIEW_NOT_FOUND",
          "MANUAL_BUY_SETTINGS_CHANGED",
        ].includes(reason.code)
      ) {
        sessionStorage.removeItem(storageKey);
        pending.value = "";
        preview.value = null;
      } else error.value += " 请先查询本次结果，勿重复提交。";
    }
  } finally {
    busy.value = false;
  }
}
async function lookup() {
  if (!pending.value || busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    const result = await call<{ status: string }>("result", { client_order_id: pending.value });
    if (alive) complete(result.status);
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
    error.value = "浏览器会话存储不可用，暂不能提交买入。";
  }
  timer = setInterval(() => (now.value = Date.now()), 1000);
  await nextTick();
  dialog.value?.querySelector<HTMLElement>("input, button")?.focus();
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
      aria-labelledby="manual-buy-title"
      @keydown="keydown"
    >
      <h2 id="manual-buy-title">手动买入</h2>
      <p class="muted">成交后纳入系统托管。卖出规则待配置，当前不会自动卖出。</p>
      <p v-if="error" role="alert" class="form-error">{{ error }}</p>
      <template v-if="pending">
        <p role="status">本次委托结果待核对，请查询原委托，勿重复提交。</p>
        <div class="actions">
          <button class="button" :disabled="busy" @click="close">关闭</button>
          <button class="button primary" :disabled="busy" @click="lookup">查询本次结果</button>
        </div>
      </template>
      <template v-else-if="preview">
        <p>
          <strong>{{ preview.name }} · {{ preview.symbol }}</strong>
        </p>
        <p>限价 {{ preview.limit_price }} 元/股 × {{ preview.quantity }} 股</p>
        <p>预计委托金额 ¥ {{ money(preview.amount) }}，手续费另计。</p>
        <p class="muted">
          含费用预留 ¥ {{ money(preview.reserved_amount) }} · 可用资金 ¥ {{ money(preview.cash) }}
        </p>
        <p v-if="expired" role="status">本次核对已过期，请返回重新核对。</p>
        <div class="actions">
          <button class="button" :disabled="busy" @click="preview = null">返回修改</button>
          <button class="button primary" :disabled="busy || expired || !ready" @click="confirm">
            确认买入
          </button>
        </div>
      </template>
      <form v-else @submit.prevent="prepare">
        <label>
          股票代码
          <input
            v-model="symbol"
            aria-label="股票代码"
            placeholder="例如 000001"
            autocomplete="off"
            maxlength="9"
            required
          />
        </label>
        <label>
          买入价格（元/股）
          <input
            v-model="price"
            aria-label="买入价格"
            inputmode="decimal"
            autocomplete="off"
            required
          />
        </label>
        <label>
          买入数量（股）
          <input
            v-model="quantity"
            aria-label="买入数量"
            inputmode="numeric"
            autocomplete="off"
            required
          />
        </label>
        <p>预计委托金额 ¥ {{ estimated }}，手续费另计。</p>
        <p class="muted">可用资金 ¥ {{ money(cash) }}。股票名称和申报规则将在下一步核对。</p>
        <p v-if="!ready" class="muted">请等待账户核验和连接恢复；仅交易连接可提交。</p>
        <div class="actions">
          <button type="button" class="button" :disabled="busy" @click="close">取消</button>
          <button type="submit" class="button primary" :disabled="busy || !valid || !ready">
            {{ busy ? "核对中…" : "下一步" }}
          </button>
        </div>
      </form>
    </section>
  </div>
</template>
