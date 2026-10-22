import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import OperatorLoginDialog from "./OperatorLoginDialog.vue";

describe("OperatorLoginDialog", () => {
  it("keeps the password local to the form and emits it only on submit", async () => {
    const wrapper = mount(OperatorLoginDialog, {
      props: { show: true, loading: false, error: null },
      global: { stubs: { teleport: true } },
    });
    await flushPromises();

    const input = wrapper.get('input[type="password"]');
    await input.setValue("operator-secret");
    await wrapper.get("form").trigger("submit");

    expect(wrapper.emitted("submit")).toEqual([["operator-secret"]]);
    expect(wrapper.text()).not.toContain("operator-secret");
  });

  it("shows a generic boundary error and blocks close while login is running", async () => {
    const wrapper = mount(OperatorLoginDialog, {
      props: { show: true, loading: true, error: "身份验证失败" },
      global: { stubs: { teleport: true } },
    });
    await flushPromises();

    expect(wrapper.text()).toContain("身份验证失败");
    expect(wrapper.get('input[type="password"]').attributes()).toHaveProperty("disabled");
    const cancel = wrapper.findAll("button").find((button) => button.text() === "取消");
    expect(cancel?.attributes()).toHaveProperty("disabled");
  });

  it("clears an entered password whenever the dialog opens again", async () => {
    const wrapper = mount(OperatorLoginDialog, {
      props: { show: true, loading: false, error: null },
      global: { stubs: { teleport: true } },
    });
    await flushPromises();
    const input = wrapper.get('input[type="password"]');
    await input.setValue("temporary-secret");

    await wrapper.setProps({ show: false });
    await wrapper.setProps({ show: true });

    expect((wrapper.get('input[type="password"]').element as HTMLInputElement).value).toBe("");
  });
});
