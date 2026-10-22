import { describe, expect, it } from "vitest";
import { validateSettings } from "../node/settingsValidation";
import type { TradingSettingsValues } from "../node/settingsValidation";

function settings(): TradingSettingsValues {
  return {
    buy: {
      order_method: "ASK1_PROTECTED_LIMIT",
      max_daily_buy_value: "50000",
      max_symbol_buy_value: "10000",
      max_order_value: "5000",
      cancel_after_seconds: 10,
      max_retries: 3,
    },
    sell: { take_profit_pct: "5", stop_loss_pct: "3", sell_by: "14:30" },
    safety: { max_daily_loss: "2000", max_positions: 5, emergency_liquidation_enabled: false },
    advanced: {
      max_order_quantity: 10000,
      max_chase_bps: 30,
      max_spread_bps: 20,
      quote_max_age_ms: 1000,
    },
  };
}

describe("operator trading settings validation", () => {
  it("accepts valid limits and explicitly disabled buying", () => {
    const value = settings();
    expect(validateSettings(value)).toEqual([]);
    value.buy.max_daily_buy_value =
      value.buy.max_symbol_buy_value =
      value.buy.max_order_value =
        "0";
    value.safety.max_daily_loss = "0";
    value.safety.max_positions = 0;
    expect(validateSettings(value)).toEqual([]);
  });
  it("identifies amount relationships and missing safety limits", () => {
    const value = settings();
    value.buy.max_order_value = "20000";
    expect(validateSettings(value).some((i) => i.field === "buy")).toBe(true);
    value.buy.max_order_value = "0";
    expect(validateSettings(value).some((i) => i.message.includes("三个买入金额"))).toBe(true);
    value.buy.max_order_value = "5000";
    value.safety.max_daily_loss = "0";
    expect(validateSettings(value).some((i) => i.field === "safety")).toBe(true);
  });
  it("identifies each invalid field and rejects imprecise money", () => {
    const value = settings();
    value.buy.max_daily_buy_value = "1000.123";
    value.sell.take_profit_pct = "0";
    value.sell.stop_loss_pct = "100";
    value.sell.sell_by = "12:00";
    value.advanced.max_chase_bps = -1;
    value.advanced.quote_max_age_ms = 99;
    const fields = validateSettings(value).map((i) => i.field);
    expect(fields).toEqual(
      expect.arrayContaining([
        "buy.max_daily_buy_value",
        "sell.take_profit_pct",
        "sell.stop_loss_pct",
        "sell.sell_by",
        "advanced.max_chase_bps",
        "advanced.quote_max_age_ms",
      ]),
    );
  });
});
