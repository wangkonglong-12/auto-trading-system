import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  LocalConsoleApiError,
  type LocalConsoleGateway,
  type LocalControlReceipt,
} from "../services/localConsoleApi";
import { controlDefinition } from "../models/controls";
import ProtectedControlDialog from "./ProtectedControlDialog.vue";

const preview = {
  preview_id: `preview_${"1".repeat(32)}`,
  actor_id: "operator_demo",
  node_id: "node-dev-01",
  account_id: "acct_demo_001",
  account_alias: "demo",
  profile: "PAPER",
  action: "EMERGENCY_HALT",
  reason_code: "LOCAL_EMERGENCY_HALT",
  requested_at: "2026-09-04T10:00:00+08:00",
  expires_at: "2026-09-04T10:01:30+08:00",
  expected_state: "AUTO",
  requested_state: "HALT",
  cancel_active_orders: true,
  impact_hash: `sha256:${"a".repeat(64)}`,
  confirmation_phrase: "确认紧急停止",
  warnings: ["当前有2笔活动委托。", "系统会请求撤销活动委托。"],
  facts: {
    active_orders: 2,
    active_buy_orders: 1,
    active_sell_orders: 1,
    open_positions: 3,
    qmt_connected: true,
    central_connected: true,
    reconciliation_complete: true,
    unknown_order: false,
  },
} as const;

const confirmed = {
  authorization_token: "authorization-token-value-123456789",
  preview_id: preview.preview_id,
  command_template: {
    action: "EMERGENCY_HALT",
    actor_id: "operator_demo",
    reason_code: "LOCAL_EMERGENCY_HALT",
    requested_at: preview.requested_at,
    expected_state: "AUTO",
    confirmation: {
      confirmation_id: `confirm_${"2".repeat(32)}`,
      actor_id: "operator_demo",
      confirmed_at: "2026-09-04T10:00:01+08:00",
      impact_hash: preview.impact_hash,
    },
    cancel_active_orders: true,
  },
} as const;

const accepted = {
  command_id: "cmd_12345678",
  action: "EMERGENCY_HALT",
  actor_id: "operator_demo",
  status: "ACCEPTED",
  final: false,
  current_state: null,
  rejection_code: null,
  blockers: [],
  error_code: null,
  retryable: false,
  requires_reconciliation: false,
  status_url: "/api/v1/local/controls/cmd_12345678",
  poll_after_ms: 1,
} as const;

const applied = { ...accepted, status: "APPLIED", final: true, current_state: "HALT" } as const;

function gateway(initialReceipt: LocalControlReceipt = applied): LocalConsoleGateway {
  return {
    getSession: vi.fn(async () => null),
    login: vi.fn(),
    logout: vi.fn(),
    createControlPreview: vi.fn(async () => preview),
    confirmControlPreview: vi.fn(async () => confirmed),
    submitConfirmedControl: vi.fn(async () => initialReceipt),
    getControlReceipt: vi.fn(async () => applied),
  };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("ProtectedControlDialog", () => {
  it("shows live impact and requires the exact server phrase before execution", async () => {
    const api = gateway();
    const wrapper = mount(ProtectedControlDialog, {
      props: { show: true, gateway: api, control: controlDefinition("EMERGENCY_HALT") },
      global: { stubs: { teleport: true } },
    });
    await flushPromises();

    expect(wrapper.text()).toContain("当前状态");
    expect(wrapper.text()).toContain("AUTO");
    expect(wrapper.text()).toContain("当前有2笔活动委托");
    const execute = wrapper
      .findAll("button")
      .find((button) => button.text().includes("确认并执行紧急停止"));
    expect(execute?.attributes()).toHaveProperty("disabled");

    await wrapper.get('input[placeholder="输入上方完整口令"]').setValue("确认紧急停止");
    await execute?.trigger("click");
    await flushPromises();

    expect(api.confirmControlPreview).toHaveBeenCalledWith(preview.preview_id, "确认紧急停止");
    expect(api.submitConfirmedControl).toHaveBeenCalledOnce();
    expect(wrapper.emitted("completed")).toEqual([[applied]]);
    expect(wrapper.text()).toContain("Runtime 状态 HALT");
  });

  it("polls an accepted command until its durable final result arrives", async () => {
    vi.useFakeTimers();
    const api = gateway(accepted);
    const wrapper = mount(ProtectedControlDialog, {
      props: { show: true, gateway: api, control: controlDefinition("EMERGENCY_HALT") },
      global: { stubs: { teleport: true } },
    });
    await flushPromises();
    await wrapper.get('input[placeholder="输入上方完整口令"]').setValue("确认紧急停止");
    const execute = wrapper
      .findAll("button")
      .find((button) => button.text().includes("确认并执行紧急停止"));
    await execute?.trigger("click");
    await flushPromises();
    await vi.runAllTimersAsync();
    await flushPromises();

    expect(api.getControlReceipt).toHaveBeenCalledWith(accepted.status_url);
    expect(wrapper.emitted("completed")).toEqual([[applied]]);
  });

  it("asks the shell to reauthenticate when session authority expires", async () => {
    const api = gateway();
    vi.mocked(api.createControlPreview).mockRejectedValueOnce(
      new LocalConsoleApiError(401, "OPERATOR_SESSION_INVALID", "会话已过期"),
    );
    const wrapper = mount(ProtectedControlDialog, {
      props: { show: true, gateway: api, control: controlDefinition("EMERGENCY_HALT") },
      global: { stubs: { teleport: true } },
    });
    await flushPromises();

    expect(wrapper.text()).toContain("会话已过期");
    expect(wrapper.emitted("unauthorized")).toHaveLength(1);
  });
});
