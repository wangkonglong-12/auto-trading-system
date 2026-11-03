import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import DailyAutoPanel from "../node/components/DailyAutoPanel.vue";
import type { DailyAutoPreview, DailyAutoState } from "../node/api";

const now = Date.parse("2026-10-26T13:00:00+08:00");
const state: DailyAutoState = {
  supported: true,
  enabled: false,
  state: "DISABLED",
  reason: "NOT_ENABLED",
  settings_hash: "sha256:" + "a".repeat(64),
  calendar_through: "2026-12-31",
  limits: {
    daily_value: "10000",
    symbol_value: "10000",
    order_value: "10000",
    daily_symbols: 1,
    positions: 1,
    position_pct: "100",
    daily_loss: "1000",
  },
};
const preview: DailyAutoPreview = {
  preview_id: "daily-auto-example",
  confirmation_phrase: "ENABLE DAILY AUTO BUY 12345678",
  expires_at: new Date(now + 30000).toISOString(),
  settings_hash: state.settings_hash!,
  state,
  buy_only: true,
};
const props = { state, preview: null, busy: false, ready: true, canStop: true, now };
describe("recurring automatic buy settings", () => {
  it("shows daily limits without enabling or requesting a daily account confirmation", () => {
    const wrapper = mount(DailyAutoPanel, { props });
    expect(wrapper.text()).toContain("每日最多 1 只");
    expect(wrapper.text()).toContain("10,000.00");
    expect(wrapper.text()).toContain("无需每天手动启动");
    expect(wrapper.emitted()).toEqual({});
    wrapper.unmount();
  });
  it("requests an exact preview before any persistent enable", async () => {
    const wrapper = mount(DailyAutoPanel, { props });
    await wrapper.get("button").trigger("click");
    expect(wrapper.emitted("preview")).toHaveLength(1);
    expect(wrapper.emitted("confirm")).toBeUndefined();
    wrapper.unmount();
  });
  it("requires the exact live enable phrase and rejects an expired preview", async () => {
    const wrapper = mount(DailyAutoPanel, { props: { ...props, preview } });
    const submit = wrapper.get('button[type="submit"]');
    expect(submit.attributes("disabled")).toBeDefined();
    await wrapper.get("input").setValue("wrong");
    expect(submit.attributes("disabled")).toBeDefined();
    await wrapper.get("input").setValue(preview.confirmation_phrase);
    expect(submit.attributes("disabled")).toBeUndefined();
    await wrapper.get("form").trigger("submit");
    expect(wrapper.emitted("confirm")?.[0]).toEqual([
      preview.preview_id,
      preview.confirmation_phrase,
    ]);
    await wrapper.setProps({ now: now + 31000 });
    expect(submit.attributes("disabled")).toBeDefined();
    wrapper.unmount();
  });
  it("cancels without enabling", async () => {
    const wrapper = mount(DailyAutoPanel, { props: { ...props, preview } });
    await wrapper.get('button[type="button"]').trigger("click");
    expect(wrapper.emitted("cancel")).toHaveLength(1);
    expect(wrapper.emitted("confirm")).toBeUndefined();
    wrapper.unmount();
  });
  it("keeps stop available during a pending request or stale status", async () => {
    const wrapper = mount(DailyAutoPanel, {
      props: {
        ...props,
        busy: true,
        ready: false,
        state,
      },
    });
    const stop = wrapper.findAll("button").find((button) => button.text() === "关闭持续自动买入")!;
    expect(stop.attributes("disabled")).toBeUndefined();
    await stop.trigger("click");
    expect(wrapper.emitted("disable")).toHaveLength(1);
    wrapper.unmount();
  });
  it("shows the actual blocked recovery codes", () => {
    const wrapper = mount(DailyAutoPanel, {
      props: {
        ...props,
        state: {
          ...state,
          state: "PAUSED",
          reason: "RECOVERY_BLOCKED",
          recovery_issues: ["UNOWNED_BROKER_ORDER"],
        },
      },
    });
    expect(wrapper.text()).toContain("UNOWNED_BROKER_ORDER");
    expect(wrapper.text()).toContain("已暂停");
    wrapper.unmount();
  });
});
