import type { FieldIssue } from "./api";

export interface TradingSettingsValues {
  buy: {
    order_method: "ASK1_PROTECTED_LIMIT";
    max_daily_buy_value: string;
    max_symbol_buy_value: string;
    max_order_value: string;
    cancel_after_seconds: number;
    max_retries: number;
  };
  sell: { take_profit_pct: string; stop_loss_pct: string; sell_by: string };
  safety: {
    max_daily_loss: string;
    max_positions: number;
    emergency_liquidation_enabled: boolean;
  };
  advanced: {
    max_order_quantity: number;
    max_chase_bps: number;
    max_spread_bps: number;
    quote_max_age_ms: number;
  };
}

export function validateSettings(value: TradingSettingsValues): FieldIssue[] {
  const issues: FieldIssue[] = [];
  const money = /^(0|[1-9]\d{0,11})(\.\d{1,2})?$/;
  const { buy, sell, safety, advanced } = value;
  for (const [field, amount, label] of [
    ["buy.max_daily_buy_value", buy.max_daily_buy_value, "当日最多买入"],
    ["buy.max_symbol_buy_value", buy.max_symbol_buy_value, "单只股票最多买入"],
    ["buy.max_order_value", buy.max_order_value, "单笔委托最多"],
    ["safety.max_daily_loss", safety.max_daily_loss, "当日最大亏损"],
  ]) {
    if (!money.test(amount!) || !Number.isFinite(Number(amount)))
      issues.push({ field: field!, message: `${label}：请填写非负金额，最多两位小数。` });
  }
  const amounts = [buy.max_daily_buy_value, buy.max_symbol_buy_value, buy.max_order_value].map(
    Number,
  );
  if (amounts.every(Number.isFinite)) {
    if (amounts.some((n) => n === 0) && amounts.some((n) => n > 0))
      issues.push({
        field: "buy",
        message: "三个买入金额必须全部大于0；关闭自动买入时，请将三个金额全部设为0。",
      });
    else if (amounts[2]! > amounts[1]! || amounts[1]! > amounts[0]!)
      issues.push({
        field: "buy",
        message: "买入金额须满足：单笔委托最多 ≤ 单只股票最多买入 ≤ 当日最多买入。",
      });
  }
  for (const [field, raw, label, maximum] of [
    ["sell.take_profit_pct", sell.take_profit_pct, "止盈涨幅", 100],
    ["sell.stop_loss_pct", sell.stop_loss_pct, "止损跌幅", 99.99],
  ] as const) {
    if (!money.test(raw) || Number(raw) <= 0 || Number(raw) > maximum)
      issues.push({ field, message: `${label}：请填写0.01至${maximum}的百分比，最多两位小数。` });
  }
  if (
    !/^\d{2}:\d{2}$/.test(sell.sell_by) ||
    !(
      (sell.sell_by >= "09:30" && sell.sell_by <= "11:25") ||
      (sell.sell_by >= "13:00" && sell.sell_by <= "14:50")
    )
  )
    issues.push({
      field: "sell.sell_by",
      message: "最晚卖出时间：请选择09:30–11:25或13:00–14:50，精确到分钟。",
    });
  for (const [field, number, label, minimum, maximum] of [
    ["buy.cancel_after_seconds", buy.cancel_after_seconds, "未成交等待（秒）", 1, 300],
    ["buy.max_retries", buy.max_retries, "最多重新下单次数", 0, 3],
    ["safety.max_positions", safety.max_positions, "最多持有股票数量", 0, 15],
    ["advanced.max_order_quantity", advanced.max_order_quantity, "单笔最大股数", 100, 1000000],
    ["advanced.max_chase_bps", advanced.max_chase_bps, "最大追价（基点）", 0, 1000],
    ["advanced.max_spread_bps", advanced.max_spread_bps, "最大买卖价差（基点）", 0, 1000],
    ["advanced.quote_max_age_ms", advanced.quote_max_age_ms, "行情有效期（毫秒）", 100, 5000],
  ] as const) {
    if (!Number.isInteger(number) || number < minimum || number > maximum)
      issues.push({ field, message: `${label}：请填写${minimum}至${maximum}的整数。` });
  }
  if (
    Number(buy.max_daily_buy_value) > 0 &&
    (Number(safety.max_daily_loss) <= 0 || safety.max_positions <= 0)
  )
    issues.push({
      field: "safety",
      message: "启用自动买入时，当日最大亏损和最多持有股票数量都必须大于0。",
    });
  return issues;
}
