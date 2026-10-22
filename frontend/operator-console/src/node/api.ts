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
  components: Record<ComponentName, Observation>;
}
export interface Session {
  actor_id: string;
  expires_at: string;
  csrf_token: string;
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
    ORDER_NOT_CANCELABLE: "该委托已结束、来自外部，或券商尚未返回可撤的委托号，请刷新后核对。",
    LIQUIDATION_DISABLED: "紧急清仓尚未在交易设置中启用。",
    LIQUIDATION_UNAVAILABLE: "当前不是QMT实盘运行环境，无法执行紧急清仓。",
    NO_STRATEGY_POSITIONS: "当前没有本系统可识别的策略持仓。",
    ACCOUNT_QUERY_BUSY: "券商查询通道正在处理其他请求，请稍后重新检查账户。",
    ACCOUNT_QUERY_FAILED: "本次读取QMT账户失败，请检查交易端连接后重试。",
    INVALID_ARGUMENTS: "提交内容未通过检查，请核对必填项、金额关系和数值范围。",
  };
  return `${known[code] ?? "操作未完成，请根据原因码检查状态。"}（${code}）`;
}
