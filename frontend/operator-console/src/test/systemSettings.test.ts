import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import SystemSettings from "../node/components/SystemSettings.vue";
import type { NodeStatus } from "../node/api";

function render(overrides = {}) {
  return mount(SystemSettings, {
    props: {
      connection: "QMT已连接",
      broker: "QMT_READ_ONLY" as NodeStatus["broker"],
      session: {
        actor_id: "local-operator",
        csrf_token: "local-csrf",
        expires_at: "2026-10-23T10:00:00Z",
      },
      busy: false,
      fresh: true,
      running: false,
      halted: true,
      settings: {
        version: 0,
        quant_scheme: "https",
        quant_host: "quant.test",
        quant_port: 443,
        close_warning: true,
        active_origin: "https://quant.test",
        restart_required: false,
      },
      ...overrides,
    },
  });
}

describe("system settings scaffold", () => {
  it("saves one shared API token and clears the input without exposing the saved value", async () => {
    const wrapper = render();
    const input = wrapper.get('[name="quant-access-token"]');
    const token = "s".repeat(43);
    await input.setValue(token);
    await wrapper
      .findAll("form")
      .find((form) => form.find('[name="quant-access-token"]').exists())!
      .trigger("submit");
    expect(wrapper.emitted("saveAccessToken")).toEqual([[token]]);
    expect((input.element as HTMLInputElement).value).toBe("");
    expect(wrapper.text()).toContain("测试与正式共用");
    wrapper.unmount();
  });
  it("enables password changes while unfinished maintenance remains unavailable", async () => {
    const wrapper = render();
    expect(wrapper.text()).toContain("真实账户只读");
    expect(wrapper.text()).toContain("18:00:00");
    expect(wrapper.text()).toContain("已暂停自动发单");
    await wrapper.get("summary").trigger("click");
    expect(wrapper.get('[aria-label="修改登录密码"]').attributes("disabled")).toBeUndefined();
    expect(wrapper.findAll('.system-password input[type="password"]')).toHaveLength(3);
    for (const label of [
      "导出诊断日志（待接入）",
      "备份系统数据（待接入）",
      "从备份恢复（待接入）",
      "检查更新（待接入）",
    ]) {
      expect(wrapper.get(`[aria-label="${label}"]`).attributes("disabled")).toBeDefined();
    }
    expect(wrapper.text()).not.toContain("已是最新版本");
    expect(wrapper.emitted("logout")).toBeUndefined();
    wrapper.unmount();
  });
  it("validates matching passwords, emits once and clears password inputs", async () => {
    const wrapper = render();
    const inputs = wrapper.findAll('.system-password input[type="password"]');
    await inputs[0]!.setValue("old-password-1234");
    await inputs[1]!.setValue("new-password-1234");
    await inputs[2]!.setValue("different-password");
    const form = wrapper.get(".system-password form");
    await form.trigger("submit");
    expect(wrapper.text()).toContain("两次输入的新密码不一致");
    expect(wrapper.emitted("changePassword")).toBeUndefined();
    await inputs[2]!.setValue("new-password-1234");
    await form.trigger("submit");
    expect(wrapper.emitted("changePassword")).toEqual([
      [
        {
          current_password: "old-password-1234",
          new_password: "new-password-1234",
        },
      ],
    ]);
    expect(inputs.every((input) => (input.element as HTMLInputElement).value === "")).toBe(true);
    wrapper.unmount();
  });
  it("submits backend host, port and close reminder without runtime permissions", async () => {
    const wrapper = render();
    await wrapper.get('[name="quant-scheme"]').setValue("http");
    await wrapper.get('[name="quant-host"]').setValue("api.example.com");
    await wrapper.get('[name="quant-port"]').setValue("8443");
    await wrapper.get('[name="close-warning"]').setValue(false);
    await wrapper.get("form").trigger("submit");
    expect(wrapper.emitted("save")).toEqual([
      [
        {
          version: 0,
          quant_scheme: "http",
          quant_host: "api.example.com",
          quant_port: 8443,
          close_warning: false,
        },
      ],
    ]);
    await wrapper.setProps({ session: null });
    expect(wrapper.get('[aria-label="修改登录密码"]').attributes("disabled")).toBeDefined();
    wrapper.unmount();
  });
  it("exposes only the existing logout action and respects in-flight operations", async () => {
    const wrapper = render();
    await wrapper
      .findAll("button")
      .find((b) => b.text() === "退出登录")!
      .trigger("click");
    expect(wrapper.emitted("logout")).toHaveLength(1);
    await wrapper.setProps({ busy: true });
    expect(
      wrapper
        .findAll("button")
        .find((b) => b.text() === "退出登录")!
        .attributes("disabled"),
    ).toBeDefined();
    wrapper.unmount();
  });
  it("shows unknown state without inventing a connected account or saved preferences", () => {
    const wrapper = render({
      broker: undefined,
      session: null,
      fresh: false,
      connection: "QMT状态待核对",
    });
    expect(wrapper.text()).toContain("尚未登录");
    expect(wrapper.text()).toContain("状态待核对");
    expect(wrapper.get('a[href="/connect-qmt"]').text()).toBe("连接本机QMT");
    expect(wrapper.text()).not.toContain("local-operator");
    for (const status of wrapper.findAll(".system-setting-row strong")) {
      expect(status.text()).not.toContain("已开启");
    }
    wrapper.unmount();
  });
  it.each([
    ["QMT_TRADING", true, false, "自动买入运行中", "实盘连接"],
    ["QMT_TRADING", false, false, "等待账户核验", "实盘连接"],
    ["MOCK", false, false, "等待账户核验", "尚未连接QMT"],
  ] as const)("renders the observed runtime mode %s", (broker, running, halted, label, mode) => {
    const wrapper = render({ broker, running, halted });
    expect(wrapper.text()).toContain(label);
    expect(wrapper.text()).toContain(mode);
    wrapper.unmount();
  });
});
