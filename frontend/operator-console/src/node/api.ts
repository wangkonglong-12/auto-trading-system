/** The engine is the authority. No credentials or control retries in storage. */
export type ComponentName = "engine" | "supervisor" | "agent";
export type PreviewAction = "CONFIRM_RECOVERY" | "ARM" | "RESUME";
export interface Observation {
  availability: "CURRENT" | "STALE" | "UNAVAILABLE";
  age_seconds: number | null;
  observed_at: string | null;
  error: string | null;
  data: Record<string, unknown> | null;
}
export interface NodeStatus {
  node_id: string;
  account_id: string;
  broker: "MOCK" | "QMT_READ_ONLY" | "QMT_TRADING";
  real_trading_enabled: boolean;
  strategy_status: string;
  operator_access_configured: boolean;
  qmt_setup_issue?:
    | "QMT_SDK_TEMPORARY"
    | "QMT_SDK_MISSING"
    | "QMT_CONFIG_MISSING"
    | "QMT_CONFIG_INVALID"
    | "QMT_SETTINGS_INVALID";
  components: Record<ComponentName, Observation>;
}
export interface Session {
  actor_id: string;
  expires_at: string;
  csrf_token: string;
}
export interface SystemSettingsValues {
  quant_scheme: "http" | "https";
  version: number;
  close_warning: boolean;
  quant_host: string;
  quant_port: number;
}
export interface SystemSettingsResponse extends SystemSettingsValues {
  access_token_configured?: boolean;
  active_origin: string;
  restart_required: boolean;
}
export interface Preview {
  preview_id: string;
  confirmation_phrase: string;
  actor_id: string;
  account_id: string;
  trade_date: string;
  permission_epoch: number;
  request: {
    request_id: string;
    action: PreviewAction;
    deadline: string;
    engine_boot_id: string;
    expected_state_version: number;
    grant: Record<string, unknown> | null;
  };
  execution_scope: Record<string, unknown> | null;
  installed_limits: Record<string, unknown>;
  recovery: Record<string, unknown>;
}
export interface Receipt {
  status: string;
  action?: string;
  request_id?: string;
  state_version?: number;
  reason_code?: string;
}
export interface DailyAutoState {
  mode?: "DEBUG_ONCE" | "CONTINUOUS";
  supported: boolean;
  enabled: boolean;
  configured?: boolean;
  consent_id?: string | null;
  state: string;
  reason?: string;
  settings_hash?: string;
  calendar_through?: string;
  recovery_issues?: string[];
  pause_reason?: string;
  limits?: {
    daily_value: string;
    symbol_value: string;
    order_value: string;
    daily_symbols: number | null;
    positions: number;
    position_pct: string;
    daily_loss: string;
  };
}
export interface DailyAutoPreview {
  preview_id: string;
  confirmation_phrase: string;
  expires_at: string;
  settings_hash: string;
  state: DailyAutoState;
  buy_only: boolean;
}
export interface FieldIssue {
  field: string;
  message: string;
}
export class NodeApiError extends Error {
  constructor(
    public readonly code: string,
    public readonly status = 0,
    public readonly issues: FieldIssue[] = [],
  ) {
    super(code);
  }
}

