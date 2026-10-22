<script setup lang="ts">
import {
  NAlert,
  NButton,
  NDescriptions,
  NDescriptionsItem,
  NInput,
  NModal,
  NRadio,
  NRadioGroup,
  NSpin,
} from "naive-ui";
import { computed, onBeforeUnmount, ref, watch } from "vue";

import type { OperatorControlDefinition } from "../models/controls";
import type {
  ControlImpactPreview,
  LocalConsoleGateway,
  LocalControlReceipt,
} from "../services/localConsoleApi";

const props = defineProps<{
  show: boolean;
  gateway: LocalConsoleGateway;
  control: OperatorControlDefinition;
}>();

const emit = defineEmits<{
  close: [];
  unauthorized: [];
  completed: [receipt: LocalControlReceipt];
}>();

const cancelActiveOrders = ref(true);
const preview = ref<ControlImpactPreview | null>(null);
const phrase = ref("");
const loading = ref(false);
const submitting = ref(false);
const error = ref<string | null>(null);
const receipt = ref<LocalControlReceipt | null>(null);
let operationGeneration = 0;

const phraseMatches = computed(
  () => preview.value !== null && phrase.value === preview.value.confirmation_phrase,
);

watch(
  () => props.show,
  (show) => {
    operationGeneration += 1;
    if (show) {
      cancelActiveOrders.value = true;
      phrase.value = "";
      receipt.value = null;
      error.value = null;
      void loadPreview();
    }
  },
  { immediate: true },
);

watch(
  () => props.control,
  () => {
    if (props.show) void loadPreview();
  },
);

onBeforeUnmount(() => {
  operationGeneration += 1;
});

async function loadPreview(): Promise<void> {
  const generation = ++operationGeneration;
  loading.value = true;
  preview.value = null;
  phrase.value = "";
  receipt.value = null;
  error.value = null;
  try {
    const result = await props.gateway.createControlPreview({
      action: props.control.action,
      reasonCode: props.control.reasonCode,
      cancelActiveOrders: props.control.haltOrderDisposition ? cancelActiveOrders.value : null,
    });
    if (generation === operationGeneration) preview.value = result;
  } catch (caught: unknown) {
    if (generation === operationGeneration) handleError(caught);
  } finally {
    if (generation === operationGeneration) loading.value = false;
  }
}

async function submit(): Promise<void> {
  if (!preview.value || !phraseMatches.value || submitting.value) return;
  const generation = ++operationGeneration;
  submitting.value = true;
  error.value = null;
  try {
    const confirmed = await props.gateway.confirmControlPreview(
      preview.value.preview_id,
      phrase.value,
    );
    let latest = await props.gateway.submitConfirmedControl(confirmed);
    receipt.value = latest;
    for (let attempt = 0; !latest.final && attempt < 20; attempt += 1) {
      await wait(latest.poll_after_ms ?? 250);
      if (generation !== operationGeneration) return;
      latest = await props.gateway.getControlReceipt(latest.status_url);
      receipt.value = latest;
    }
    if (latest.final) emit("completed", latest);
    else error.value = "控制命令仍在处理中，请在操作记录中继续核对结果。";
  } catch (caught: unknown) {
    if (generation === operationGeneration) handleError(caught);
  } finally {
    if (generation === operationGeneration) submitting.value = false;
  }
}

function handleError(caught: unknown): void {
  error.value = caught instanceof Error ? caught.message : "本地控制服务暂时不可用";
  if (
    typeof caught === "object" &&
    caught !== null &&
    "status" in caught &&
    (caught as { status: unknown }).status === 401
  ) {
    emit("unauthorized");
  }
}

function wait(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}
</script>

<template>
  <NModal
    :show="show"
    preset="card"
    class="operator-dialog operator-dialog--wide"
    :title="control.label"
    :mask-closable="!submitting"
    :closable="!submitting"
    @close="emit('close')"
  >
    <NAlert :type="control.tone" :show-icon="false" class="halt-banner">
      执行前会锁定当前 Runtime、委托、持仓、连接和对账事实，并生成一次性授权。
    </NAlert>

    <section
      v-if="control.haltOrderDisposition"
      class="halt-section"
      aria-labelledby="order-disposition-title"
    >
      <h3 id="order-disposition-title">活动委托处置</h3>
      <NRadioGroup
        v-model:value="cancelActiveOrders"
        :disabled="loading || submitting"
        @update:value="loadPreview"
      >
        <NRadio :value="true">请求撤销全部活动委托</NRadio>
        <NRadio :value="false">保留活动委托并转人工核对</NRadio>
      </NRadioGroup>
    </section>

    <div v-if="loading" class="dialog-loading"><NSpin size="small" /> 正在读取实时影响</div>

    <template v-if="preview">
      <NDescriptions bordered :column="2" size="small" label-placement="left">
        <NDescriptionsItem label="账户">{{ preview.account_alias }}</NDescriptionsItem>
        <NDescriptionsItem label="环境">{{ preview.profile }}</NDescriptionsItem>
        <NDescriptionsItem label="当前状态">{{ preview.expected_state }}</NDescriptionsItem>
        <NDescriptionsItem label="目标状态">{{ preview.requested_state }}</NDescriptionsItem>
        <NDescriptionsItem label="活动委托">{{ preview.facts.active_orders }}</NDescriptionsItem>
        <NDescriptionsItem label="持仓标的">{{ preview.facts.open_positions }}</NDescriptionsItem>
      </NDescriptions>

      <ul class="impact-warnings" aria-label="操作影响">
        <li v-for="warning in preview.warnings" :key="warning">{{ warning }}</li>
      </ul>

      <section class="halt-section" aria-labelledby="confirmation-title">
        <h3 id="confirmation-title">逐字输入确认口令</h3>
        <code>{{ preview.confirmation_phrase }}</code>
        <NInput
          v-model:value="phrase"
          :disabled="submitting"
          autocomplete="off"
          placeholder="输入上方完整口令"
          @keyup.enter="submit"
        />
      </section>
    </template>

    <NAlert v-if="error" type="error" :show-icon="false" class="dialog-alert">
      {{ error }}
    </NAlert>
    <NAlert v-if="receipt" :type="receipt.final ? 'success' : 'info'" :show-icon="false">
      命令 {{ receipt.command_id }}：{{ receipt.status }}
      <span v-if="receipt.current_state">，Runtime 状态 {{ receipt.current_state }}</span>
    </NAlert>

    <div class="dialog-actions">
      <NButton :disabled="submitting" @click="emit('close')">关闭</NButton>
      <NButton
        :type="control.tone"
        :loading="submitting"
        :disabled="!phraseMatches || loading || Boolean(receipt?.final)"
        @click="submit"
      >
        确认并执行{{ control.label }}
      </NButton>
    </div>
  </NModal>
</template>
