import { describe, expect, it } from "vitest";

import { NAVIGATION_ITEMS } from "./navigation";
import { CONSOLE_ROUTES, createTestRouter } from "./router";

describe("console routing", () => {
  it("provides every frozen local-console destination exactly once", () => {
    const paths = NAVIGATION_ITEMS.map((item) => item.path);

    expect(NAVIGATION_ITEMS).toHaveLength(10);
    expect(new Set(paths).size).toBe(paths.length);
    expect(paths).toEqual([
      "/overview",
      "/plans",
      "/orders",
      "/portfolio",
      "/execution",
      "/risk",
      "/reconciliation",
      "/incidents",
      "/diagnostics",
      "/settings",
    ]);
    expect(CONSOLE_ROUTES).toHaveLength(12);
  });

  it("redirects unknown locations to the local overview", async () => {
    const router = createTestRouter();
    await router.push("/not-a-page");
    await router.isReady();

    expect(router.currentRoute.value.fullPath).toBe("/overview");
  });

  it("resolves a lazy module with its delivery metadata", async () => {
    const router = createTestRouter();
    await router.push("/orders");
    await router.isReady();

    const matched = router.currentRoute.value.matched.at(-1);
    expect(matched?.props.default).toMatchObject({
      title: "委托与成交",
      delivery: "UI-002",
    });
    expect(router.currentRoute.value.meta.title).toBe("委托与成交");
  });
});
