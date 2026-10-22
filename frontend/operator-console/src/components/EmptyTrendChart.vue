<script setup lang="ts">
import { LineChart } from "echarts/charts";
import { GridComponent, TooltipComponent } from "echarts/components";
import { init, use, type ECharts } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { onBeforeUnmount, onMounted, ref } from "vue";

use([LineChart, GridComponent, TooltipComponent, CanvasRenderer]);

const chartElement = ref<HTMLDivElement | null>(null);
let chart: ECharts | null = null;
let observer: ResizeObserver | null = null;

onMounted(() => {
  if (chartElement.value === null) return;
  chart = init(chartElement.value, undefined, { renderer: "canvas" });
  chart.setOption({
    animation: false,
    grid: { left: 38, right: 12, top: 16, bottom: 24 },
    tooltip: { show: false },
    xAxis: {
      type: "category",
      boundaryGap: false,
      data: ["09:30", "10:30", "11:30", "13:30", "14:30", "15:00"],
      axisLine: { lineStyle: { color: "#334155" } },
      axisLabel: { color: "#718096", fontSize: 10 },
      axisTick: { show: false },
    },
    yAxis: {
      type: "value",
      min: 0,
      max: 1,
      splitNumber: 2,
      axisLabel: { show: false },
      splitLine: { lineStyle: { color: "#1d2a38", type: "dashed" } },
    },
    series: [
      {
        type: "line",
        data: [null, null, null, null, null, null],
        showSymbol: false,
        lineStyle: { color: "#38bdf8", width: 2 },
      },
    ],
  });
  observer = new ResizeObserver(() => chart?.resize());
  observer.observe(chartElement.value);
});

onBeforeUnmount(() => {
  observer?.disconnect();
  chart?.dispose();
  observer = null;
  chart = null;
});
</script>

<template>
  <div class="empty-trend-chart" role="img" aria-label="日内资产曲线，等待首次资产快照">
    <div ref="chartElement" class="empty-trend-chart__canvas" aria-hidden="true" />
    <span class="empty-trend-chart__label">等待资产快照</span>
  </div>
</template>
