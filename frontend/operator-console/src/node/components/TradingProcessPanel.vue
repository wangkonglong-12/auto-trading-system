<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import type { TradingProcess } from "../consoleTypes";
import { timeText } from "../consoleTypes";

const props = defineProps<{
  process?: TradingProcess | undefined;
  loggedIn: boolean;
  stateLabel: string;
}>();
const viewport = ref<HTMLElement>();
const follow = ref(true);
const paused = ref(false);
const stock = ref("");
const category = ref("ALL");
const events = computed(() =>
  (props.process?.events ?? []).filter(
    (event) =>
      (!stock.value || event.symbol === stock.value) &&
      (category.value === "ALL" || event.level === category.value),
  ),
);
const labels = { INFO: "进度", WAIT: "等待", ERROR: "异常", SUCCESS: "买入成功" };
function scrolled() {
  if (paused.value) return;
  const element = viewport.value;
  if (element) follow.value = element.scrollHeight - element.clientHeight - element.scrollTop < 40;
}
async function latest() {
  paused.value = false;
  follow.value = true;
  await nextTick();
  if (viewport.value) viewport.value.scrollTop = viewport.value.scrollHeight;
}
function pause() {
  paused.value = true;
  follow.value = false;
}
watch(
  () => props.process?.stream_id,
  () => {
    stock.value = "";
    category.value = "ALL";
    paused.value = false;
    follow.value = true;
  },
);
watch(
  () => [props.process?.stream_id, props.process?.events.at(-1)?.seq, stock.value, category.value],
  async () => {
    if (follow.value) await latest();
  },
  { flush: "post" },
);
</script>

<template>
  <section class="process-panel" aria-labelledby="process-title">
    <div class="process-heading">
      <div>
        <h2 id="process-title">交易过程</h2>
        <p>{{ stateLabel }}</p>
      </div>
      <button class="button" @click="follow ? pause() : latest()">
        {{ follow ? "暂停滚动" : "回到最新" }}
      </button>
    </div>
    <template v-if="loggedIn">
      <div class="process-stocks" aria-label="交易股票名单">
        <strong>共 {{ process?.stocks.length ?? 0 }} 只股票</strong>
        <span v-for="item in process?.stocks ?? []" :key="item.symbol" class="process-stock">
          <b>{{ item.symbol }}</b>
          {{ item.name }}
        </span>
        <span v-if="!process?.stocks.length" class="muted">等待选股或本系统手动买入记录</span>
      </div>
      <div class="process-filters">
        <label>
          股票
          <select v-model="stock" aria-label="筛选交易过程股票">
            <option value="">全部股票 / 系统</option>
            <option v-for="item in process?.stocks ?? []" :key="item.symbol" :value="item.symbol">
              {{ item.symbol }} {{ item.name }}
            </option>
          </select>
        </label>
        <label>
          内容
          <select v-model="category" aria-label="筛选交易过程内容">
            <option value="ALL">全部事项</option>
            <option value="ERROR">异常</option>
            <option value="SUCCESS">买入成功</option>
          </select>
        </label>
        <span class="muted">重要事项约每5秒更新；每行包含记录时间、股票及进展。</span>
      </div>
      <p v-if="process?.warning" class="process-warning" role="status">{{ process.warning }}</p>
      <div
        ref="viewport"
        class="process-viewport"
        role="log"
        aria-label="交易过程记录"
        aria-live="polite"
        aria-relevant="additions"
        tabindex="0"
        @scroll="scrolled"
      >
        <div
          v-for="event in events"
          :key="`${process?.stream_id}:${event.seq}`"
          :class="['process-line', `process-${event.level.toLowerCase()}`]"
        >
          <time :datetime="event.occurred_at">{{ timeText(event.occurred_at) }}</time>
          <span class="process-level">{{ labels[event.level] }}</span>
          <span class="process-symbol">
            <template v-if="event.symbol">
              <b>{{ event.symbol }}</b>
              {{ event.name }}
            </template>
            <template v-else>系统</template>
          </span>
          <span>{{ event.message }}</span>
        </div>
        <p v-if="!events.length" class="process-empty">
          {{ process ? "暂无符合条件的记录，等待交易进展。" : "等待后台交易过程记录。" }}
        </p>
      </div>
      <p class="process-note">
        展示最近200条重要记录，本机最多保留当天500条；相同等待状态不重复刷屏。
        委托提交和实际买入分开记录，蓝色买入成功以实际成交核对为准。
      </p>
    </template>
    <p v-else class="process-empty">登录后查看本账户交易过程。</p>
  </section>
</template>

<style scoped>
.process-panel {
  margin-top: 24px;
  background: #fff;
  border: 1px solid #e1e8f1;
  border-radius: 12px;
  padding: 22px;
}
.process-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
}
h2 {
  margin: 0;
  font-size: 20px;
}
.process-heading p,
.process-note {
  color: #66758b;
  font-size: 13px;
  margin: 8px 0;
}
.process-stocks {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin: 18px 0;
}
.process-stock {
  background: #f0f5fc;
  padding: 7px 10px;
  border-radius: 6px;
  font-size: 13px;
}
.process-filters {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 13px;
  margin-bottom: 12px;
}
select {
  margin-left: 6px;
  padding: 6px;
  border: 1px solid #cbd8eb;
  border-radius: 5px;
  background: white;
}
.process-viewport {
  height: 320px;
  overflow: auto;
  border: 1px solid #e1e8f1;
  background: #f8fafc;
  border-radius: 6px;
}
.process-line {
  display: grid;
  grid-template-columns: 68px 64px minmax(160px, 210px) 1fr;
  gap: 10px;
  padding: 10px 12px;
  font-size: 13px;
  line-height: 1.6;
  border-bottom: 1px solid #e9eef5;
  overflow-wrap: anywhere;
}
.process-line time {
  color: #66758b;
  font-variant-numeric: tabular-nums;
}
.process-level {
  font-weight: 600;
}
.process-symbol b {
  display: block;
}
.process-error,
.process-warning {
  color: #c62828;
}
.process-success {
  color: #1d5fd1;
}
.process-wait {
  color: #815a17;
}
.process-empty {
  padding: 24px;
  color: #66758b;
}
.process-note {
  margin-top: 12px;
}
@media (max-width: 800px) {
  .process-panel {
    padding: 15px;
  }
  .process-line {
    grid-template-columns: 68px 1fr;
  }
  .process-symbol {
    grid-column: 1 / -1;
  }
}
</style>
