import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import DebugPickDialog from "../node/components/DebugPickDialog.vue";
const proposal = {
  preview_id: "debug-" + "a".repeat(32),
  symbol: "000001.SZ",
  name: "平安银行",
  phrase: "确认真实自动买入 000001.SZ",
  cash: "10000",
  position_limit: 1,
  daily_symbols_limit: 1,
  symbol_limit: "10000",
  daily_limit: "20000",
  cancel_after_seconds: 5,
  max_retries: 3,
  expires_at: new Date(Date.now() + 30000).toISOString(),
};
let wrapper: VueWrapper;
const calls: { path: string; body: Record<string, unknown> }[] = [];
let fail = false;
beforeEach(() => {
  calls.length = 0;
  fail = false;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init: RequestInit) => {
      calls.push({ path, body: JSON.parse(String(init.body)) });
      if (path.endsWith("preview")) return new Response(JSON.stringify(proposal));
      if (fail) throw new Error("network lost");
      return new Response(JSON.stringify({ status: "STARTED" }));
    }),
  );
  wrapper = mount(DebugPickDialog, {
    props: {
      session: {
        actor_id: "operator",
        csrf_token: "csrf",
        expires_at: new Date(Date.now() + 3600000).toISOString(),
      },
    },
  });
});
afterEach(() => {
  wrapper.unmount();
  vi.unstubAllGlobals();
});
async function click(label: string) {
  await wrapper
    .findAll("button")
    .find((b) => b.text() === label)!
    .trigger("click");
  await flushPromises();
}
async function preview() {
  await wrapper.get("#debug-symbol").setValue("000001");
  await click("预览");
}
it("previews only a symbol and requires exact consent for real execution", async () => {
  await preview();
  expect(calls).toHaveLength(1);
  expect(calls[0]!.body).toEqual({ symbol: "000001" });
  expect(wrapper.text()).toContain("平安银行");
  const button = wrapper.findAll("button").find((b) => b.text() === "确认真实自动买入")!;
  expect(button.attributes("disabled")).toBeDefined();
  await wrapper.get("#debug-confirm").setValue(proposal.phrase);
  await click("确认真实自动买入");
  expect(calls[1]!.body).toEqual({ preview_id: proposal.preview_id, phrase: proposal.phrase });
  expect(wrapper.text()).toContain("已接收调试选股");
  expect(wrapper.emitted("submitted")).toHaveLength(1);
});
it("reuses the same confirmation after a lost reply", async () => {
  await preview();
  await wrapper.get("#debug-confirm").setValue(proposal.phrase);
  fail = true;
  await click("确认真实自动买入");
  expect(wrapper.text()).toContain("请求结果未知");
  fail = false;
  await click("核对本次确认");
  expect(calls[1]!.body).toEqual(calls[2]!.body);
  expect(calls.filter((c) => c.path.endsWith("preview"))).toHaveLength(1);
});
