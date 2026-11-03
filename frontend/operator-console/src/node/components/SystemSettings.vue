<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { NIcon } from "naive-ui";
import {
  LinkOutline,
  ShieldCheckmarkOutline,
  PowerOutline,
  FolderOpenOutline,
  InformationCircleOutline,
} from "@vicons/ionicons5";
import type { NodeStatus, Session, SystemSettingsResponse, SystemSettingsValues } from "../api";
import { timeText } from "../consoleTypes";

const props = defineProps<{
  connection: string;
  broker: NodeStatus["broker"] | undefined;
  session: Session | null;
  busy: boolean;
  fresh: boolean;
  running: boolean;
  halted: boolean;
  settings?: SystemSettingsResponse | null;
}>();
const emit = defineEmits<{
  logout: [];
  save: [value: SystemSettingsValues];
  changePassword: [value: { current_password: string; new_password: string }];
  saveAccessToken: [token: string];
}>();
const currentPassword = ref("");
const newPassword = ref("");
const repeatedPassword = ref("");
const passwordError = ref("");
const quantHost = ref("");
const quantPort = ref(443);
const quantScheme = ref<"http" | "https">("https");
const closeWarning = ref(true);
const accessToken = ref("");
function saveAccessToken() {
  if (!props.session || props.busy || !/^[A-Za-z0-9_-]{43,256}$/.test(accessToken.value)) return;
  const token = accessToken.value;
  accessToken.value = "";
  emit("saveAccessToken", token);
}
watch(
  () => props.settings,
  (value) => {
    if (!value) return;
    quantHost.value = value.quant_host;
    quantScheme.value = value.quant_scheme;
    quantPort.value = value.quant_port;
    closeWarning.value = value.close_warning;
  },
  { immediate: true },
);
watch(
  () => props.session,
  () => {
    currentPassword.value = newPassword.value = repeatedPassword.value = passwordError.value = "";
    accessToken.value = "";
  },
);
function changePassword() {
  if (!props.session || props.busy) return;
  if (newPassword.value !== repeatedPassword.value) {
    passwordError.value = "两次输入的新密码不一致。";
    return;
  }
  if (!currentPassword.value || newPassword.value.length < 14 || newPassword.value.length > 128) {
    passwordError.value = "请填写当前密码，新密码需14～128个字符。";
    return;
  }
  const value = { current_password: currentPassword.value, new_password: newPassword.value };
  currentPassword.value = newPassword.value = repeatedPassword.value = passwordError.value = "";
  emit("changePassword", value);
}
function save() {
  if (!props.session || !props.settings || props.busy) return;
  emit("save", {
    version: props.settings.version,
    close_warning: closeWarning.value,
    quant_host: quantHost.value.trim(),
    quant_scheme: quantScheme.value,
    quant_port: quantPort.value,
  });
}
const mode = computed(() =>
  props.broker === "QMT_READ_ONLY"
    ? "真实账户只读"
    : props.broker === "QMT_TRADING"
      ? "实盘连接"
      : "尚未连接QMT",
);
const runtimeState = computed(() =>
  !props.fresh
    ? "状态待核对"
    : props.running
      ? "自动买入运行中"
      : props.halted
        ? "已暂停自动发单"
        : "等待账户核验",
);
const startupItems = [
  { title: "开机启动", description: "开机后打开本机程序；启动程序不会自动恢复下单。" },
  { title: "异常提醒", description: "连接中断、委托失败或交易停止时，提示需要处理的问题。" },
];
const maintenanceItems = [
  { title: "导出诊断日志", description: "导出排查运行问题所需的日志，隐藏敏感信息。" },
  { title: "备份系统数据", description: "备份交易设置、关键交易记录与恢复所需的数据。" },
  { title: "从备份恢复", description: "选择并核验备份后恢复，恢复完成仍需重新核验账户。" },
];
</script>

