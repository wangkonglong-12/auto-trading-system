<script setup lang="ts">
import { NButton, NDropdown } from "naive-ui";
import { computed, onMounted, ref } from "vue";
import { RouterLink, RouterView, useRoute } from "vue-router";

import { NAVIGATION_ITEMS } from "../navigation";
import {
  OPERATOR_CONTROLS,
  controlDefinition,
  type LocalControlAction,
  type OperatorControlDefinition,
} from "../models/controls";
import { EMPTY_SHELL_STATUS } from "../models/shell";
import {
  LocalConsoleApiError,
  localConsoleApi,
  type LocalConsoleGateway,
  type LocalControlReceipt,
  type OperatorSession,
} from "../services/localConsoleApi";
import OperatorLoginDialog from "./OperatorLoginDialog.vue";
import ProtectedControlDialog from "./ProtectedControlDialog.vue";
import StatusPill from "./StatusPill.vue";

const props = withDefaults(defineProps<{ gateway?: LocalConsoleGateway }>(), {
  gateway: () => localConsoleApi,
});

const route = useRoute();
const status = EMPTY_SHELL_STATUS;
const session = ref<OperatorSession | null>(null);
const mutationReady = ref(false);
const loginVisible = ref(false);
const loginLoading = ref(false);
const loginError = ref<string | null>(null);
const controlVisible = ref(false);
const selectedControl = ref<OperatorControlDefinition>(controlDefinition("EMERGENCY_HALT"));
const pendingControlAction = ref<LocalControlAction | null>(null);
const foundationMessage = ref("操作台基础已加载，等待 Runtime 首次状态快照");
const controlOptions = OPERATOR_CONTROLS.map((control) => ({
  key: control.action,
  label: control.label,
}));

const operatorLabel = computed(() => {
  if (mutationReady.value && session.value) return `已验证 ${session.value.actor_id}`;
  if (session.value) return `重新验证 ${session.value.actor_id}`;
  return "验证操作员";
});

onMounted(async () => {
  try {
    session.value = await props.gateway.getSession();
  } catch {
    session.value = null;
  }
});

function openLogin(): void {
  loginError.value = null;
  loginVisible.value = true;
}

function openHalt(): void {
  requestControl("EMERGENCY_HALT");
}

function requestControl(action: LocalControlAction): void {
  selectedControl.value = controlDefinition(action);
  if (!mutationReady.value) {
    pendingControlAction.value = action;
    openLogin();
    return;
  }
  controlVisible.value = true;
}

function selectControl(key: string | number): void {
  if (typeof key !== "string") return;
  const selected = OPERATOR_CONTROLS.find((control) => control.action === key);
  if (selected) requestControl(selected.action);
}

async function login(password: string): Promise<void> {
  loginLoading.value = true;
  loginError.value = null;
  try {
    session.value = await props.gateway.login(password);
    mutationReady.value = true;
    loginVisible.value = false;
    foundationMessage.value = "本机操作员身份已验证，可以执行受保护的控制操作";
    if (pendingControlAction.value !== null) {
      selectedControl.value = controlDefinition(pendingControlAction.value);
      pendingControlAction.value = null;
      controlVisible.value = true;
    }
  } catch (caught: unknown) {
    loginError.value = errorMessage(caught);
  } finally {
    loginLoading.value = false;
  }
}

async function logout(): Promise<void> {
  try {
    await props.gateway.logout();
    session.value = null;
    mutationReady.value = false;
    controlVisible.value = false;
    foundationMessage.value = "本机操作会话已结束，控制操作需要重新验证";
  } catch (caught: unknown) {
    foundationMessage.value = errorMessage(caught);
  }
}

function requireReauthentication(): void {
  controlVisible.value = false;
  session.value = null;
  mutationReady.value = false;
  openLogin();
}

function controlCompleted(receipt: LocalControlReceipt): void {
  const label = OPERATOR_CONTROLS.find((control) => control.action === receipt.action)?.label;
  if (receipt.status === "APPLIED") {
    foundationMessage.value = `${label ?? receipt.action}已执行，Runtime 状态 ${receipt.current_state ?? "未知"}`;
  } else {
    foundationMessage.value = `${label ?? receipt.action}结果为 ${receipt.status}，请核对操作记录`;
  }
}

function errorMessage(caught: unknown): string {
  if (caught instanceof LocalConsoleApiError && caught.retryAfterSeconds !== null) {
    return `${caught.message}，${caught.retryAfterSeconds} 秒后可重试`;
  }
  return caught instanceof Error ? caught.message : "本地控制服务暂时不可用";
}
</script>

<template>
  <a class="skip-link" href="#main-content">跳到主要内容</a>
  <div class="console-shell">
    <aside class="sidebar" aria-label="操作台主导航">
      <div class="brand">
        <span class="brand__mark" aria-hidden="true">AT</span>
        <span class="brand__copy">
          <strong>交易节点</strong>
          <small>OPERATOR CONSOLE</small>
        </span>
      </div>

      <nav class="navigation">
        <RouterLink
          v-for="item in NAVIGATION_ITEMS"
          :key="item.path"
          :to="item.path"
          class="navigation__item"
        >
          <span class="navigation__mark" aria-hidden="true">{{ item.mark }}</span>
          <span>{{ item.label }}</span>
        </RouterLink>
      </nav>

      <div class="sidebar__footer">
        <span class="sidebar__footer-label">本地节点</span>
        <strong>127.0.0.1</strong>
        <span>静态资源已就绪</span>
      </div>
    </aside>

    <section class="workspace">
      <header class="topbar">
        <div class="topbar__identity">
          <span class="environment-badge">{{ status.environment }}</span>
          <span class="account-label">账户 {{ status.accountAlias }}</span>
          <span class="mode-label">模式 {{ status.mode }}</span>
        </div>
        <div class="topbar__connections" aria-label="连接状态">
          <StatusPill label="QMT" :state="status.qmt" />
          <StatusPill label="量化服务" :state="status.central" />
        </div>
        <div class="operator-actions">
          <NDropdown :options="controlOptions" trigger="click" @select="selectControl">
            <NButton size="small" secondary>安全控制</NButton>
          </NDropdown>
          <NButton size="small" secondary @click="openLogin">{{ operatorLabel }}</NButton>
          <NButton v-if="mutationReady" size="tiny" quaternary @click="logout">退出</NButton>
        </div>
        <NButton class="halt-button" type="error" secondary @click="openHalt"> 紧急停止 </NButton>
      </header>

      <div class="foundation-notice" role="status">
        <span class="foundation-notice__indicator" aria-hidden="true" />
        <span>{{ foundationMessage }}</span>
        <span class="foundation-notice__route">{{ route.meta.title ?? "运行总览" }}</span>
      </div>

      <main id="main-content" class="main-content" tabindex="-1">
        <RouterView />
      </main>
    </section>
  </div>

  <OperatorLoginDialog
    :show="loginVisible"
    :loading="loginLoading"
    :error="loginError"
    @close="loginVisible = false"
    @submit="login"
  />
  <ProtectedControlDialog
    :show="controlVisible"
    :gateway="props.gateway"
    :control="selectedControl"
    @close="controlVisible = false"
    @unauthorized="requireReauthentication"
    @completed="controlCompleted"
  />
</template>
