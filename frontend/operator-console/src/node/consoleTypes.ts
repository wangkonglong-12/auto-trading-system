import type { TradingSettingsValues } from "./settingsValidation";

export interface AccountPosition {
  symbol: string;
  quantity: number;
  sellable: number;
  frozen: number | null;
  market_value?: string | null;
}
export interface AccountOrder {
  client_order_id: string;
  symbol: string;
  side: string;
  status: string;
  quantity: number;
  filled: number;
  remaining: number;
  limit_price: string;
  owned_by_system: boolean;
  cancelable: boolean;
  cancel_requested?: boolean;
  cancel_state?: "NONE" | "PENDING" | "UNKNOWN" | "FAILED" | "CONFIRMED";
  cancel_reason?: string | null;
  reason?: string;
}
export interface AccountTrade {
  trade_id: string;
  symbol: string;
  side: string;
  quantity: number;
  price: string;
  amount?: string;
  traded_at: string;
  owned_by_system: boolean;
}
export interface AccountDetails {
  available: boolean;
  coherent?: boolean;
  observed_at?: string;
  cash?: string;
  total_equity?: string | null;
  market_value?: string | null;
  positions: AccountPosition[];
  orders: AccountOrder[];
  trades: AccountTrade[];
  names?: Record<string, string>;
  truncated?: boolean;
  managed_lots?: {
    lot_id: string;
    symbol: string;
    quantity: number;
    cost_price: string | null;
    acquired_on: string;
    eligible_sell_date: string;
    sell_state: string;
  }[];
  sell_positions?: SellPosition[];
  sell_submissions?: {
    client_order_id: string;
    symbol: string;
    name: string;
    operation_id: string;
    quantity: number;
    limit_price: string;
    status: string;
  }[];
}
export interface SellPosition {
  symbol: string;
  name: string | null;
  remaining: number;
  sellable: number;
  reserved: number;
  sold_today: number;
  sell_amount: string;
  average_sell_price: string | null;
  eligible_sell_date: string;
  can_sell: boolean;
  disabled_reason: string | null;
  state: string;
}
export function sellReason(value: string | null | undefined): string {
  return (
    (
      {
        NO_POSITION: "无剩余持仓",
        T_PLUS_ONE: "今日买入，尚未到可卖日期",
        RESULT_UNKNOWN: "持仓归属待核对",
        ORDER_CONFLICT: "已有未完成委托，请先处理",
        BUSY: "卖出处理中",
        NO_SELLABLE: "当前无可卖数量",
        NOT_READY: "账户尚未准备好",
        OUTSIDE_WINDOW: "当前不在卖出交易时段",
      } as Record<string, string>
    )[value ?? ""] ?? "持仓待读取"
  );
}
export interface BuyRow {
  symbol: string;
  name: string | null;
  state: string;
  signal_state: string | null;
  quantity: number | null;
  bought_at: string | null;
  average_price: string | null;
  amount: string | null;
  source?: "MANUAL" | "AUTO" | "DEBUG";
  trade_date?: string | null;
  sell_state?: string | null;
  is_snapshot?: boolean;
  observed_at?: string;
  submitted_at?: string | null;
  expires_at?: string | null;
}
export interface Dashboard {
  process?: TradingProcess;
  account: AccountDetails;
  buys: {
    has_manual?: boolean;
    today_summary?: {
      available: boolean;
      completed: number | null;
      partial: number | null;
      pending: number | null;
      not_bought: number | null;
      occupied: number | null;
      remaining: number | null;
      maximum: number | null;
    };
    selection_date?: string | null;
    starts_at?: string | null;
    expires_at?: string | null;
    display_state?: string;
    decision_phases?: string[];
    snapshot_saved?: boolean;
    calendar_pending?: boolean;
    available: boolean;
    observed_at: string | null;
    rows: BuyRow[];
    completed: number | null;
    partial: number | null;
    pending: number | null;
    not_bought: number | null;
    occupied: number | null;
    remaining: number | null;
    maximum: number | null;
  };
}
export interface TradingProcess {
  day: string;
  stream_id: string;
  warning: string | null;
  retained: number;
  maximum: number;
  stocks: { symbol: string; name: string }[];
  events: {
    seq: number;
    occurred_at: string;
    level: "INFO" | "WAIT" | "ERROR" | "SUCCESS";
    symbol: string | null;
    name: string;
    message: string;
  }[];
}
export interface SettingsResponse {
  application?: {
    supported: boolean;
    active_version: number | null;
    reason: string | null;
    state: string;
  } | null;
  draft: { version: number; saved_at: string; settings: TradingSettingsValues } | null;
  live_ready: boolean;
  restart_required: boolean;
  buy_configured: boolean;
}
export type ConsoleView =
  "today" | "positions" | "history" | "trading-settings" | "system-settings";

export function money(value: string | null | undefined): string {
  if (value == null || value === "") return "—";
  const parts = value.split(".");
  return `${parts[0]!.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}.${(parts[1] ?? "").padEnd(2, "0")}`;
}
export function timeText(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "—"
    : date.toLocaleTimeString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
}
export function stateText(value: string | null | undefined): string {
  const labels: Record<string, string> = {
    FILLED: "已完成买入",
    PARTIAL: "部分成交",
    PENDING: "委托中",
    NOT_BOUGHT: "未买入",
    UNAVAILABLE: "待核对",
    SUBMITTING: "提交中",
    ACKED: "已报",
    PARTIALLY_FILLED: "部分成交",
    CANCEL_PENDING: "撤单中",
    CANCELLED: "已撤单",
    PARTIALLY_FILLED_CANCELLED: "部成部撤",
    PARTIAL_FILL_CANCEL_PENDING: "部分成交，撤单中",
    REJECTED: "已拒绝",
    UNKNOWN: "结果待核对",
    WAITING_MARKET: "等待合格行情",
    WAITING_WINDOW: "等待买入时间",
    POSITION_CAPACITY: "名额已满",
    SKIPPED_MIN_ORDER: "金额不足最小申报数量",
    MISSED_ENTRY_WINDOW: "已过买入时间",
    FINISHED: "本批执行结束",
    DUPLICATE_SIGNAL: "重复入选，沿用原买入目标",
    READY: "等待执行",
    WORKING: "执行中",
    COMPLETE: "执行结束",
    RULES_NOT_CONFIGURED: "待配置卖出规则",
    SELL_UNVERIFIED: "卖出状态待核对",
    SELL_NOT_BOUND: "待绑定卖出规则",
    SELL_BOUND_PAUSED: "已绑定规则，尚未开启或已暂停",
    SELL_MONITORING: "自动卖出监控中",
    SELL_TRIGGERED: "已触发卖出条件",
    SELL_BLOCKED: "卖出受阻，请查看引擎状态",
    SELL_MIXED: "各持仓批次卖出状态不同",
    MANUAL_BUY_MANAGED: "已由手动买入接管",
  };
  return value ? (labels[value] ?? "等待执行") : "等待信号";
}