export async function nodeRequest<T>(
  path: string,
  method = "GET",
  body?: object,
  csrf?: string,
  signal?: AbortSignal,
): Promise<T> {
  const timeout = new AbortController();
  const timer = setTimeout(() => timeout.abort(), 18000);
  const abort = () => timeout.abort();
  signal?.addEventListener("abort", abort, { once: true });
  if (signal?.aborted) timeout.abort();
  try {
    const response = await fetch(`/api/v2/node/${path}`, {
      method,
      credentials: "same-origin",
      cache: "no-store",
      signal: timeout.signal,
      headers: {
        ...(body ? { "Content-Type": "application/json" } : {}),
        ...(csrf ? { "X-CSRF-Token": csrf } : {}),
      },
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
    const value = await response.json();
    if (!response.ok) {
      throw new NodeApiError(
        typeof value.code === "string" ? value.code : "REQUEST_FAILED",
        response.status,
        Array.isArray(value.issues)
          ? value.issues
              .filter(
                (item: unknown): item is FieldIssue =>
                  typeof item === "object" &&
                  item !== null &&
                  "field" in item &&
                  typeof item.field === "string" &&
                  "message" in item &&
                  typeof item.message === "string",
              )
              .slice(0, 20)
          : [],
      );
    }
    return value as T;
  } catch (error) {
    if (error instanceof NodeApiError) throw error;
    // A timed-out mutation may already be applied. Callers retain its original
    // request identity for explicit lookup, never silently repeat confirmation.
    throw new NodeApiError("CONNECTION_LOST_OR_TIMEOUT");
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}

export function errorText(error: unknown): string {
  const code = error instanceof NodeApiError ? error.code : "REQUEST_FAILED";
  if (code === "INVALID_ARGUMENTS" && error instanceof NodeApiError && error.issues.length)
    return error.issues.map((item) => item.message).join("；");
  const known: Record<string, string> = {
    QUANT_ACCESS_UNAVAILABLE: "接口令牌无法读取或保存，请检查本机凭据存储。",
    PASSWORD_CURRENT_INVALID: "当前密码不正确，请核对后重试。",
    PASSWORD_SAVE_FAILED: "密码保存失败，原密码仍然有效，请检查本机凭据存储。",
    PASSWORD_CHANGE_UNAVAILABLE: "当前运行环境尚未开放修改密码。",
    SYSTEM_SETTINGS_STORE_INVALID: "系统设置无法读取，请检查本机存储。",
    SYSTEM_SETTINGS_SAVE_FAILED: "系统设置保存失败，请检查本机存储。",
    SYSTEM_SETTINGS_VERSION_CONFLICT: "系统设置已被修改，请重新登录或刷新后重试。",
    OPERATOR_LOGIN_FAILED: "口令不正确，请核对后重试。",
    OPERATOR_SESSION_REQUIRED: "请先登录本机操作员。",
    OPERATOR_SESSION_EXPIRED: "登录已失效，请重新登录。",
    OPERATOR_SESSION_INVALID: "登录已失效，请重新登录。",
    OPERATOR_LOGIN_RATE_LIMITED: "登录尝试过多，请稍后再试。",
    PIPE_UNAVAILABLE: "目标进程未连接，请检查进程启动情况。",
    CONSOLE_BUSY: "控制通道正忙，请等待当前操作完成。",
    PREVIEW_EXPIRED_OR_FENCED: "预览已过期或状态已变化，请重新核验。",
    CONNECTION_LOST_OR_TIMEOUT: "连接中断或超时；操作结果尚未确认。",
    CONTROL_RESULT_UNKNOWN: "本次引擎尚无该请求的可确认结果，请核对状态，勿重复授权。",
    CONFIRMATION_MISMATCH: "确认短语不一致。",
    RULE_VERSION_CONFLICT: "规则已在另一处修改，请刷新页面后核对新版内容。",
    RULE_STORE_INVALID: "规则文件无法读取，请暂停操作并检查本机存储。",
    RULE_SAVE_FAILED: "规则保存失败，请检查本机存储后重试。",
    TRADING_SETTINGS_VERSION_CONFLICT: "交易设置已在另一处修改，请刷新后核对新版内容。",
    TRADING_SETTINGS_STORE_INVALID: "交易设置文件无法读取，请暂停操作并检查本机存储。",
    TRADING_SETTINGS_SAVE_FAILED: "交易设置保存失败，请检查本机存储后重试。",
    TRADING_SETTINGS_REQUIRED: "尚未完成交易设置，自动交易保持锁定。请先保存交易设置。",
    QMT_CONNECTION_REQUIRED: "请先连接本机 QMT，再保存交易设置。",
    TRADING_SETTINGS_RESTART_REQUIRED:
      "当前引擎尚未加载这版交易设置。请关闭自动交易系统主窗口并重新打开后再启用。",
    DAILY_AUTO_UNAVAILABLE: "当前连接未加载持续自动买入，请重新打开主程序加载新版本。",
    DAILY_AUTO_NOT_READY:
      "持续模式尚未就绪，请确认三批买入设置已加载、卖出留空且执行日历覆盖下一交易日。",
    DAILY_AUTO_SAVE_FAILED: "保存持续开关失败，未确认开启。请检查本机存储及交易设置。",
    DAILY_AUTO_OPERATOR_DISABLED: "持续自动买入已关闭。如需手动交易，请先重新核验账户。",
    DAILY_AUTO_SETTINGS_CHANGED:
      "交易设置已变更，持续自动买入已关闭。请重新打开主程序加载新设置后，再确认开启。",
    DEBUG_PICK_UNAVAILABLE: "当前节点未接入调试选股，请重启加载新版本并检查尾盘设置。",
    DEBUG_PICK_ALREADY_USED: "今天已有调试选股记录，请查看该股票的执行结果。",
    DEBUG_PICK_DAY_LOCKED: "今天已使用调试选股，不能再开启持续正式选股买入。",
    DEBUG_PICK_OUTSIDE_WINDOW: "请在交易日14:40至14:55启动调试选股买入。",
    DEBUG_PICK_DISABLE_AUTO_FIRST: "请先关闭持续自动买入，再进行调试。",
    DEBUG_PICK_FORMAL_CONFLICT: "已有正式选股目标、未完成委托或交易正在处理中，暂不能进入调试。",
    DEBUG_PICK_BOARD_BLOCKED: "该股票不在当前允许买入的板块范围内。",
    DEBUG_PICK_CONFIRMATION_INVALID: "确认已过期、账户状态变化或确认文字不一致，请重新预览。",
    DAILY_AUTO_CONFIRMATION_INVALID: "确认已过期、设置已变化或确认文字不一致，请重新预览。",
    DAILY_AUTO_STATE_CHANGED:
      "确认期间系统发生安全暂停或重新核验，本次未开启。请处理页面上的暂停原因后重新预览。",
    DAILY_AUTO_MARKET_UNAVAILABLE:
      "行情处理尚未恢复，本次未开启。请先处理行情异常，再重新核验账户。",
    QMT_TICK_CLOCK_SKEW:
      "QMT行情与电脑时钟偏差超过3秒，或等待队列已满，请检查时钟和行情连接后重新核验。",
    QMT_TICK_CALLBACK_INVALID: "连续收到异常行情，系统已暂停；行情恢复后请重新核验账户。",
    QMT_TICK_DELIVERY_FAILED: "行情交付失败，系统已暂停，请检查运行记录。",
    DAILY_AUTO_ALREADY_ENABLED:
      "持续自动买入已开启，无需再做单次授权。要改为手工模式请先关闭持续开关。",
    ORDER_NOT_CANCELABLE: "该委托已结束、来自外部，或券商尚未返回可撤的委托号，请刷新后核对。",
    ORDER_CANCEL_NOT_READY: "本次撤单未提交，账户正在核对或状态已变化，请刷新后重试。",
    ORDER_CANCEL_RESULT_UNKNOWN: "本次撤单结果待核对，请等待查询结果，勿重复提交。",
    MANUAL_BUY_UNAVAILABLE: "当前运行环境尚未开放手动买入，请使用已加载新版的交易连接。",
    MANUAL_BUY_NOT_READY: "请先完成账户核验，并确认交易连接和安全状态正常。",
    MANUAL_BUY_BUSY: "账户核对或上一笔委托正在处理中，本次尚未提交，请稍后再次确认。",
    MANUAL_BUY_SETTINGS_CHANGED: "交易设置尚未加载，请重新打开主程序后操作。",
    MANUAL_BUY_INSTRUMENT_UNAVAILABLE: "未能核实股票名称或可交易规则，请检查代码和QMT状态。",
    MANUAL_BUY_PREVIEW_NOT_FOUND: "本次核对已失效，请重新填写。",
    MANUAL_BUY_PREVIEW_EXPIRED: "本次核对已过期，请重新填写。",
    MANUAL_BUY_REJECTED: "未提交委托，请检查账户、交易时段、资金限额和是否已有同股买单。",
    MANUAL_BUY_RESULT_UNKNOWN: "尚未查到本次委托记录，请刷新列表核对，勿直接重发。",
    MANUAL_SELL_UNAVAILABLE: "当前运行环境尚未加载手动卖出。",
    MANUAL_SELL_NOT_READY: "账户核验或系统状态尚未准备好卖出。",
    MANUAL_SELL_OUTSIDE_WINDOW: "当前不在卖出交易时段。",
    MANUAL_SELL_INSUFFICIENT: "本系统当前可卖数量不足，请刷新持仓后核对。",
    MANUAL_SELL_QUANTITY_INVALID: "卖出数量不符合申报规则；不足一手的剩余股份需一次卖完。",
    MANUAL_SELL_TOO_MANY_CHILDREN: "本次需要拆分的委托过多，请减少卖出数量。",
    MANUAL_SELL_ORDER_CONFLICT: "该股已有未完成委托，请先处理原委托。",
    MANUAL_SELL_BUSY: "该股的卖出操作正在处理，请勿重复提交。",
    MANUAL_SELL_SETTINGS_CHANGED: "交易设置已变更，请重新加载主程序后核对。",
    MANUAL_SELL_INSTRUMENT_UNAVAILABLE: "股票申报规则或账户数据暂不可读取，请稍后核对。",
    MANUAL_SELL_PREVIEW_EXPIRED: "本次卖出核对已过期，请重新核对。",
    MANUAL_SELL_PREVIEW_NOT_FOUND: "本次卖出核对已失效或属于其他会话。",
    MANUAL_SELL_PREVIEW_STALE: "持仓或申报条件已变化，请重新核对卖出。",
    MANUAL_SELL_RESULT_UNKNOWN: "卖出结果尚不能确认，请查询原操作，勿重复提交。",
    LIQUIDATION_DISABLED: "紧急清仓尚未在交易设置中启用。",
    LIQUIDATION_UNAVAILABLE: "当前不是QMT实盘运行环境，无法执行紧急清仓。",
    NO_STRATEGY_POSITIONS: "当前没有本系统可识别的策略持仓。",
    ACCOUNT_QUERY_BUSY: "券商查询通道正在处理其他请求，请稍后重新检查账户。",
    ACCOUNT_QUERY_FAILED: "本次读取QMT账户失败，请检查交易端连接后重试。",
    RECOVERY_FAILED: "后台账户核验失败，请重新核验并查看具体原因。",
    QMT_TIMESTAMP_OUTSIDE_TRADE_DATE: "QMT返回了不属于当前交易日的委托或成交，需要核对日期和归属。",
    QMT_TIMESTAMP_INVALID: "QMT返回的委托或成交时间无效，账户核验已停止。",
    QMT_ACCOUNT_NOT_READY: "QMT账户尚未就绪，请确认交易端已登录且账户连接正常。",
    QMT_COMMAND_UNAVAILABLE: "QMT交易查询会话不可用，请确认交易端连接后重新打开主程序。",
    QMT_COMMAND_DISCONNECTED: "QMT交易连接已断开，需要重新建立连接。",
    QMT_ACCOUNT_STATUS_UNKNOWN: "未能读取QMT账户状态，请检查交易端连接。",
    QMT_ACCOUNT_QUERY_INVALID: "QMT返回的资金或持仓数据未通过校验。",
    QMT_ORDER_QUERY_INVALID: "QMT返回的委托数据未通过校验。",
    QMT_TRADE_QUERY_INVALID: "QMT返回的成交数据未通过校验。",
    RECOVERY_BROKER_QUERY_FAILED: "读取券商账户、委托或成交时失败。",
    RECOVERY_JOURNAL_SCAN_FAILED: "本地交易记录未通过恢复校验，请保留日志排查。",
    RECOVERY_HISTORY_EVIDENCE_INCOMPLETE:
      "历史委托缺少终态回报或完整成交明细，无法确认剩余持仓，请核对具体委托。",
    RECOVERY_HISTORY_FACT_CONFLICT:
      "历史委托或成交的账户、编号、方向、数量存在冲突，请保留记录核对。",
    RECOVERY_HISTORY_SCOPE_UNRESOLVED: "部分历史交易缺少可验证的策略或持仓批次归属，请核对后恢复。",
    RECOVERY_OPENING_REFERENCE_FAILED:
      "跨日持仓封存记录缺失、过期或校验失败。操作台仍可使用；请核对前一交易日记录后再恢复交易。",
    RECOVERY_RECONCILE_FAILED: "券商数据与本地交易记录核对失败。",
    RECOVERY_CHECKPOINT_WRITE_FAILED: "账户核验结果保存失败，请检查本机存储。",
    RECOVERY_ACCOUNT_PROJECTION_FAILED: "账户、委托与持仓归属未通过一致性校验。",
    INVALID_ARGUMENTS: "提交内容未通过检查，请核对必填项、金额关系和数值范围。",
  };
  return `${known[code] ?? "操作未完成，请根据原因码检查状态。"}（${code}）`;
}
