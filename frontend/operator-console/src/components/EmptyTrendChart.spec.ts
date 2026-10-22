import { mount } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";

const chartMocks = vi.hoisted(() => ({
  dispose: vi.fn(),
  init: vi.fn(),
  resize: vi.fn(),
  setOption: vi.fn(),
  use: vi.fn(),
}));

vi.mock("echarts/core", () => ({
  init: chartMocks.init,
  use: chartMocks.use,
}));
vi.mock("echarts/charts", () => ({ LineChart: class LineChart {} }));
vi.mock("echarts/components", () => ({
  GridComponent: class GridComponent {},
  TooltipComponent: class TooltipComponent {},
}));
vi.mock("echarts/renderers", () => ({ CanvasRenderer: class CanvasRenderer {} }));

import EmptyTrendChart from "./EmptyTrendChart.vue";

describe("EmptyTrendChart", () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it("renders an explicit empty chart and releases it on unmount", () => {
    chartMocks.init.mockReturnValue({
      dispose: chartMocks.dispose,
      resize: chartMocks.resize,
      setOption: chartMocks.setOption,
    });

    const wrapper = mount(EmptyTrendChart);

    expect(chartMocks.use).toHaveBeenCalledOnce();
    expect(chartMocks.init).toHaveBeenCalledOnce();
    expect(chartMocks.setOption).toHaveBeenCalledWith(
      expect.objectContaining({
        animation: false,
        xAxis: expect.objectContaining({ type: "category" }),
      }),
    );
    expect(wrapper.attributes("aria-label")).toContain("等待首次资产快照");

    wrapper.unmount();
    expect(chartMocks.dispose).toHaveBeenCalledOnce();
  });
});
