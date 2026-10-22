<script setup lang="ts">
import { NAlert, NButton, NForm, NFormItem, NInput, NModal } from "naive-ui";
import { ref, watch } from "vue";

const props = defineProps<{
  show: boolean;
  loading: boolean;
  error: string | null;
}>();

const emit = defineEmits<{
  close: [];
  submit: [password: string];
}>();

const password = ref("");

watch(
  () => props.show,
  (show) => {
    if (show) password.value = "";
  },
);

function submit(): void {
  if (password.value.length > 0 && !props.loading) emit("submit", password.value);
}
</script>

<template>
  <NModal
    :show="show"
    preset="card"
    class="operator-dialog"
    title="验证本机操作员"
    :mask-closable="!loading"
    :closable="!loading"
    @close="emit('close')"
  >
    <p class="dialog-copy">密码只提交给本机 127.0.0.1 服务，用于建立短时操作会话。</p>
    <NAlert v-if="error" type="error" :show-icon="false" class="dialog-alert">
      {{ error }}
    </NAlert>
    <NForm @submit.prevent="submit">
      <NFormItem label="操作员密码">
        <NInput
          v-model:value="password"
          type="password"
          show-password-on="mousedown"
          autocomplete="current-password"
          :disabled="loading"
          autofocus
          @keyup.enter="submit"
        />
      </NFormItem>
      <div class="dialog-actions">
        <NButton :disabled="loading" @click="emit('close')">取消</NButton>
        <NButton type="primary" attr-type="submit" :loading="loading" :disabled="!password">
          验证并建立会话
        </NButton>
      </div>
    </NForm>
  </NModal>
</template>
