<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue";
import { nodeRequest, errorText, NodeApiError } from "../api";
import type { Session } from "../api";

type Board = "MAIN" | "CHINEXT";
type Engine = {
  enabled: boolean;
  authorized: boolean;
  state: string;
  configuration_version: number | null;
  startup_status?: string | null;
  targets: {
    lot_id: string;
    symbol: string;
    target_remaining: number | null;
    reasons: string[];
    waiting_reason?: string | null;
    configuration_version: number;
    rule: { mode: string; sell_at: string; take_profit_pct: string | null };
  }[];
};
type Status = { restored: boolean; boards: Record<Board, Engine> };
const props = defineProps<{ session: Session | null; blocked: boolean }>();
const status = ref<Status | null>(null);
const busy = ref(false);
const error = ref("");
const names = { MAIN: "主板", CHINEXT: "创业板" };
const boards: Board[] = ["MAIN", "CHINEXT"];
const states: Record<string, string> = {
  UNPREPARED: "待准备持仓",
  RESTORING: "正在恢复",
  PERSISTING: "正在保存",
  AUTHORIZING: "正在核验卖出授权",
  READY: "就绪",
  FAILED: "异常暂停",
  BOARD_SELL_RESTORE_FAILED: "恢复失败",
  BOARD_SELL_AUTHORIZATION_FAILED: "授权核验失败",
  BOARD_SELL_CONFIGURATION_UNAVAILABLE: "配置读取异常，已阻断自动卖出",
};
const reasons: Record<string, string> = {
  SELL_BY_TIME: "定时退出",
  TAKE_PROFIT: "止盈",
  REPOST_LIMIT_REACHED: "已达重报上限，需处理",
  SOURCE_EXCLUDED: "该持仓来源已取消勾选，停止新增委托",
};
const startup: Record<string, string> = {
  STARTING: "正在核验并启动",
  STARTED: "已启动自动卖出",
  PAUSED: "已手动暂停",
  BOARD_SELL_NO_ELIGIBLE_LOTS: "暂无可卖持仓",
  BOARD_SELL_GLOBAL_PAUSED: "全局停止中，请先核对并恢复账户",
  BOARD_SELL_START_TIMEOUT: "启动等待超时，请检查状态后重试",
  BOARD_SELL_OUTSIDE_SESSION: "当前不在允许启动的交易时段",
  BOARD_SELL_CONFIGURATION_REQUIRED: "请先保存卖出配置",
  BOARD_SELL_BUY_SESSION_ACTIVE: "当前买入会话正在运行，卖出尚未启动",
  BOARD_SELL_CONFIGURATION_UNAVAILABLE: "配置读取异常，已阻断自动卖出",
  BOARD_SELL_AUTHORIZATION_FAILED: "卖出授权核验失败，请检查交易诊断后重试",
  BOARD_SELL_ENGINE_FAILED: "卖出引擎异常，已暂停",
  BOARD_SELL_RESTORE_FAILED: "持仓恢复失败，请检查交易诊断",
  OPERATOR_SESSION_EXPIRED: "登录已失效，请重新登录",
  OPERATOR_SESSION_REQUIRED: "登录已退出，请重新登录",
};
const errors: Record<string, string> = {
  BOARD_SELL_CONFIGURATION_REQUIRED: "请先保存卖出配置。",
  BOARD_SELL_NOT_READY: "引擎正在准备，状态将自动刷新。",
  BOARD_SELL_NO_ELIGIBLE_LOTS: "当前没有符合板块、来源和可卖日期的托管持仓。",
  BOARD_SELL_LEGACY_CONFLICT: "旧卖出规则仍在使用，请先完成规则切换。",
  BOARD_SELL_OUTSIDE_SESSION: "当前不在允许开启卖出引擎的交易时段。",
  BOARD_SELL_BUY_SESSION_ACTIVE: "当前买入会话正在运行，请暂停并完成核验后再开启卖出引擎。",
};
onBeforeUnmount(() => {
  ++epoch;
  if (refreshTimer) clearInterval(refreshTimer);
});
let epoch = 0;
let refreshTimer: ReturnType<typeof setInterval> | undefined;
async function request(action?: string, payload?: object) {
  if (!props.session || busy.value || (action && props.blocked)) return;
  const current = epoch;
  busy.value = true;
  error.value = "";
  try {
    const result = await nodeRequest<Status>(
      action ? `sell-engines/${action}` : "sell-engines",
      action ? "POST" : "GET",
      payload,
      props.session.csrf_token,
    );
    if (current !== epoch) return;
    status.value = result;
  } catch (reason) {
    if (current === epoch)
      error.value =
        reason instanceof NodeApiError
          ? (errors[reason.code] ?? errorText(reason))
          : errorText(reason);
  } finally {
    if (current === epoch) busy.value = false;
  }
}
watch(
  () => props.session?.csrf_token,
  () => {
    ++epoch;
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = undefined;
    status.value = null;
    error.value = "";
    busy.value = false;
    if (props.session) {
      void request();
      refreshTimer = setInterval(() => {
        if (!busy.value) void request();
      }, 2000);
    }
  },
  { immediate: true },
);
</script>

<template>
  <section aria-label="独立卖出引擎">
    <h3>主板与创业板独立卖出引擎</h3>
    <p>
      登录后按已保存配置自动核验持仓并启动两套卖出引擎，无需预览或输入短语。重启后需重新登录；手动暂停后刷新不会重新启动，全局停止仍然有效。
    </p>
    <button type="button" :disabled="busy || !session" @click="request()">刷新状态</button>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="status?.boards">
      <div v-for="board in boards" :key="board">
        <h4>{{ names[board] }}</h4>
        <p>
          {{ status.boards[board].enabled ? "已开启" : "已暂停" }} ·
          {{ status.boards[board].authorized ? "卖出授权有效" : "等待卖出授权" }} ·
          {{ states[status.boards[board].state] ?? "异常暂停，请检查交易诊断" }} · 配置版本
          {{ status.boards[board].configuration_version ?? "待绑定" }}
        </p>
        <p v-if="status.boards[board].startup_status">
          {{ startup[status.boards[board].startup_status!] ?? "启动未完成，请检查交易诊断" }}
        </p>
        <button
          v-if="!status.boards[board].enabled && status.boards[board].startup_status !== 'STARTING'"
          type="button"
          :disabled="busy || blocked"
          @click="request('start', { board })"
        >
          启动{{ names[board] }}自动卖出
        </button>
        <button
          v-else
          type="button"
          :disabled="busy || blocked"
          @click="request('pause', { board })"
        >
          暂停{{ names[board] }}
        </button>
        <ul>
          <li v-for="lot in status.boards[board].targets" :key="lot.lot_id">
            {{ lot.symbol }} · 配置v{{ lot.configuration_version }} · {{ lot.rule.sell_at }}定时退出
            <template v-if="lot.rule.mode === 'TAKE_PROFIT_OR_TIME'">
              / 止盈{{ lot.rule.take_profit_pct }}%
            </template>
            · 目标剩余 {{ lot.target_remaining ?? "待判断" }}股 ·
            {{ lot.reasons.map((reason) => reasons[reason] ?? "扩展规则触发").join("、") }}
            <span v-if="lot.waiting_reason">· {{ reasons[lot.waiting_reason] ?? "等待处理" }}</span>
          </li>
        </ul>
      </div>
    </template>
  </section>
</template>