<template>
  <div class="system-settings">
    <p class="muted system-intro">管理本机连接、登录安全和日常维护。交易参数请到交易设置修改。</p>
    <p class="system-planned-note">标有“待接入”的功能已预留位置，当前不能保存或执行。</p>

    <section class="system-section" aria-labelledby="system-qmt-heading">
      <div class="system-section-heading">
        <NIcon :component="LinkOutline" />
        <h2 id="system-qmt-heading">QMT连接</h2>
      </div>
      <div class="system-setting-row">
        <div>
          <strong>{{ connection }}</strong>
          <p>{{ mode }}。连接成功不代表已开启自动下单。</p>
        </div>
        <a class="button" href="/connect-qmt">
          {{
            broker === "QMT_READ_ONLY" || broker === "QMT_TRADING" ? "检查连接设置" : "连接本机QMT"
          }}
        </a>
      </div>
    </section>

    <section class="system-section" aria-labelledby="system-quant-heading">
      <div class="system-section-heading">
        <NIcon :component="LinkOutline" />
        <h2 id="system-quant-heading">量化后台连接</h2>
      </div>
      <form @submit.prevent="save">
        <fieldset class="system-password-form" :disabled="busy || !session || !settings">
          <label>
            连接协议
            <select v-model="quantScheme" name="quant-scheme">
              <option value="http">HTTP</option>
              <option value="https">HTTPS</option>
            </select>
          </label>
          <label>
            后台域名或IP
            <input
              v-model="quantHost"
              name="quant-host"
              required
              maxlength="253"
              placeholder="quant.example.com"
            />
          </label>
          <label>
            后台端口
            <input
              v-model.number="quantPort"
              name="quant-port"
              type="number"
              required
              min="1"
              max="65535"
              step="1"
            />
          </label>
          <small>只填写域名或IP，不含协议、路径或端口；修改后重新打开主程序生效。</small>
          <small v-if="quantScheme === 'http'">HTTP不会加密接口令牌和传输内容。</small>
          <label>
            <input v-model="closeWarning" name="close-warning" type="checkbox" />
            关闭网页前提醒
          </label>
          <small>
            交易运行中或状态待核对时，关闭/刷新浏览器页面会触发浏览器确认。关闭页面后后台仍可运行；停止程序请使用桌面主窗口。主程序窗口退出始终要求确认，不受此开关影响。浏览器提示文字由浏览器决定。
          </small>
          <button class="button" type="submit">保存系统设置</button>
        </fieldset>
      </form>
      <p v-if="settings">当前进程使用：{{ settings.active_origin }}</p>
      <p v-if="settings?.restart_required" class="notice warning">
        后台地址已保存，等待重启主程序生效。
      </p>
      <p v-if="!session" class="muted">请先登录后修改。</p>
      <form @submit.prevent="saveAccessToken">
        <fieldset class="system-password-form" :disabled="busy || !session">
          <label>
            接口访问令牌
            <input
              v-model="accessToken"
              name="quant-access-token"
              type="password"
              autocomplete="off"
              required
              minlength="43"
              maxlength="256"
              pattern="[A-Za-z0-9_-]{43,256}"
              :placeholder="
                settings?.access_token_configured
                  ? '已保存，无需重复填写；填写可替换'
                  : '填写后台提供的接口令牌'
              "
            />
          </label>
          <small>测试与正式共用，只需保存一次。不会改变本机登录密码或开启交易。</small>
          <button class="button" type="submit">保存接口令牌</button>
        </fieldset>
      </form>
    </section>

    <section class="system-section" aria-labelledby="system-security-heading">
      <div class="system-section-heading">
        <NIcon :component="ShieldCheckmarkOutline" />
        <h2 id="system-security-heading">登录与安全</h2>
      </div>
      <div class="system-setting-row">
        <div>
          <strong>{{ session ? "当前已登录" : "尚未登录" }}</strong>
          <p v-if="session" class="system-session">
            {{ session.actor_id }} · 本次登录有效至 {{ timeText(session.expires_at) }}
          </p>
          <p v-else>登录后可查看账户和管理本机操作。</p>
        </div>
        <button v-if="session" class="button" :disabled="busy" @click="$emit('logout')">
          退出登录
        </button>
      </div>
      <details class="system-password">
        <summary>修改登录密码</summary>
        <p>验证当前密码后保存新密码，保存成功后所有会话需重新登录。此操作不修改QMT密码。</p>
        <form @submit.prevent="changePassword">
          <fieldset
            :disabled="busy || !session"
            class="system-password-form"
            aria-label="修改登录密码"
          >
            <label>
              当前登录密码
              <input
                v-model="currentPassword"
                type="password"
                autocomplete="current-password"
                required
                maxlength="128"
              />
            </label>
            <label>
              新登录密码
              <input
                v-model="newPassword"
                type="password"
                autocomplete="new-password"
                required
                minlength="14"
                maxlength="128"
              />
            </label>
            <label>
              再次输入新密码
              <input
                v-model="repeatedPassword"
                type="password"
                autocomplete="new-password"
                required
                minlength="14"
                maxlength="128"
              />
            </label>
            <small>新密码需14～128个字符，不支持首尾空格；修改成功需重新登录。</small>
            <p v-if="passwordError" role="alert">{{ passwordError }}</p>
            <button class="button" type="submit">保存新密码</button>
          </fieldset>
        </form>
      </details>
    </section>

    <section class="system-section" aria-labelledby="system-startup-heading">
      <div class="system-section-heading">
        <NIcon :component="PowerOutline" />
        <h2 id="system-startup-heading">启动与提醒</h2>
      </div>
      <div v-for="item in startupItems" :key="item.title" class="system-setting-row">
        <div>
          <strong>{{ item.title }}</strong>
          <p>{{ item.description }}</p>
        </div>
        <span class="planned-badge">待接入</span>
      </div>
    </section>

    <section class="system-section" aria-labelledby="system-maintenance-heading">
      <div class="system-section-heading">
        <NIcon :component="FolderOpenOutline" />
        <h2 id="system-maintenance-heading">日志与数据</h2>
      </div>
      <div v-for="item in maintenanceItems" :key="item.title" class="system-setting-row">
        <div>
          <strong>{{ item.title }}</strong>
          <p>{{ item.description }}</p>
        </div>
        <button class="button" type="button" disabled :aria-label="`${item.title}（待接入）`">
          待接入
        </button>
      </div>
    </section>

    <section class="system-section" aria-labelledby="system-about-heading">
      <div class="system-section-heading">
        <NIcon :component="InformationCircleOutline" />
        <h2 id="system-about-heading">版本与更新</h2>
      </div>
      <div class="system-setting-row">
        <div>
          <strong>本机运行状态</strong>
          <p>{{ runtimeState }} · {{ mode }}</p>
        </div>
      </div>
      <div class="system-setting-row">
        <div>
          <strong>软件版本与更新</strong>
          <p>后续支持查看当前版本、更新记录与可用更新。</p>
        </div>
        <button class="button" type="button" disabled aria-label="检查更新（待接入）">
          检查更新 · 待接入
        </button>
      </div>
    </section>
  </div>
</template>
