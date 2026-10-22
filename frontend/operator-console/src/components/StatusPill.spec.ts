import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import StatusPill from "./StatusPill.vue";

describe("StatusPill", () => {
  it.each([
    ["UNKNOWN", "未知"],
    ["CONNECTED", "已连接"],
    ["DEGRADED", "已降级"],
    ["DISCONNECTED", "已断开"],
  ] as const)("renders %s explicitly", (state, label) => {
    const wrapper = mount(StatusPill, { props: { label: "QMT", state } });

    expect(wrapper.attributes("data-state")).toBe(state);
    expect(wrapper.text()).toContain("QMT");
    expect(wrapper.text()).toContain(label);
  });
});
