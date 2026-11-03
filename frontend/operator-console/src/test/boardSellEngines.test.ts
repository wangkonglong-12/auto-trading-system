import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, expect, it, vi } from "vitest";
import BoardSellEngines from "../node/components/BoardSellEngines.vue";

const session = { actor_id: "operator", csrf_token: "csrf", expires_at: "2099-01-01T00:00:00Z" };
const board = () => ({
  enabled: false,
  authorized: false,
  state: "READY",
  configuration_version: 4,
  startup_status: "PAUSED",
  targets: [],
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

it("reads status on login and starts or pauses each board without a preview", async () => {
  const calls: { url: string; body: unknown }[] = [];
  const status = { restored: true, boards: { MAIN: board(), CHINEXT: board() } };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url, body: init.body ? JSON.parse(String(init.body)) : null });
      if (url.endsWith("/start")) status.boards.MAIN.enabled = true;
      if (url.endsWith("/pause")) status.boards.MAIN.enabled = false;
      return new Response(JSON.stringify(status));
    }),
  );
  const wrapper = mount(BoardSellEngines, { props: { session, blocked: false } });
  try {
    await flushPromises();
    expect(calls).toHaveLength(1);
    expect(calls[0]!.url).toBe("/api/v2/node/sell-engines");
    const click = async (text: string) => {
      await wrapper
        .findAll("button")
        .find((b) => b.text() === text)!
        .trigger("click");
      await flushPromises();
    };
    await click("启动主板自动卖出");
    expect(calls.at(-1)).toEqual({
      url: "/api/v2/node/sell-engines/start",
      body: { board: "MAIN" },
    });
    expect(wrapper.find("input").exists()).toBe(false);
    expect(wrapper.text()).toContain("启动创业板自动卖出");
    await click("暂停主板");
    expect(calls.at(-1)?.body).toEqual({ board: "MAIN" });
    expect(calls.some((c) => /preview|confirm|prepare/.test(c.url))).toBe(false);
  } finally {
    wrapper.unmount();
  }
});

it("polls read-only status, distinguishes empty holdings and never restarts a pause", async () => {
  vi.useFakeTimers();
  const status = { restored: true, boards: { MAIN: board(), CHINEXT: board() } };
  status.boards.CHINEXT.startup_status = "BOARD_SELL_NO_ELIGIBLE_LOTS";
  const calls: RequestInit[] = [];
  const fetch = vi.fn(async (_url: string, init: RequestInit) => {
    calls.push(init);
    return new Response(JSON.stringify(status));
  });
  vi.stubGlobal("fetch", fetch);
  const wrapper = mount(BoardSellEngines, { props: { session: null, blocked: false } });
  try {
    expect(fetch).not.toHaveBeenCalled();
    await wrapper.setProps({ session });
    await flushPromises();
    expect(wrapper.text()).toContain("暂无可卖持仓");
    expect(wrapper.text()).toContain("已手动暂停");
    await vi.advanceTimersByTimeAsync(4000);
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(calls.every((call) => call.method === "GET")).toBe(true);
    await wrapper.setProps({ session: null });
    await vi.advanceTimersByTimeAsync(4000);
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(wrapper.text()).not.toContain("已手动暂停");
  } finally {
    wrapper.unmount();
  }
});

it("discards a delayed status response after logout", async () => {
  let finish!: (response: Response) => void;
  vi.stubGlobal(
    "fetch",
    vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          finish = resolve;
        }),
    ),
  );
  const wrapper = mount(BoardSellEngines, { props: { session, blocked: false } });
  await wrapper.setProps({ session: null });
  finish(
    new Response(JSON.stringify({ restored: true, boards: { MAIN: board(), CHINEXT: board() } })),
  );
  await flushPromises();
  expect(wrapper.text()).not.toContain("已手动暂停");
  wrapper.unmount();
});
