import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import ConsoleSettings from "../node/components/ConsoleSettings.vue";
import type { SettingsResponse } from "../node/consoleTypes";
import type { TradingSettingsValues } from "../node/settingsValidation";

const value: SettingsResponse = {
  draft: {
    version: 1,
    saved_at: "2026-10-26T13:00:00+08:00",
    settings: {
      buy: {
        order_method: "ASK1_PROTECTED_LIMIT",
        max_daily_symbols: 1,
        max_daily_buy_value: "10000",
        max_symbol_buy_value: "10000",
        max_order_value: "10000",
        cancel_after_seconds: 10,
        max_retries: 3,
      },
      sell: null,
      safety: { max_daily_loss: "1000", max_positions: 1, emergency_liquidation_enabled: false },
      advanced: {
        max_order_quantity: 10000,
        max_chase_bps: 30,
        max_spread_bps: 20,
        quote_max_age_ms: 1000,
      },
    },
  },
  live_ready: true,
  restart_required: false,
  buy_configured: true,
};

describe("tail buying board selection", () => {
  it("defaults missing legacy and new selections to main board only", () => {
    for (const settings of [value, null]) {
      const wrapper = mount(ConsoleSettings, {
        props: { value: settings, busy: false, connected: true },
      });
      const options = wrapper.findAll<HTMLInputElement>('input[type="checkbox"]');
      expect(options.map((option) => [option.element.value, option.element.checked])).toEqual([
        ["MAIN", true],
        ["CHINEXT", false],
        ["STAR", false],
      ]);
      wrapper.unmount();
    }
  });
  it("saves multiple selected boards and preserves an explicitly empty selection", async () => {
    const wrapper = mount(ConsoleSettings, { props: { value, busy: false, connected: true } });
    await wrapper.get('input[value="CHINEXT"]').setValue(true);
    await wrapper.get('input[value="STAR"]').setValue(true);
    await wrapper.get("form").trigger("submit");
    expect((wrapper.emitted("save")![0]![0] as TradingSettingsValues).buy.allowed_boards).toEqual([
      "MAIN",
      "CHINEXT",
      "STAR",
    ]);
    for (const option of wrapper.findAll('input[type="checkbox"]')) await option.setValue(false);
    await wrapper.get("form").trigger("submit");
    expect((wrapper.emitted("save")![1]![0] as TradingSettingsValues).buy.allowed_boards).toEqual(
      [],
    );
    wrapper.unmount();
    const cleared = structuredClone(value);
    cleared.draft!.settings.buy.allowed_boards = [];
    const reopened = mount(ConsoleSettings, {
      props: { value: cleared, busy: false, connected: true },
    });
    expect(
      reopened
        .findAll<HTMLInputElement>('input[type="checkbox"]')
        .every((option) => !option.element.checked),
    ).toBe(true);
    reopened.unmount();
  });
});
