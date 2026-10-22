<script setup lang="ts">
import type { ConnectionState } from "../models/shell";

defineProps<{
  label: string;
  state: ConnectionState;
}>();

const STATE_LABELS: Readonly<Record<ConnectionState, string>> = Object.freeze({
  UNKNOWN: "未知",
  CONNECTED: "已连接",
  DEGRADED: "已降级",
  DISCONNECTED: "已断开",
});
</script>

<template>
  <span class="status-pill" :data-state="state">
    <span class="status-pill__dot" aria-hidden="true" />
    <span class="status-pill__label">{{ label }}</span>
    <span class="status-pill__value">{{ STATE_LABELS[state] }}</span>
  </span>
</template>
