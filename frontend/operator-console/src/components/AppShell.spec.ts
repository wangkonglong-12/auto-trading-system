import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import { createTestRouter } from "../router";
import type { LocalConsoleGateway, OperatorSession } from "../services/localConsoleApi";
import AppShell from "./AppShell.vue";

const session: OperatorSession = {
  actor_id: "operator_demo",
  issued_at: "2026-09-04T10:00:00+08:00",
  expires_at: "2026-09-04T11:00:00+08:00",
  csrf_token: "csrf-token",
};

function gateway(restored: OperatorSession | null = null): LocalConsoleGateway {
  return {
    getSession: vi.fn(async () => restored),
    login: vi.fn(async () => session),
    logout: vi.fn(async () => undefined),
    createControlPreview: vi.fn(),
    confirmControlPreview: vi.fn(),
    submitConfirmedControl: vi.fn(),
    getControlReceipt: vi.fn(),
  };
}

const dialogStubs = {
  EmptyTrendChart: true,
  OperatorLoginDialog: {
    props: ["show", "loading", "error"],
    emits: ["close", "submit"],
    template:
      '<button v-if="show" class="login-submit" @click="$emit(\'submit\', \'operator-password\')">login</button>',
  },
  ProtectedControlDialog: {
    props: ["show", "gateway", "control"],
    emits: ["close", "unauthorized", "completed"],
    template: '<div v-if="show" class="emergency-dialog-stub">halt dialog</div>',
  },
};

describe("AppShell", () => {
  it("keeps identity, local status and every route visible", async () => {
    const router = createTestRouter();
    await router.push("/overview");
    await router.isReady();
    const wrapper = mount(AppShell, {
      props: { gateway: gateway() },
      global: { plugins: [router], stubs: dialogStubs },
    });
    await flushPromises();

    expect(wrapper.get("aside").attributes("aria-label")).toBe("操作台主导航");
    expect(wrapper.findAll(".navigation__item")).toHaveLength(10);
    expect(wrapper.text()).toContain("环境未加载");
    expect(wrapper.text()).toContain("模式 BOOTING");
    expect(wrapper.text()).toContain("等待 Runtime 首次状态快照");
    expect(wrapper.get(".halt-button").attributes()).not.toHaveProperty("disabled");
  });

  it("updates the visible route title after navigation", async () => {
    const router = createTestRouter();
    await router.push("/overview");
    await router.isReady();
    const wrapper = mount(AppShell, {
      props: { gateway: gateway() },
      global: { plugins: [router], stubs: dialogStubs },
    });

    await router.push("/risk");
    await flushPromises();

    expect(wrapper.get(".foundation-notice__route").text()).toBe("风控与控制");
    expect(wrapper.get("h1").text()).toBe("风控与控制");
    expect(wrapper.text()).toContain("UI-003");
  });

  it("requires fresh operator validation before opening emergency halt", async () => {
    const api = gateway();
    const router = createTestRouter();
    await router.push("/overview");
    await router.isReady();
    const wrapper = mount(AppShell, {
      props: { gateway: api },
      global: { plugins: [router], stubs: dialogStubs },
    });
    await flushPromises();

    await wrapper.get(".halt-button").trigger("click");
    expect(wrapper.find(".login-submit").exists()).toBe(true);
    expect(wrapper.find(".emergency-dialog-stub").exists()).toBe(false);

    await wrapper.get(".login-submit").trigger("click");
    await flushPromises();
    expect(api.login).toHaveBeenCalledWith("operator-password");
    expect(wrapper.text()).toContain("已验证 operator_demo");
    expect(wrapper.text()).toContain("可以执行受保护的控制操作");

    await wrapper.get(".halt-button").trigger("click");
    expect(wrapper.find(".emergency-dialog-stub").exists()).toBe(true);
  });

  it("shows a restored identity but still requires fresh mutation proof", async () => {
    const api = gateway({ ...session, csrf_token: null });
    const router = createTestRouter();
    await router.push("/overview");
    await router.isReady();
    const wrapper = mount(AppShell, {
      props: { gateway: api },
      global: { plugins: [router], stubs: dialogStubs },
    });
    await flushPromises();

    expect(wrapper.text()).toContain("重新验证 operator_demo");
    await wrapper.get(".halt-button").trigger("click");
    expect(wrapper.find(".login-submit").exists()).toBe(true);
  });
});
