<script setup lang="ts">
import type { AccountOrder, SellPosition } from "../consoleTypes";
import { money, sellReason, stateText } from "../consoleTypes";
defineProps<{
  position?: SellPosition | undefined;
  symbol: string;
  orders: AccountOrder[];
  ready: boolean;
  cancelDisabled: boolean;
  supported?: boolean;
  blockedReason?: string;
}>();
const emit = defineEmits<{ sell: [position: SellPosition]; cancel: [id: string] }>();
const terminal = new Set(["FILLED", "CANCELLED", "PARTIALLY_FILLED_CANCELLED", "REJECTED"]);
</script>
<template>
  <div class="stock-sale">
    <span v-if="position" class="stock-name">
      剩余 {{ position.remaining }} 股 · 可卖 {{ position.sellable }} 股
      <span v-if="position.sold_today">· 今日已卖 {{ position.sold_today }} 股</span>
    </span>
    <span v-if="position?.sold_today" class="stock-name">
      卖出均价 {{ position.average_sell_price }} 元 · 成交金额 {{ money(position.sell_amount) }} 元
      <span v-if="position.state === 'SOLD'">· 本系统持仓已全部卖出</span>
    </span>
    <button
      class="button"
      :aria-label="`卖出${symbol}`"
      :disabled="!ready || !position?.can_sell"
      @click="position && emit('sell', position)"
    >
      卖出
    </button>
    <span v-if="!ready || !position?.can_sell" class="stock-name">
      {{
        !ready
          ? "当前状态暂不可卖出，请查看页面提示"
          : position
            ? position.disabled_reason === "NOT_READY" && blockedReason
              ? blockedReason
              : sellReason(position.disabled_reason)
            : !ready
              ? "持仓数据待读取"
              : supported === false
                ? "手动卖出待重启主程序加载"
                : "无本系统托管持仓"
      }}
    </span>
    <div
      v-for="order in orders.filter(
        (o) => o.symbol === symbol && o.side === 'SELL' && o.owned_by_system,
      )"
      :key="order.client_order_id"
      class="stock-name"
    >
      卖出 {{ order.quantity }} 股 · 已成交 {{ order.filled }} 股 ·
      {{ order.status === "FILLED" ? "券商回报全部成交" : stateText(order.status) }}
      <button
        v-if="order.cancelable"
        class="text-button"
        :aria-label="`撤销${symbol}卖出委托${order.client_order_id}`"
        :disabled="cancelDisabled || order.cancel_requested"
        @click="emit('cancel', order.client_order_id)"
      >
        撤销卖出委托
      </button>
      <span v-else-if="order.cancel_requested && !terminal.has(order.status)">撤单处理中</span>
      <span v-if="order.cancel_state === 'UNKNOWN'">· 撤单结果待核对</span>
      <span v-if="order.cancel_state === 'FAILED'">· 撤单失败，可核对后重试</span>
    </div>
  </div>
</template>
