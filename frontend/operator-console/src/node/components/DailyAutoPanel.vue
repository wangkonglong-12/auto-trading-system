<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { DailyAutoPreview, DailyAutoState } from "../api";
import { money } from "../consoleTypes";
const props = defineProps<{
  state: DailyAutoState | null;
  preview: DailyAutoPreview | null;
  busy: boolean;
  ready: boolean;
  canStop: boolean;
  now: number;
}>();
const emit = defineEmits<{
  preview: [];
  confirm: [identity: string, phrase: string];
  disable: [];
  cancel: [];
}>();
const phrase = ref("");
watch(
  () => props.preview,
  () => {
    phrase.value = "";
  },
);
const explanation = computed(() => {
  if (props.state?.mode === "DEBUG_ONCE")
    return props.state.enabled
      ? "当前为单次调试选股执行，不会保存持续开关；重启后不自行续发。"
      : "单次调试已停止，已有委托及成交请在列表核对；今天不会自动切回正式选股。";
  const reason = props.state?.reason;
  const labels: Record<string, string> = {
    NOT_ENABLED: "尚未开启，不会自动买入。",
    WAITING_WINDOW: "已开启，等待尾盘买入时段。",
    WAITING_FORMAL_PICKS: "已开启，等待今天的正式优名单；无需每天点启动。",
    WAITING_NEXT_DAY: "今天的交易窗口已结束，等待下一交易日。",
    NON_TRADING_DAY: "今天不在已核验的交易日历中，等待交易日。",
    AUTO_RECOVERY: "正在自动核验账户和本系统订单，无需手动操作。",
    BUY_ONLY: "正在按每日限额自动买入，自动卖出未开启。",
    OPERATOR_DISABLED: "你已关闭持续自动买入，不会自行重新开启。",
    SETTINGS_CHANGED:
      "交易设置已变更，持续自动买入已关闭。请重新打开主程序加载新设置后，再确认开启。",
    RECOVERY_BLOCKED: "自动核验未通过，已暂停。不会跳过未核清的系统交易。",
    CALENDAR_COVERAGE_MISSING: "执行日历未覆盖下一交易日，已暂停，需要更新日历。",
    CONSENT_INVALID: "持续开关的本机授权记录无法验证，保持暂停。",
    CONSENT_SAVE_FAILED: "保存持续开关失败，当前进程保持暂停；请检查本机数据存储。",
    AUTO_CHECK_FAILED: "自动准备或收盘核验失败，已暂停，请查看运行记录。",
    ENGINE_PAUSED: "交易引擎异常暂停，持续自动买入已关闭，请核对原因后重新开启。",
  };
  return labels[reason ?? ""] ?? "等待系统状态。";
});
const canConfirm = computed(
  () =>
    !!props.preview &&
    !props.busy &&
    props.ready &&
    phrase.value === props.preview.confirmation_phrase &&
    Date.parse(props.preview.expires_at) > props.now,
);
const recoveryExplanation = computed(() => {
  const meanings: Record<string, string> = {
    UNOWNED_BROKER_ORDER: "券商有无法匹配到本系统记录的委托，需要核对来源。",
    UNOWNED_BROKER_TRADE: "券商成交缺少对应的本机发单记录。",
    ORDER_NOT_OBSERVED_IS_NOT_PROOF: "系统记录过发单，但券商尚未查到结果，不能认定没有发送。",
    ORDER_ACTIVE_OR_TRADE_MISMATCH: "本系统有未结束或成交明细不一致的委托。",
    POSITION_OWNERSHIP_UNRESOLVED: "本系统应持有的股票数量与账户实际持仓不一致。",
    ACCOUNT_OBSERVATION_STALE: "账户查询数据过期，请检查QMT连接。",
    OBSERVATION_CROSSED_EVENT: "查询过程中账户状态发生变化，本次核验结果不稳定。",
    ACCOUNT_ORDER_WINDOW_MISMATCH: "资金和委托查询没有形成一致快照。",
  };
  return (
    props.state?.recovery_issues?.map((code) => meanings[code] ?? `核验原因：${code}`).join(" ") ??
    ""
  );
});
</script>

<template>
  <section class="settings-section daily-auto-panel" aria-labelledby="daily-auto-title">
    <h2 id="daily-auto-title">
      {{ state?.mode === "DEBUG_ONCE" ? "单次调试自动买入" : "持续自动买入" }}
    </h2>
    <p v-if="state?.mode !== 'DEBUG_ONCE'">
      本人确认开启一次后，程序运行期间每天自动核验、获取正式优名单并按已保存额度买入，无需每天手动启动。
    </p>
    <p v-if="!state?.supported" class="notice">
      当前连接尚未加载持续自动买入功能；请重新打开主程序加载新版本。
    </p>
    <template v-else>
      <p class="notice" role="status">{{ explanation }}</p>
      <p v-if="state.limits">
        每日最多 {{ state.limits.daily_symbols ?? state.limits.positions }} 只 · 每日
        {{ money(state.limits.daily_value) }} 元 · 单股 {{ money(state.limits.symbol_value) }} 元 ·
        单笔 {{ money(state.limits.order_value) }} 元 · 尾盘持仓最多 {{ state.limits.positions }} 只
        · 账户仓位 {{ state.limits.position_pct }}% · 单日亏损停止线
        {{ money(state.limits.daily_loss) }} 元
      </p>
      <p class="muted">
        原有非系统持仓不占尾盘名额。修改交易设置或主动关闭后，需要重新确认；安全异常会暂停。
      </p>
      <p v-if="state.calendar_through" class="muted">
        执行日历覆盖至 {{ state.calendar_through }}；覆盖不足会暂停，不猜测交易日。
      </p>
      <p v-if="state.recovery_issues?.length" role="alert">{{ recoveryExplanation }}</p>
      <details v-if="state.state === 'PAUSED' || state.state === 'FAULT'">
        <summary>查看原因码</summary>
        <p>{{ state.recovery_issues?.join("、") }} {{ state.pause_reason }}</p>
      </details>
      <div class="actions">
        <button
          v-if="!state.enabled && state.mode !== 'DEBUG_ONCE'"
          class="button primary"
          :disabled="busy || !ready"
          @click="emit('preview')"
        >
          开启持续自动买入
        </button>
        <button class="button danger" :disabled="!canStop" @click="emit('disable')">
          {{ state.mode === "DEBUG_ONCE" ? "停止调试自动买入" : "关闭持续自动买入" }}
        </button>
      </div>
    </template>
    <form
      v-if="preview"
      class="daily-auto-confirm"
      @submit.prevent="emit('confirm', preview.preview_id, phrase)"
    >
      <h3>确认开启每日真实自动买入</h3>
      <p>确认后会持续有效，程序保持运行时每天按以上设置执行真实买入。不会开启自动卖出。</p>
      <p>
        请输入：
        <strong>{{ preview.confirmation_phrase }}</strong>
      </p>
      <input v-model="phrase" aria-label="持续自动买入确认文字" autocomplete="off" />
      <div class="actions">
        <button class="button" type="button" @click="emit('cancel')">取消开启</button>
        <button class="button primary" type="submit" :disabled="!canConfirm">
          确认开启持续自动买入
        </button>
      </div>
    </form>
  </section>
</template>
