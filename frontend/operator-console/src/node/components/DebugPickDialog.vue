<script setup lang="ts">
import { ref, onBeforeUnmount, onMounted } from "vue";
import { nodeRequest, errorText, type Session } from "../api";
const props = defineProps<{ session: Session }>();
const emit = defineEmits<{ close: []; submitted: [] }>();
interface Preview {
  preview_id: string;
  symbol: string;
  name: string;
  phrase: string;
  position_limit: number;
  daily_symbols_limit: number;
  cash: string;
  symbol_limit: string;
  daily_limit: string;
  cancel_after_seconds: number;
  max_retries: number;
  expires_at: string;
}
const symbol = ref("");
const phrase = ref("");
const preview = ref<Preview | null>(null);
const busy = ref(false);
const error = ref("");
const result = ref("");
const uncertain = ref(false);
const dialog = ref<HTMLElement>();
const lifecycle = new AbortController();
let alive = true;
let previousFocus: HTMLElement | null = null;
onMounted(() => {
  previousFocus = document.activeElement as HTMLElement;
  dialog.value?.focus();
});
onBeforeUnmount(() => {
  alive = false;
  lifecycle.abort();
  previousFocus?.focus();
});
async function call<T>(path: string, body: object): Promise<T> {
  return nodeRequest<T>(
    `debug-pick/${path}`,
    "POST",
    body,
    props.session.csrf_token,
    lifecycle.signal,
  );
}
async function prepare() {
  if (busy.value || !/^\d{6}(\.(SH|SZ))?$/i.test(symbol.value.trim())) return;
  busy.value = true;
  error.value = "";
  try {
    const value = await call<Preview>("preview", { symbol: symbol.value.trim().toUpperCase() });
    if (alive) preview.value = value;
  } catch (reason) {
    if (alive) error.value = errorText(reason);
  } finally {
    busy.value = false;
  }
}
async function confirm() {
  const item = preview.value;
  if (!item || busy.value || phrase.value !== item.phrase) return;
  if (!uncertain.value && Date.now() >= Date.parse(item.expires_at)) {
    error.value = "预览已过期，请重新预览。";
    return;
  }
  busy.value = true;
  error.value = "";
  try {
    const value = await call<{ status: string }>("confirm", {
      preview_id: item.preview_id,
      phrase: phrase.value,
    });
    if (alive) {
      result.value =
        value.status === "STARTED"
          ? "已接收调试选股，正在按自动策略执行。请在交易过程窗口查看结果。"
          : "本次结果待核对，请查看交易过程及委托列表。";
      uncertain.value = false;
      emit("submitted");
    }
  } catch (reason) {
    if (alive) {
      error.value = errorText(reason);
      uncertain.value = true;
    }
  } finally {
    busy.value = false;
  }
}
function trap(event: KeyboardEvent) {
  if (event.key === "Escape" && !busy.value) emit("close");
  if (event.key !== "Tab") return;
  const nodes = dialog.value?.querySelectorAll<HTMLElement>(
    "button:not(:disabled), input:not(:disabled)",
  );
  if (!nodes?.length) return;
  const first = nodes[0],
    last = nodes[nodes.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last?.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first?.focus();
  }
}
</script>
<template>
  <div class="modal-backdrop">
    <section
      ref="dialog"
      class="confirmation manual-buy-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="debug-pick-heading"
      tabindex="-1"
      @keydown="trap"
    >
      <h2 id="debug-pick-heading">调试选股买入</h2>
      <p class="form-error">此操作使用真实账户、真实资金进行自动买入。</p>
      <p class="muted">
        仅14:40至14:55可启动，每天限一只调试股票。执行期间与正式选股互斥，重启不自行续发。需要先关闭持续自动买入并完成账户核验。
      </p>
      <p v-if="error" role="alert" class="form-error">{{ error }}</p>
      <p v-if="result" role="status">{{ result }}</p>
      <template v-else-if="preview">
        <p>
          <strong>{{ preview.symbol }} {{ preview.name }}</strong>
        </p>
        <p>
          单股上限 ¥{{ preview.symbol_limit }}；每日预算上限 ¥{{ preview.daily_limit }}；可用资金
          ¥{{ preview.cash }}。实际额度仍扣除已有占用。
        </p>
        <p>
          每日最多新买入{{ preview.daily_symbols_limit }}只；系统持仓上限{{
            preview.position_limit
          }}只。
        </p>
        <p>
          自动取价和计算股数；未成交{{ preview.cancel_after_seconds }}秒后撤单，最多重报{{
            preview.max_retries
          }}次。仍检查行情、仓位及全部风控，可能部分成交或未买入。
        </p>
        <label for="debug-confirm">请输入：{{ preview.phrase }}</label>
        <input
          id="debug-confirm"
          v-model="phrase"
          :disabled="busy || !!result"
          autocomplete="off"
        />
        <p v-if="uncertain" class="form-error">
          请求结果未知，可用同一次确认核对；请勿重新创建交易。
        </p>
      </template>
      <template v-else>
        <label for="debug-symbol">股票代码</label>
        <input
          id="debug-symbol"
          v-model="symbol"
          :disabled="busy"
          maxlength="9"
          placeholder="例如600698"
          autocomplete="off"
        />
      </template>
      <div class="actions">
        <button class="button" :disabled="busy" @click="emit('close')">关闭</button>
        <button
          v-if="!preview && !result"
          class="button primary"
          :disabled="busy || !symbol.trim()"
          @click="prepare"
        >
          预览
        </button>
        <button
          v-if="preview && !result"
          class="button primary"
          :disabled="busy || phrase !== preview.phrase"
          @click="confirm"
        >
          {{ uncertain ? "核对本次确认" : "确认真实自动买入" }}
        </button>
        <button
          v-if="preview && !result && !uncertain"
          class="button"
          :disabled="busy"
          @click="
            preview = null;
            phrase = '';
          "
        >
          重新预览
        </button>
      </div>
    </section>
  </div>
</template>
