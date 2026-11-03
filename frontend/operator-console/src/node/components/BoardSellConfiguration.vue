<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import BoardSellEngines from "./BoardSellEngines.vue";
import { NIcon } from "naive-ui";
import { InformationCircleOutline } from "@vicons/ionicons5";
import { nodeRequest, errorText, NodeApiError } from "../api";
import type { Session } from "../api";

type Rule = {
  mode: "TIME_ONLY" | "TAKE_PROFIT_OR_TIME";
  take_profit_pct: string | null;
  sell_at: string;
};
type Values = {
  main: Rule;
  chinext: Rule;
  include_automatic: boolean;
  include_system_manual: boolean;
  cancel_after_seconds: number;
  max_reposts?: number;
};
type Reply = {
  draft: { version: number; saved_at: string; settings: Values } | null;
  defaults: Values;
  save_enables_execution: boolean;
  execution_supported: boolean;
};
const props = defineProps<{ session: Session | null; blocked: boolean }>();
const emit = defineEmits<{ expired: [] }>();
const value = ref<Values | null>(null);
const saved = ref<Values | null>(null);
const version = ref(0);
const loading = ref(false);
const saving = ref(false);
const ready = ref(false);
const error = ref("");
const message = ref("");
const keys = ["main", "chinext"] as const;
const titles = { main: "主板", chinext: "创业板" };
let epoch = 0;
let controller: AbortController | undefined;
const clone = (v: Values): Values => JSON.parse(JSON.stringify(v)) as Values;
const issues = computed(() => {
  const v = value.value;
  if (!v) return [];
  const result: string[] = [];
  for (const k of keys) {
    const r = v[k];
    if (
      r.mode === "TAKE_PROFIT_OR_TIME" &&
      (!/^(0|[1-9][0-9]{0,2})(\.[0-9]{1,2})?$/.test(r.take_profit_pct ?? "") ||
        Number(r.take_profit_pct) <= 0 ||
        Number(r.take_profit_pct) > 100)
    )
      result.push(`${titles[k]}止盈比例须大于0且不超过100%，最多两位小数。`);
    if (
      !/^\d{2}:\d{2}$/.test(r.sell_at) ||
      !(
        (r.sell_at >= "09:30" && r.sell_at <= "11:25") ||
        (r.sell_at >= "13:00" && r.sell_at <= "14:50")
      )
    )
      result.push(`${titles[k]}退出时间须为09:30至11:25或13:00至14:50。`);
  }
  if (
    !Number.isInteger(v.cancel_after_seconds) ||
    v.cancel_after_seconds < 1 ||
    v.cancel_after_seconds > 300
  )
    result.push("撤单时效须为1至300的整数秒。");
  if (
    v.max_reposts !== undefined &&
    (!Number.isInteger(v.max_reposts) || v.max_reposts < 0 || v.max_reposts > 100)
  )
    result.push("最多重报次数须为0至100的整数。0表示不重报。");
  return result;
});
const dirty = computed(
  () => value.value && JSON.stringify(value.value) !== JSON.stringify(saved.value),
);
function failure(reason: unknown) {
  const code = reason instanceof NodeApiError ? reason.code : "";
  if (["OPERATOR_SESSION_REQUIRED", "OPERATOR_SESSION_EXPIRED"].includes(code)) emit("expired");
  const messages: Record<string, string> = {
    SELL_CONFIGURATION_VERSION_CONFLICT: "配置已在其他页面修改。请先重新读取，再核对后保存。",
    SELL_CONFIGURATION_STORE_INVALID:
      "卖出配置文件无法读取，请保留记录排查。原交易配置不受本次操作影响。",
    SELL_CONFIGURATION_SAVE_FAILED: "卖出配置保存失败，请检查本机存储后重试。",
  };
  error.value =
    code in messages
      ? messages[code]!
      : code === "REQUEST_FAILED"
        ? "卖出配置接口尚未加载，请重新打开主程序后读取。"
        : errorText(reason);
}
function apply(reply: Reply) {
  value.value = clone(reply.draft?.settings ?? reply.defaults);
  saved.value = clone(value.value);
  version.value = reply.draft?.version ?? 0;
  ready.value = true;
}
async function load() {
  const token = props.session?.csrf_token;
  if (!token) return;
  const current = ++epoch;
  controller?.abort();
  controller = new AbortController();
  loading.value = true;
  ready.value = false;
  error.value = message.value = "";
  try {
    const result = await nodeRequest<Reply>(
      "sell-configuration",
      "GET",
      undefined,
      token,
      controller.signal,
    );
    if (current === epoch) apply(result);
  } catch (reason) {
    if (current === epoch) failure(reason);
  } finally {
    if (current === epoch) loading.value = false;
  }
}
async function save() {
  if (
    !ready.value ||
    !value.value ||
    !props.session ||
    props.blocked ||
    saving.value ||
    issues.value.length
  )
    return;
  const current = epoch;
  const payload = clone(value.value);
  for (const k of keys) if (payload[k].mode === "TIME_ONLY") payload[k].take_profit_pct = null;
  saving.value = true;
  error.value = message.value = "";
  try {
    const result = await nodeRequest<Reply>(
      "sell-configuration",
      "POST",
      { expected_version: version.value, settings: payload },
      props.session.csrf_token,
      controller?.signal,
    );
    if (current === epoch) {
      apply(result);
      message.value =
        "卖出配置已保存，将在引擎下一轮检查时应用到已准备持仓，无需重启。请核对下方配置版本；已触发的退出目标保留，暂停的引擎不会自动开启。";
    }
  } catch (reason) {
    if (current === epoch) {
      failure(reason);
      // A lost reply or conflicting version requires explicit read-back, never retry blindly.
      ready.value = false;
    }
  } finally {
    if (current === epoch) saving.value = false;
  }
}
watch(
  () => props.session?.csrf_token,
  () => {
    ++epoch;
    controller?.abort();
    value.value = saved.value = null;
    ready.value = false;
    saving.value = loading.value = false;
    error.value = message.value = "";
    if (props.session) void load();
  },
  { immediate: true },
);
onBeforeUnmount(() => {
  ++epoch;
  controller?.abort();
});
</script>

