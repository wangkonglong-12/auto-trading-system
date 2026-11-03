import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import BoardSellConfiguration from "../node/components/BoardSellConfiguration.vue";

const defaults = {
  main: { mode: "TAKE_PROFIT_OR_TIME", take_profit_pct: "7.00", sell_at: "10:30" },
  chinext: { mode: "TIME_ONLY", take_profit_pct: null, sell_at: "10:50" },
  include_automatic: true,
  include_system_manual: true,
  cancel_after_seconds: 5,
};
const props = {
  session: { actor_id: "operator", csrf_token: "csrf", expires_at: "2099-01-01T00:00:00Z" },
  blocked: false,
};
const reply = (draft: unknown = null) => ({
  draft,
  defaults,
  execution_supported: false,
  execution_enabled: false,
});
const calls: { path: string; init: RequestInit }[] = [];
let handler: (init: RequestInit) => Response | Promise<Response>;
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });
beforeEach(() => {
  calls.length = 0;
  handler = () => response(reply());
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ path: url, init });
      return handler(init);
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());
async function open() {
  const w = mount(BoardSellConfiguration, { props });
  await flushPromises();
  return w;
}

describe("saved sell configuration without execution", () => {
  it("loads defaults with execution unavailable", async () => {
    const w = await open();
    expect(w.get('[aria-label="主板止盈比例"]').element).toHaveProperty("value", "7.00");
    expect(w.get('[aria-label="创业板止盈比例"]').attributes("disabled")).toBeDefined();
    expect(w.text()).toContain("保存参数不会自动开启");
    expect(calls.every((c) => c.init.method === "GET")).toBe(true);
    w.unmount();
  });
  it("saves independent board rules, never calling trading controls", async () => {
    handler = (init) =>
      init.method === "POST"
        ? response(reply({ version: 1, settings: JSON.parse(String(init.body)).settings }))
        : response(reply());
    const w = await open();
    await w.get('[aria-label="创业板退出方式"]').setValue("TAKE_PROFIT_OR_TIME");
    expect(w.get('button[type="submit"]').attributes("disabled")).toBeDefined();
    await w.get('[aria-label="创业板止盈比例"]').setValue("12.50");
    await w.get('[aria-label="卖出配置撤单时效"]').setValue(8);
    await w.get("form").trigger("submit");
    await flushPromises();
    const body = JSON.parse(String(calls.find((c) => c.init.method === "POST")!.init.body));
    expect(body.settings.main.take_profit_pct).toBe("7.00");
    expect(body.settings.chinext.take_profit_pct).toBe("12.50");
    expect(body.settings.cancel_after_seconds).toBe(8);
    expect(body.settings.enabled).toBeUndefined();
    expect(w.text()).toContain("下一轮检查时应用到已准备持仓，无需重启");
    expect(calls.every((c) => c.path.endsWith("/sell-configuration"))).toBe(true);
    w.unmount();
  });
  it.each(["0", "100.01", "1.234", "-1"])("blocks invalid profit %s", async (profit) => {
    const w = await open();
    await w.get('[aria-label="主板止盈比例"]').setValue(profit);
    await w.get("form").trigger("submit");
    await flushPromises();
    expect(calls.filter((c) => c.init.method === "POST")).toHaveLength(0);
    w.unmount();
  });
  it("requires read-back after version conflict rather than retrying writes", async () => {
    handler = (init) =>
      init.method === "POST"
        ? response({ code: "SELL_CONFIGURATION_VERSION_CONFLICT" }, 409)
        : response(reply());
    const w = await open();
    await w.get("form").trigger("submit");
    await flushPromises();
    expect(w.text()).toContain("请先重新读取");
    await w.get("form").trigger("submit");
    await flushPromises();
    expect(calls.filter((c) => c.init.method === "POST")).toHaveLength(1);
    w.unmount();
  });
  it("ignores a late save after logout", async () => {
    let finish: ((r: Response) => void) | undefined;
    handler = (init) =>
      init.method === "POST"
        ? new Promise<Response>((resolve) => {
            finish = resolve;
          })
        : response(reply());
    const w = await open();
    await w.get("form").trigger("submit");
    await flushPromises();
    await w.setProps({ session: null });
    finish!(response(reply({ version: 1, settings: defaults })));
    await flushPromises();
    expect(w.text()).not.toContain("卖出配置已保存");
    expect(w.find("form").exists()).toBe(false);
    w.unmount();
  });
});
