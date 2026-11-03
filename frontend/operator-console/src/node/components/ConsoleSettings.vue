<script setup lang="ts">
import { ref, watch } from "vue";
import type { SettingsResponse } from "../consoleTypes";
import type { TradingBoard, TradingSettingsValues } from "../settingsValidation";
const props = defineProps<{ value: SettingsResponse | null; busy: boolean; connected: boolean }>();
const emit = defineEmits<{ save: [value: TradingSettingsValues] }>();
const maximum = ref("");
const allowedBoards = ref<TradingBoard[]>(["MAIN"]);
const boardOptions: { value: TradingBoard; label: string }[] = [
  { value: "MAIN", label: "深沪主板" },
  { value: "CHINEXT", label: "创业板" },
  { value: "STAR", label: "科创板" },
];
const perStock = ref("");
const dailyLoss = ref("");
const dailyBudget = ref("600000");
const positionPct = ref("70");
const cancelAfter = ref(5);
const sellCancelAfter = ref(5);
const retries = ref(3);
const maxChase = ref(30);
const spread = ref(20);
const quoteAge = ref(1000);
const maxQuantity = ref(10000);
const maxPositions = ref(15);
watch(
  () => props.value?.draft,
  (value) => {
    const settings = value?.settings;
    if (!settings) return;
    allowedBoards.value = [...(settings.buy.allowed_boards ?? ["MAIN"])];
    maximum.value = String(settings.buy.max_daily_symbols ?? settings.safety.max_positions);
    perStock.value = settings.buy.max_symbol_buy_value;
    dailyLoss.value = settings.safety.max_daily_loss;
    dailyBudget.value = settings.buy.max_daily_buy_value;
    positionPct.value = settings.safety.max_total_position_pct ?? "70";
    cancelAfter.value = settings.buy.cancel_after_seconds ?? 5;
    sellCancelAfter.value = settings.sell_execution?.cancel_after_seconds ?? 5;
    retries.value = settings.buy.max_retries;
    maxChase.value = settings.advanced.max_chase_bps;
    spread.value = settings.advanced.max_spread_bps;
    quoteAge.value = settings.advanced.quote_max_age_ms;
    maxQuantity.value = settings.advanced.max_order_quantity;
    maxPositions.value = settings.safety.max_positions;
  },
  { immediate: true },
);
function submit() {
  const count = Number(maximum.value);
  const cap = String(perStock.value);
  if (!Number.isInteger(count) || count < 1 || count > 15 || !/^\d+(\.\d{1,2})?$/.test(cap)) return;
  emit("save", {
    buy: {
      allowed_boards: [...allowedBoards.value],
      signal_mode: "TAIL_EXCELLENT_3_PHASES",
      order_method: "ASK1_PROTECTED_LIMIT",
      max_daily_symbols: count,
      max_daily_buy_value: String(dailyBudget.value),
      max_symbol_buy_value: cap,
      max_order_value: cap,
      cancel_after_seconds: Number(cancelAfter.value),
      max_retries: Number(retries.value),
    },
    sell: null,
    sell_execution: { cancel_after_seconds: Number(sellCancelAfter.value) },
    safety: {
      max_daily_loss: String(dailyLoss.value),
      max_total_position_pct: String(positionPct.value),
      max_positions: Number(maxPositions.value),
      emergency_liquidation_enabled: false,
    },
    advanced: {
      max_order_quantity: Number(maxQuantity.value),
      max_chase_bps: Number(maxChase.value),
      max_spread_bps: Number(spread.value),
      quote_max_age_ms: Number(quoteAge.value),
    },
  });
}
</script>
<template>
  <section class="settings-section">
    <h2>买入设置</h2>
    <p class="muted">14:40、14:45、14:50识别为“优”后自动处理，同一股票当天只买一次。</p>
    <p v-if="value?.application?.supported" class="muted">
      已保存版本：{{ value.draft?.version ?? "—" }} · 引擎已加载版本：{{
        value.application.active_version ?? "—"
      }}
      ·
      {{
        value.application.reason
          ? "等待应用或读取异常，请核对运行状态"
          : "保存后实时应用，买卖开关保持原状态"
      }}
    </p>
    <form class="settings-form" @submit.prevent="submit">
      <fieldset :disabled="busy || !connected">
        <div class="board-settings" role="group" aria-labelledby="allowed-boards-label">
          <strong id="allowed-boards-label">可交易的板块</strong>
          <div class="board-options">
            <label v-for="board in boardOptions" :key="board.value">
              <input v-model="allowedBoards" type="checkbox" :value="board.value" />
              {{ board.label }}
            </label>
          </div>
          <p class="muted">
            可多选，默认只选深沪主板。未选板块不会自动买入；全部取消则不自动买入任何股票。
          </p>
        </div>
        <div class="form-grid">
          <label>
            每日买入总预算
            <span class="input-unit">
              <input
                v-model="dailyBudget"
                aria-label="每日买入总预算"
                type="number"
                min="0.01"
                max="999999999999"
                step="0.01"
                required
              />
              <span>元</span>
            </span>
            <small>默认60万元，三批共用，包含已成交和在途委托预占。</small>
          </label>
          <label>
            账户总仓位上限
            <span class="input-unit">
              <input
                v-model="positionPct"
                aria-label="账户总仓位上限"
                type="number"
                min="0.01"
                max="100"
                step="0.01"
                required
              />
              <span>%</span>
            </span>
            <small>
              默认70%，已有持仓（含手工持仓）及待成交买单合计不得超过账户总资产的此比例。
            </small>
          </label>
          <label>
            每日最多买入股票数量
            <span class="input-unit">
              <input
                v-model="maximum"
                aria-label="每日最多买入股票数量"
                type="number"
                min="1"
                max="15"
                step="1"
                required
              />
              <span>只</span>
            </span>
            <small>三批共用名额，已买入及委托中的股票都占名额。</small>
          </label>
          <label>
            单只股票买入金额上限
            <span class="input-unit">
              <input
                v-model="perStock"
                aria-label="单只股票买入金额上限"
                type="number"
                min="0.01"
                max="999999999999"
                step="0.01"
                required
              />
              <span>元</span>
            </span>
            <small>包含已成交和在途委托，实际买入金额不会超过此上限。</small>
          </label>
          <label>
            当日账户亏损停止线
            <span class="input-unit">
              <input
                v-model="dailyLoss"
                aria-label="当日账户亏损停止线"
                type="number"
                min="0.01"
                step="0.01"
                required
              />
              <span>元</span>
            </span>
            <small>达到上限后停止新增自动委托。以账户资产变化计算。</small>
          </label>
        </div>
        <div class="form-grid">
          <label>
            买入未成交撤单时效
            <span class="input-unit">
              <input
                v-model="cancelAfter"
                aria-label="买入未成交撤单时效"
                type="number"
                min="1"
                max="300"
                step="1"
                required
              />
              <span>秒</span>
            </span>
            <small>
              默认5秒。自动买单到时申请撤单，确认后按最新卖一价继续买入剩余数量，受重报次数和风控限制。
            </small>
          </label>
          <label>
            卖出未成交撤单时效
            <span class="input-unit">
              <input
                v-model="sellCancelAfter"
                aria-label="卖出未成交撤单时效"
                type="number"
                min="1"
                max="300"
                step="1"
                required
              />
              <span>秒</span>
            </span>
            <small>
              默认5秒。自动卖单到时申请撤单，确认后按最新买一价继续卖出剩余数量，受交易时段和风控限制。手动限价单不自动改价。
            </small>
          </label>
        </div>
        <div class="sell-placeholder">
          <strong>卖出规则</strong>
          <span>参数及板块引擎在卖出配置页分别管理</span>
        </div>
        <p v-if="value?.draft?.settings.sell" class="notice warning">
          当前存在旧卖出规则。保存本页并重启后清除旧规则；主板、创业板引擎单独管理。
        </p>
        <details class="advanced">
          <summary>高级执行设置</summary>
          <p class="muted">账户持仓总数上限包含手工持仓，与每日新买入数量分别限制。</p>
          <div class="form-grid compact">
            <label>
              账户持仓总数上限
              <input
                v-model="maxPositions"
                aria-label="账户持仓总数上限"
                type="number"
                min="1"
                max="15"
                required
              />
            </label>
            <label>
              最多重报次数
              <input v-model="retries" type="number" min="0" max="3" required />
            </label>
            <label>
              最大追价（基点）
              <input v-model="maxChase" type="number" min="0" max="1000" required />
              <small>30基点＝0.3%。</small>
            </label>
            <label>
              最大买卖价差（基点）
              <input v-model="spread" type="number" min="0" max="1000" required />
            </label>
            <label>
              行情有效期（毫秒）
              <input v-model="quoteAge" type="number" min="100" max="5000" required />
            </label>
            <label>
              单笔最大股数
              <input v-model="maxQuantity" type="number" min="100" max="1000000" required />
            </label>
          </div>
        </details>
        <button class="button primary" type="submit">
          {{ busy ? "正在保存…" : "保存买入设置" }}
        </button>
      </fieldset>
    </form>
  </section>
</template>