<template>
  <section class="sell-config" aria-labelledby="sell-config-title">
    <div class="sell-heading">
      <div>
        <h2 id="sell-config-title">自动卖出配置</h2>
        <p class="muted">按板块设定退出条件，规则独立保存。</p>
      </div>
      <span class="live-label">QMT实盘</span>
    </div>
    <div class="sell-status">
      <strong>配置保存与引擎开启分别操作</strong>
      <span>下方分别管理主板、创业板引擎；保存参数不会自动开启。</span>
    </div>
    <p v-if="loading" role="status">正在读取卖出配置…</p>
    <div v-if="error" class="notice warning" role="alert">
      {{ error }}
      <button class="text-button" :disabled="loading || saving || !session" @click="load">
        重新读取卖出配置
      </button>
    </div>
    <p v-if="message" class="notice" role="status">{{ message }}</p>
    <form v-if="value" @submit.prevent="save">
      <fieldset :disabled="!ready || loading || saving || blocked || !session">
        <h3>卖出规则</h3>
        <p class="muted">买入后的下一交易日，达到止盈条件或到达退出时间后开始委托。</p>
        <div class="rule-scroll">
          <table class="rule-table">
            <thead>
              <tr>
                <th>适用板块</th>
                <th>退出方式</th>
                <th>止盈幅度</th>
                <th>定时退出</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="key in keys" :key="key">
                <td>
                  <strong>{{ titles[key] }}</strong>
                  <small>{{ key === "main" ? "10cm" : "20cm" }}</small>
                </td>
                <td>
                  <select v-model="value[key].mode" :aria-label="`${titles[key]}退出方式`">
                    <option value="TIME_ONLY">定时退出</option>
                    <option value="TAKE_PROFIT_OR_TIME">止盈＋定时退出</option>
                  </select>
                </td>
                <td>
                  <span class="rule-unit">
                    <input
                      v-model="value[key].take_profit_pct"
                      :aria-label="`${titles[key]}止盈比例`"
                      :disabled="value[key].mode === 'TIME_ONLY'"
                      :placeholder="value[key].mode === 'TIME_ONLY' ? '不使用止盈' : '填写比例'"
                      inputmode="decimal"
                      :required="value[key].mode !== 'TIME_ONLY'"
                    />
                    <span>%</span>
                  </span>
                </td>
                <td>
                  <input
                    v-model="value[key].sell_at"
                    :aria-label="`${titles[key]}退出时间`"
                    type="time"
                    min="09:30"
                    max="14:50"
                    required
                  />
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="rule-hint">
          止盈基准：该买入批次的实际成交均价；触发后卖出全部剩余可卖数量。两类板块的比例独立设置。
        </p>
        <div class="rule-explanation">
          <NIcon :component="InformationCircleOutline" />
          <div>
            <p v-for="key in keys" :key="key">
              <strong>{{ titles[key] }}：</strong>
              <template v-if="value[key].mode === 'TAKE_PROFIT_OR_TIME'">
                达到{{
                  value[key].take_profit_pct ? `${value[key].take_profit_pct}%` : "所设止盈比例"
                }}即开始卖出，否则
              </template>
              下一交易日{{ value[key].sell_at }}开始卖出。
            </p>
          </div>
        </div>
        <h3>持仓范围与生效方式</h3>
        <div class="scope-options">
          <label>
            <input v-model="value.include_automatic" type="checkbox" />
            系统自动买入
          </label>
          <label>
            <input v-model="value.include_system_manual" type="checkbox" />
            本系统手动买入
          </label>
        </div>
        <p class="rule-hint">
          仅处理明确归属本系统的托管持仓。不勾选来源表示该类持仓不纳入这套规则。
        </p>
        <div class="effective-scope">
          <strong>计划生效范围</strong>
          <div>
            经预览确认的托管持仓
            <span>
              保存后实时更新已准备持仓的参数和来源范围；新增批次仍需准备持仓。已触发的退出目标及已有委托不会被清除。
            </span>
          </div>
        </div>
        <details open>
          <summary>
            委托执行
            <small>高级设置</small>
          </summary>
          <div class="sell-execution">
            <label>
              卖出未成交撤单时效
              <span class="rule-unit">
                <input
                  v-model.number="value.cancel_after_seconds"
                  aria-label="卖出配置撤单时效"
                  type="number"
                  min="1"
                  max="300"
                  step="1"
                  required
                />
                <span>秒</span>
              </span>
            </label>
            <label>
              最多重报次数
              <input
                v-model.number="value.max_reposts"
                aria-label="卖出最多重报次数"
                type="number"
                min="0"
                max="100"
                step="1"
              />
            </label>
            <span>委托方式：买一价限价委托</span>
            <p class="rule-hint">
              确认撤单后，按最新买一价重新委托未成交部分。保存本页不会开启引擎。
            </p>
          </div>
        </details>
        <ul v-if="issues.length" class="validation" role="alert">
          <li v-for="issue in issues" :key="issue">{{ issue }}</li>
        </ul>
        <footer>
          <span>仅保存配置，不开启自动卖出。</span>
          <div>
            <button
              class="button"
              type="button"
              :disabled="!dirty"
              @click="
                value = saved && clone(saved);
                error = message = '';
              "
            >
              取消修改
            </button>
            <button class="button primary" type="submit" :disabled="!!issues.length">
              {{ saving ? "正在保存…" : "保存规则" }}
            </button>
          </div>
        </footer>
      </fieldset>
    </form>
    <BoardSellEngines :session="session" :blocked="blocked" />
  </section>
