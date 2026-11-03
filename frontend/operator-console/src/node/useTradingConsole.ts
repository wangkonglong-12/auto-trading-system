import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { errorText, nodeRequest, NodeApiError } from "./api";
import type {
  NodeStatus,
  Preview,
  PreviewAction,
  Receipt,
  Session,
  SystemSettingsResponse,
  SystemSettingsValues,
  DailyAutoState,
  DailyAutoPreview,
} from "./api";
import { pendingKey, readPending, writePending } from "./pending";
import type { ConsoleView, Dashboard, SettingsResponse } from "./consoleTypes";
import { validateSettings } from "./settingsValidation";
import type { TradingSettingsValues } from "./settingsValidation";

export function useTradingConsole() {
  const view = ref<ConsoleView>("today");
  const status = ref<NodeStatus | null>(null);
  const session = ref<Session | null>(null);
  const dashboard = ref<Dashboard | null>(null);
  const settings = ref<SettingsResponse | null>(null);
  const dailyAuto = ref<DailyAutoState | null>(null);
  const dailyAutoPreview = ref<DailyAutoPreview | null>(null);
  const systemSettings = ref<SystemSettingsResponse | null>(null);
  const password = ref("");
  const error = ref("");
  const accountError = ref("");
  const message = ref("");
  const busy = ref(false);
  const loadingAccount = ref(false);
  const settingsLoaded = ref(false);
  const halting = ref(false);
  const streamFailed = ref(false);
  const disconnected = ref(false);
  const preview = ref<Preview | null>(null);
  const phrase = ref("");
  const pendingId = ref("");
  const storageFailed = ref(false);
  const receivedAt = ref(0);
  const now = ref(Date.now());
  const lifecycle = new AbortController();
  let events: EventSource | undefined;
  let timer: ReturnType<typeof setInterval> | undefined;
  let alive = true;
  let generation = 0;
  let trackingKey = "";
  let loadingStatus = false;
  let lastAccountRefresh = 0;
  let refreshAfterCancel = false;
  let controlEpoch = 0;
  const engine = computed(() => status.value?.components.engine.data ?? null);
  const fresh = computed(
    () =>
      !disconnected.value &&
      !streamFailed.value &&
      receivedAt.value > 0 &&
      now.value - receivedAt.value < 6000 &&
      status.value?.components.engine.availability === "CURRENT",
  );
  const realAccount = computed(
    () => status.value?.broker === "QMT_READ_ONLY" || status.value?.broker === "QMT_TRADING",
  );
  const unconfigured = computed(() => Boolean(status.value?.node_id.startsWith("unconfigured-")));
  const running = computed(
    () => fresh.value && engine.value?.state === "ARMED" && engine.value?.halted !== true,
  );
  const displayBuys = computed(() => {
    const value = dashboard.value?.buys;
    if (!value) return null;
    if (value.starts_at && now.value < Date.parse(value.starts_at)) return null;
    if (!value.has_manual && value.expires_at && now.value >= Date.parse(value.expires_at))
      return null;
    return value;
  });
  const displaySnapshot = computed(() => displayBuys.value?.display_state === "SNAPSHOT");
  const buyFactsAvailable = computed(
    () =>
      !!displayBuys.value?.available &&
      (displaySnapshot.value || (fresh.value && accountFresh.value)),
  );
  const buys = computed(() => {
    const today = displayBuys.value?.today_summary;
    if (today) return today.available && fresh.value && accountFresh.value ? today : null;
    return buyFactsAvailable.value ? displayBuys.value : null;
  });
  const account = computed(() => (realAccount.value ? (dashboard.value?.account ?? null) : null));
  const accountFresh = computed(() => {
    const observed = account.value?.observed_at;
    return (
      !accountError.value &&
      observed != null &&
      account.value?.available === true &&
      now.value - Date.parse(observed) >= -1000 &&
      now.value - Date.parse(observed) < 15000
    );
  });
  const canControl = computed(
    () =>
      fresh.value &&
      !!session.value &&
      !busy.value &&
      !storageFailed.value &&
      !pendingId.value &&
      status.value?.broker === "QMT_TRADING",
  );
  const startReason = computed(() => {
    if (!session.value) return "登录后可查看账户和操作系统";
    if (!fresh.value) return "运行状态待核对";
    if (!realAccount.value || unconfigured.value) return "请先连接本机QMT";
    if (status.value?.broker === "QMT_READ_ONLY") return "当前为真实账户只读连接，未启用下单";
    if (settings.value?.draft?.settings.buy.signal_mode !== "TAIL_EXCELLENT_3_PHASES")
      return "请先保存新版买入设置，再重新启动引擎";
    if (!settings.value?.buy_configured) return "请先设置买入金额和股票数量";
    if (settings.value.restart_required) return "新设置待加载，请先停止引擎再重新启动";
    if (engine.value?.recovery !== "CLEAR") return "账户核验尚未完成";
    if (!engine.value?.plan_loaded) return "等待后台正式选股结果";
    return "";
  });
  function clearPrivate() {
    generation++;
    refreshAfterCancel = false;
    session.value = null;
    dashboard.value = null;
    settings.value = null;
    systemSettings.value = null;
    settingsLoaded.value = false;
    dailyAuto.value = null;
    dailyAutoPreview.value = null;
    preview.value = null;
    password.value = "";
    phrase.value = "";
  }
  function failed(reason: unknown) {
    error.value = errorText(reason);
    if (reason instanceof NodeApiError && reason.status === 401) clearPrivate();
  }
  async function request<T>(path: string, method = "GET", body?: object): Promise<T> {
    return nodeRequest<T>(path, method, body, session.value?.csrf_token, lifecycle.signal);
  }
  async function loadSettings() {
    const epoch = generation;
    try {
      const result = await request<SettingsResponse>("trading-settings");
      if (alive && epoch === generation && session.value) {
        if (settings.value && settings.value.draft?.version === result.draft?.version)
          result.draft = settings.value.draft;
        settings.value = result;
        settingsLoaded.value = true;
      }
    } catch (reason) {
      if (alive && epoch === generation) failed(reason);
    }
    if (status.value?.broker === "QMT_TRADING" && session.value) {
      try {
        const result = await request<DailyAutoState>("daily-auto");
        if (alive && epoch === generation && session.value) dailyAuto.value = result;
      } catch {
        if (alive && epoch === generation)
          dailyAuto.value = { supported: false, enabled: false, state: "UNAVAILABLE" };
      }
    }
    try {
      const result = await request<SystemSettingsResponse>("system-settings");
      if (alive && epoch === generation && session.value) systemSettings.value = result;
    } catch (reason) {
      if (alive && epoch === generation) failed(reason);
    }
  }
  async function saveSystemSettings(value: SystemSettingsValues) {
    if (busy.value || !session.value) return;
    busy.value = true;
    error.value = message.value = "";
    const epoch = generation;
    try {
      const result = await request<SystemSettingsResponse>("system-settings", "POST", value);
      if (alive && epoch === generation && session.value) {
        systemSettings.value = result;
        message.value = result.restart_required
          ? "系统设置已保存。量化后台地址将在重新打开主程序后生效。"
          : "系统设置已保存。";
      }
    } catch (reason) {
      if (alive && epoch === generation) failed(reason);
    } finally {
      busy.value = false;
    }
  }
  async function saveAccessToken(token: string) {
    if (busy.value || !session.value) return;
    busy.value = true;
    error.value = message.value = "";
    const epoch = generation;
    try {
      await request("system-settings/access-token", "POST", { token });
      if (alive && epoch === generation && session.value) {
        if (systemSettings.value) systemSettings.value.access_token_configured = true;
        message.value = "接口令牌已保存，测试与正式共用，无需切换或重复填写。";
      }
    } catch (reason) {
      if (alive && epoch === generation) failed(reason);
    } finally {
      busy.value = false;
    }
  }
  async function changePassword(value: { current_password: string; new_password: string }) {
    if (busy.value || !session.value) return;
    busy.value = true;
    error.value = message.value = "";
    const epoch = generation;
    try {
      await request("password", "POST", value);
      if (alive && epoch === generation) {
        clearPrivate();
        message.value = "密码已修改，所有登录会话已失效，请使用新密码重新登录。";
      }
    } catch (reason) {
      if (alive && epoch === generation) failed(reason);
    } finally {
      busy.value = false;
    }
  }
  function beforeClose(event: BeforeUnloadEvent) {
    if (
      (systemSettings.value?.close_warning ?? true) &&
      (running.value || (!!status.value && !fresh.value))
    ) {
      event.preventDefault();
      event.returnValue = "";
    }
  }
  async function refreshAccount() {
    if (!session.value || !realAccount.value || loadingAccount.value) return;
    refreshAfterCancel = false;
    const epoch = generation;
    loadingAccount.value = true;
    lastAccountRefresh = Date.now();
    try {
      const result = await request<Dashboard>("dashboard");
      if (alive && epoch === generation && session.value) {
        dashboard.value = result;
        accountError.value = result.account.available ? "" : "QMT账户数据暂不可用";
      }
    } catch (reason) {
      if (alive && epoch === generation) {
        dashboard.value = null;
        accountError.value = errorText(reason);
        if (reason instanceof NodeApiError && reason.status === 401) failed(reason);
      }
    } finally {
      loadingAccount.value = false;
      if (alive && refreshAfterCancel) void refreshAccount();
    }
  }
  async function cancelOrder(clientOrderId: string) {
    const order = account.value?.orders.find((item) => item.client_order_id === clientOrderId);
    if (
      !canControl.value ||
      !accountFresh.value ||
      account.value?.truncated ||
      !order?.owned_by_system ||
      !order.cancelable ||
      order.cancel_requested ||
      order.remaining <= 0
    )
      return;
    busy.value = true;
    error.value = message.value = "";
    const epoch = generation;
    try {
      await request("orders/cancel", "POST", { client_order_id: clientOrderId });
      if (!alive || epoch !== generation) return;
      message.value = "撤单请求已提交，请等待券商确认，期间仍可能成交。";
    } catch (reason) {
      if (alive && epoch === generation) failed(reason);
    } finally {
      if (alive && epoch === generation && session.value) {
        refreshAfterCancel = true;
        await refreshAccount();
      }
      busy.value = false;
    }
  }
  function applyStatus(value: NodeStatus) {
    const key = pendingKey(value.node_id, value.account_id);
    if (trackingKey !== key) {
      clearPrivate();
      trackingKey = key;
      try {
        pendingId.value = readPending(key);
        storageFailed.value = false;
      } catch {
        storageFailed.value = true;
      }
    }
    status.value = value;
    const automatic = value.components.engine.data?.daily_auto;
    if (session.value && automatic && typeof automatic === "object")
      dailyAuto.value = automatic as DailyAutoState;
    receivedAt.value = Date.now();
    disconnected.value = false;
  }
  async function refreshStatus() {
    if (loadingStatus) return;
    loadingStatus = true;
    try {
      const result = await request<NodeStatus>("status");
      if (alive) applyStatus(result);
    } catch {
      if (alive) disconnected.value = true;
    } finally {
      loadingStatus = false;
    }
  }
  function closeStream() {
    events?.close();
    events = undefined;
  }
  function openStream() {
    if (!alive || events || streamFailed.value || document.visibilityState === "hidden") return;
    events = new EventSource("/api/v2/node/status-stream");
    events.onmessage = (event) => {
      try {
        if (alive) applyStatus(JSON.parse(event.data) as NodeStatus);
      } catch {
        disconnected.value = true;
      }
    };
    events.onerror = () => {
      streamFailed.value = true;
      closeStream();
    };
  }
  async function retry() {
    streamFailed.value = false;
    await refreshStatus();
    openStream();
    await refreshAccount();
  }
  function visibility() {
    if (document.visibilityState === "hidden") closeStream();
    else {
      void refreshStatus();
      openStream();
      void refreshAccount();
    }
  }
  async function restore() {
    const epoch = generation;
    try {
      const result = await request<Session>("session");
      if (alive && epoch === generation) {
        session.value = result;
        await loadSettings();
        await refreshAccount();
      }
    } catch {
      /* Fresh pages may legitimately have no session. */
    }
  }
  async function login() {
    if (busy.value || !password.value) return;
    busy.value = true;
    error.value = "";
    const secret = password.value;
    password.value = "";
    const epoch = ++generation;
    try {
      const result = await request<Session>("session", "POST", {
        password: secret,
        start_board_sell: true,
      });
      if (alive && epoch === generation) {
        session.value = result;
        await loadSettings();
        await refreshAccount();
      }
    } catch (reason) {
      if (alive && epoch === generation) failed(reason);
    } finally {
      busy.value = false;
    }
  }
  async function logout() {
    if (busy.value) return;
    busy.value = true;
    try {
      await request("session", "DELETE");
      clearPrivate();
    } catch (reason) {
      failed(reason);
    } finally {
      busy.value = false;
    }
  }
  async function saveSettings(value: TradingSettingsValues) {
    if (busy.value || !session.value) return;
    const issues = validateSettings(value);
    if (issues.length) {
      error.value = issues.map((item) => item.message).join(" ");
      return;
    }
    busy.value = true;
    controlEpoch++;
    dailyAutoPreview.value = null;
    error.value = message.value = "";
    const epoch = generation;
    try {
      const result = await request<SettingsResponse>("trading-settings", "POST", {
        expected_version: settings.value?.draft?.version ?? 0,
        settings: value,
      });
      if (alive && epoch === generation) {
        settings.value = result;
        message.value = result.restart_required
          ? dailyAuto.value?.supported
            ? "买入配置已保存，当前引擎需重新打开主程序加载；保存不会改变买卖开关。"
            : "设置已保存，重新打开主程序后生效。"
          : "买入配置已保存，后台将实时加载；买入和卖出开关保持原状态。";
      }
    } catch (reason) {
      if (alive && epoch === generation) failed(reason);
    } finally {
      busy.value = false;
    }
  }
  async function previewDailyAuto() {
    if (!canControl.value || busy.value) return;
    busy.value = true;
    error.value = message.value = "";
    view.value = "trading-settings";
    const epoch = generation;
    const operationEpoch = ++controlEpoch;
    try {
      const result = await request<DailyAutoPreview>("daily-auto/preview", "POST", {});
      if (alive && epoch === generation && operationEpoch === controlEpoch)
        dailyAutoPreview.value = result;
    } catch (reason) {
      if (alive && epoch === generation) failed(reason);
    } finally {
      busy.value = false;
    }
  }
  async function confirmDailyAuto(identity: string, text: string) {
    const current = dailyAutoPreview.value;
    if (
      !canControl.value ||
      !current ||
      current.preview_id !== identity ||
      current.confirmation_phrase !== text ||
      Date.parse(current.expires_at) <= Date.now()
    )
      return;
    busy.value = true;
    dailyAutoPreview.value = null;
    const epoch = generation;
    const operationEpoch = ++controlEpoch;
    try {
      const result = await request<DailyAutoState>("daily-auto/confirm", "POST", {
        preview_id: identity,
        phrase: text,
      });
      if (alive && epoch === generation && operationEpoch === controlEpoch) {
        dailyAuto.value = result;
        message.value = "持续自动买入已开启；之后由系统每天自动核验和等待正式名单。";
      }
    } catch (reason) {
      if (alive && epoch === generation && operationEpoch === controlEpoch) {
        // Read back an uncertain save; never send an enable confirmation twice.
        try {
          const actual = await request<DailyAutoState>("daily-auto");
          if (alive && epoch === generation && operationEpoch === controlEpoch) {
            dailyAuto.value = actual;
            if (actual.consent_id === identity && actual.enabled)
              message.value = "持续开关已保存并核对，无需重复开启。";
            else failed(reason);
          }
        } catch {
          if (alive && epoch === generation && operationEpoch === controlEpoch) failed(reason);
        }
      }
    } finally {
      busy.value = false;
    }
  }
  async function disableDailyAuto() {
    if (!session.value || halting.value) return;
    controlEpoch++;
    dailyAutoPreview.value = null;
    preview.value = null;
    halting.value = true;
    const epoch = generation;
    try {
      const result = await request<DailyAutoState>("daily-auto/disable", "POST", {});
      if (alive && epoch === generation) {
        dailyAuto.value = result;
        message.value = "持续自动买入已关闭；不会自行重新开启，已有委托不会自动撤销。";
      }
    } catch (reason) {
      if (alive && epoch === generation) failed(reason);
    } finally {
      halting.value = false;
    }
  }
  async function prepare(action: PreviewAction) {
    if (!canControl.value) return;
    busy.value = true;
    error.value = phrase.value = "";
    const epoch = generation;
    const operationEpoch = ++controlEpoch;
    try {
      const result = await request<Preview>("preview", "POST", { action });
      if (alive && epoch === generation && operationEpoch === controlEpoch) preview.value = result;
    } catch (reason) {
      if (alive && epoch === generation) failed(reason);
    } finally {
      busy.value = false;
    }
  }
  function acceptReceipt(result: Receipt) {
    if (result.request_id !== pendingId.value) throw new NodeApiError("CONTROL_RESULT_UNCERTAIN");
    if (result.status === "APPLIED" || result.status === "REJECTED") {
      writePending(trackingKey, "");
      pendingId.value = "";
      message.value =
        result.status === "APPLIED"
          ? "操作已生效。"
          : `操作未执行：${result.reason_code ?? "请重新核验"}`;
    } else message.value = "操作处理中，正在等待引擎结果。";
  }
  async function confirm() {
    const current = preview.value;
    if (
      !canControl.value ||
      !current ||
      phrase.value !== current.confirmation_phrase ||
      Date.parse(current.request.deadline) <= Date.now()
    )
      return;
    try {
      writePending(trackingKey, current.request.request_id);
    } catch {
      storageFailed.value = true;
      return;
    }
    pendingId.value = current.request.request_id;
    preview.value = null;
    phrase.value = "";
    busy.value = true;
    const epoch = generation;
    try {
      const result = await request<Receipt>("confirm", "POST", {
        preview_id: current.preview_id,
        phrase: current.confirmation_phrase,
      });
      if (!alive || epoch !== generation) return;
      acceptReceipt(result);
      await refreshStatus();
    } catch (reason) {
      if (alive && epoch === generation) failed(reason);
    } finally {
      busy.value = false;
    }
  }
  async function lookup() {
    if (!session.value || busy.value || !pendingId.value) return;
    busy.value = true;
    const epoch = generation;
    try {
      const result = await request<Receipt>("result", "POST", { request_id: pendingId.value });
      if (!alive || epoch !== generation) return;
      acceptReceipt(result);
      await refreshStatus();
    } catch (reason) {
      if (alive && epoch === generation) failed(reason);
    } finally {
      busy.value = false;
    }
  }
  async function control(action: "HALT" | "RECOVER") {
    if (!session.value || halting.value || (busy.value && action !== "HALT")) return;
    controlEpoch++;
    dailyAutoPreview.value = null;
    if (action === "HALT") halting.value = true;
    preview.value = null;
    busy.value = true;
    const epoch = generation;
    try {
      const result = await request<Receipt>("control", "POST", { action });
      if (!alive || epoch !== generation) return;
      message.value =
        action === "HALT" && result.status === "APPLIED"
          ? "已停止自动发单；在途委托仍需等待券商确认。"
          : "已提交账户核验。";
      await refreshStatus();
    } catch (reason) {
      if (alive && epoch === generation) failed(reason);
    } finally {
      busy.value = false;
      halting.value = false;
    }
  }
  onMounted(async () => {
    window.addEventListener("beforeunload", beforeClose);
    document.addEventListener("visibilitychange", visibility);
    await refreshStatus();
    openStream();
    await restore();
    timer = setInterval(() => {
      now.value = Date.now();
      if (
        settings.value?.application?.supported &&
        settings.value.application.active_version !== settings.value.draft?.version &&
        !busy.value &&
        session.value
      )
        void loadSettings();
      if (document.visibilityState !== "hidden" && Date.now() - lastAccountRefresh >= 5000)
        void refreshAccount();
    }, 1000);
  });
  onBeforeUnmount(() => {
    window.removeEventListener("beforeunload", beforeClose);
    alive = false;
    lifecycle.abort();
    closeStream();
    clearInterval(timer);
    document.removeEventListener("visibilitychange", visibility);
  });
  return {
    view,
    status,
    session,
    dashboard,
    settings,
    systemSettings,
    dailyAuto,
    dailyAutoPreview,
    previewDailyAuto,
    confirmDailyAuto,
    disableDailyAuto,
    saveSystemSettings,
    saveAccessToken,
    changePassword,
    password,
    error,
    accountError,
    message,
    busy,
    loadingAccount,
    settingsLoaded,
    halting,
    streamFailed,
    disconnected,
    preview,
    phrase,
    pendingId,
    storageFailed,
    now,
    engine,
    fresh,
    realAccount,
    unconfigured,
    running,
    buys,
    displayBuys,
    displaySnapshot,
    buyFactsAvailable,
    account,
    accountFresh,
    canControl,
    startReason,
    refreshAccount,
    cancelOrder,
    retry,
    login,
    logout,
    saveSettings,
    prepare,
    confirm,
    lookup,
    control,
  };
}