</template>

<style scoped>
.sell-config {
  color: #142138;
  padding: 24px 0;
}
.sell-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
}
.sell-heading h2 {
  font-size: 30px;
  margin: 0 0 8px;
}
.live-label {
  color: #1671ed;
  border: 1px solid #b8d3fa;
  border-radius: 6px;
  padding: 6px 14px;
  white-space: nowrap;
}
.sell-status {
  display: flex;
  align-items: center;
  gap: 20px;
  background: #f2f7fd;
  padding: 20px;
  margin: 24px 0;
  border-radius: 6px;
  flex-wrap: wrap;
}
.sell-status span {
  color: #67748a;
  font-size: 15px;
}
fieldset {
  border: 0;
  margin: 0;
  padding: 0;
  min-width: 0;
}
h3 {
  font-size: 22px;
  margin: 28px 0 12px;
}
p.muted,
.rule-hint {
  font-size: 15px;
  color: #67748a;
}
.rule-scroll {
  overflow-x: auto;
  margin-top: 18px;
}
.rule-table {
  width: 100%;
  border-collapse: collapse;
  min-width: 700px;
  text-align: left;
}
.rule-table th {
  background: #edf4fd;
  color: #58677e;
  font-weight: 500;
  padding: 14px 20px;
}
.rule-table td {
  padding: 18px 20px;
  border-bottom: 1px solid #e4ebf4;
}
.rule-table td:first-child {
  white-space: nowrap;
}
.rule-table small {
  margin-left: 16px;
}
.rule-table input,
.rule-table select,
.rule-unit input {
  box-sizing: border-box;
  height: 44px;
  font: inherit;
  width: 100%;
  min-width: 0;
  border: 1px solid #ced8e8;
  background: white;
  border-radius: 5px;
  padding: 8px 12px;
  color: inherit;
}
.rule-table input:disabled {
  background: #f1f3f6;
}
.rule-unit {
  display: flex;
  align-items: center;
  max-width: 180px;
  gap: 10px;
}
.rule-hint {
  margin: 10px 0;
  line-height: 1.7;
}
.rule-explanation {
  display: flex;
  gap: 14px;
  background: #f2f7fd;
  padding: 18px 22px;
  margin: 18px 0 26px;
  border-radius: 6px;
  font-size: 16px;
  color: #4d617f;
}
.rule-explanation .n-icon {
  color: #1671ed;
  margin-top: 3px;
}
.scope-options {
  display: flex;
  flex-wrap: wrap;
  gap: 30px;
  padding: 10px 0;
}
.scope-options label {
  display: flex;
  align-items: center;
  gap: 10px;
}
input[type="checkbox"] {
  width: 20px;
  height: 20px;
  accent-color: #1671ed;
}
.effective-scope {
  display: flex;
  align-items: center;
  gap: 25px;
  background: #f7faff;
  padding: 18px 22px;
  margin: 16px 0 26px;
  border: 1px solid #e0eafb;
  border-radius: 5px;
}
.effective-scope span {
  display: block;
  color: #67748a;
  font-size: 14px;
  margin-top: 5px;
}
summary {
  cursor: pointer;
  font-size: 22px;
  font-weight: 600;
  margin: 12px 0;
}
summary small {
  font-weight: 400;
  margin-left: 14px;
}
.sell-execution {
  display: flex;
  gap: 20px 45px;
  flex-wrap: wrap;
  align-items: center;
  background: #f7faff;
  padding: 18px 22px;
  border-radius: 5px;
}
.sell-execution label {
  display: flex;
  gap: 20px;
  align-items: center;
}
.sell-execution .rule-unit {
  width: 135px;
}
.sell-execution p {
  flex-basis: 100%;
  margin: 0;
}
.validation {
  color: #b42318;
  font-size: 15px;
}
footer {
  position: sticky;
  bottom: 0;
  background: white;
  padding: 18px 0;
  margin-top: 22px;
  border-top: 1px solid #e5ebf3;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
}
footer > span {
  color: #67748a;
  font-size: 14px;
}
footer > div {
  display: flex;
  gap: 12px;
}
@media (max-width: 900px) {
  .effective-scope,
  footer {
    align-items: flex-start;
    flex-direction: column;
  }
  .sell-status {
    gap: 12px;
  }
}
</style>
