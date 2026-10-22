import type { LocalControlAction } from "../models/controls";

export interface OperatorSession {
  readonly actor_id: string;
  readonly issued_at: string;
  readonly expires_at: string;
  readonly csrf_token: string | null;
}

export interface ControlImpactFacts {
  readonly active_orders: number;
  readonly active_buy_orders: number;
  readonly active_sell_orders: number;
  readonly open_positions: number;
  readonly qmt_connected: boolean;
  readonly central_connected: boolean;
  readonly reconciliation_complete: boolean;
  readonly unknown_order: boolean;
}

export interface ControlImpactPreview {
  readonly preview_id: string;
  readonly actor_id: string;
  readonly node_id: string;
  readonly account_id: string;
  readonly account_alias: string;
  readonly profile: string;
  readonly action: string;
  readonly reason_code: string;
  readonly requested_at: string;
  readonly expires_at: string;
  readonly expected_state: string;
  readonly requested_state: string;
  readonly cancel_active_orders: boolean | null;
  readonly impact_hash: string;
  readonly confirmation_phrase: string;
  readonly warnings: readonly string[];
  readonly facts: ControlImpactFacts;
}

export interface ControlConfirmation {
  readonly confirmation_id: string;
  readonly actor_id: string;
  readonly confirmed_at: string;
  readonly impact_hash: string;
}

export interface ControlCommandTemplate {
  readonly action: string;
  readonly actor_id: string;
  readonly reason_code: string;
  readonly requested_at: string;
  readonly expected_state: string;
  readonly confirmation: ControlConfirmation | null;
  readonly cancel_active_orders: boolean | null;
}

export interface ConfirmedControl {
  readonly authorization_token: string;
  readonly preview_id: string;
  readonly command_template: ControlCommandTemplate;
}

export interface LocalControlReceipt {
  readonly command_id: string;
  readonly action: string;
  readonly actor_id: string;
  readonly status: string;
  readonly final: boolean;
  readonly current_state: string | null;
  readonly rejection_code: string | null;
  readonly blockers: readonly string[];
  readonly error_code: string | null;
  readonly retryable: boolean;
  readonly requires_reconciliation: boolean;
  readonly status_url: string;
  readonly poll_after_ms: number | null;
}

export interface LocalConsoleGateway {
  getSession(): Promise<OperatorSession | null>;
  login(password: string): Promise<OperatorSession>;
  logout(): Promise<void>;
  createControlPreview(request: ControlPreviewRequest): Promise<ControlImpactPreview>;
  confirmControlPreview(previewId: string, phrase: string): Promise<ConfirmedControl>;
  submitConfirmedControl(confirmed: ConfirmedControl): Promise<LocalControlReceipt>;
  getControlReceipt(statusUrl: string): Promise<LocalControlReceipt>;
}

export interface ControlPreviewRequest {
  readonly action: LocalControlAction;
  readonly reasonCode: string;
  readonly cancelActiveOrders: boolean | null;
}

export class LocalConsoleApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly retryAfterSeconds: number | null = null,
  ) {
    super(message);
    this.name = "LocalConsoleApiError";
  }
}

type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
type IdentifierFactory = (prefix: "cmd" | "corr") => string;

const CSRF_HEADER = "X-CSRF-Token";
const CONTROL_AUTHORIZATION_HEADER = "X-Control-Authorization";

export class LocalConsoleApi implements LocalConsoleGateway {
  private csrfToken: string | null = null;

  constructor(
    private readonly fetcher: Fetcher = globalThis.fetch.bind(globalThis),
    private readonly identifierFactory: IdentifierFactory = createIdentifier,
  ) {}

  async getSession(): Promise<OperatorSession | null> {
    try {
      const payload = await this.request("/api/v1/local/session", { method: "GET" });
      return decodeSession(payload);
    } catch (error: unknown) {
      if (error instanceof LocalConsoleApiError && error.status === 401) {
        this.csrfToken = null;
        return null;
      }
      throw error;
    }
  }

  async login(password: string): Promise<OperatorSession> {
    if (password.length === 0) {
      throw new LocalConsoleApiError(0, "OPERATOR_PASSWORD_REQUIRED", "请输入操作员密码");
    }
    const payload = await this.request("/api/v1/local/session", {
      method: "POST",
      body: JSON.stringify({ password }),
    });
    const session = decodeSession(payload);
    if (!session.csrf_token) {
      throw new LocalConsoleApiError(0, "OPERATOR_CSRF_MISSING", "登录响应缺少操作授权凭据");
    }
    this.csrfToken = session.csrf_token;
    return session;
  }

  async logout(): Promise<void> {
    await this.mutatingRequest("/api/v1/local/session", { method: "DELETE" });
    this.csrfToken = null;
  }

  async createControlPreview(request: ControlPreviewRequest): Promise<ControlImpactPreview> {
    const payload = await this.mutatingRequest("/api/v1/local/control-previews", {
      method: "POST",
      body: JSON.stringify({
        action: request.action,
        reason_code: request.reasonCode,
        cancel_active_orders: request.cancelActiveOrders,
      }),
    });
    return decodePreview(payload);
  }

  async confirmControlPreview(previewId: string, phrase: string): Promise<ConfirmedControl> {
    const payload = await this.mutatingRequest(
      `/api/v1/local/control-previews/${encodeURIComponent(previewId)}/confirm`,
      { method: "POST", body: JSON.stringify({ phrase }) },
    );
    return decodeConfirmedControl(payload);
  }

  async submitConfirmedControl(confirmed: ConfirmedControl): Promise<LocalControlReceipt> {
    const command = {
      ...confirmed.command_template,
      command_id: this.identifierFactory("cmd"),
      correlation_id: this.identifierFactory("corr"),
    };
    const payload = await this.mutatingRequest("/api/v1/local/controls", {
      method: "POST",
      body: JSON.stringify(command),
      headers: {
        [CONTROL_AUTHORIZATION_HEADER]: confirmed.authorization_token,
      },
    });
    return decodeReceipt(payload);
  }

  async getControlReceipt(statusUrl: string): Promise<LocalControlReceipt> {
    if (!statusUrl.startsWith("/api/v1/local/controls/")) {
      throw new LocalConsoleApiError(0, "CONTROL_STATUS_URL_INVALID", "控制结果地址无效");
    }
    return decodeReceipt(await this.request(statusUrl, { method: "GET" }));
  }

  private mutatingRequest(path: string, init: RequestInit): Promise<unknown> {
    if (!this.csrfToken) {
      throw new LocalConsoleApiError(401, "OPERATOR_REAUTH_REQUIRED", "请先验证操作员身份");
    }
    return this.request(path, {
      ...init,
      headers: {
        ...headersRecord(init.headers),
        [CSRF_HEADER]: this.csrfToken,
      },
    });
  }

  private async request(path: string, init: RequestInit): Promise<unknown> {
    const response = await this.fetcher(path, {
      ...init,
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        ...(init.body === undefined ? {} : { "Content-Type": "application/json" }),
        ...headersRecord(init.headers),
      },
    });
    if (!response.ok) {
      throw await decodeError(response);
    }
    if (response.status === 204) {
      return null;
    }
    return response.json() as Promise<unknown>;
  }
}

function headersRecord(headers: HeadersInit | undefined): Record<string, string> {
  if (headers === undefined) return {};
  return Object.fromEntries(new Headers(headers).entries());
}

function createIdentifier(prefix: "cmd" | "corr"): string {
  return `${prefix}_${crypto.randomUUID().replaceAll("-", "")}`;
}

async function decodeError(response: Response): Promise<LocalConsoleApiError> {
  let code = "LOCAL_API_ERROR";
  let message = `本地控制服务返回 ${response.status}`;
  try {
    const payload = await response.json();
    const root = record(payload);
    const detail = record(root.detail);
    code = optionalString(detail.code) ?? code;
    message = optionalString(detail.message) ?? message;
  } catch {
    // The stable status still gives the operator an actionable boundary error.
  }
  const retryAfter = response.headers.get("retry-after");
  const retryAfterSeconds = retryAfter === null ? null : Number.parseInt(retryAfter, 10);
  return new LocalConsoleApiError(
    response.status,
    code,
    message,
    Number.isFinite(retryAfterSeconds) ? retryAfterSeconds : null,
  );
}

function decodeSession(payload: unknown): OperatorSession {
  const value = record(payload);
  return {
    actor_id: requiredString(value.actor_id, "actor_id"),
    issued_at: requiredString(value.issued_at, "issued_at"),
    expires_at: requiredString(value.expires_at, "expires_at"),
    csrf_token: optionalString(value.csrf_token),
  };
}

function decodePreview(payload: unknown): ControlImpactPreview {
  const value = record(payload);
  const facts = record(value.facts);
  const warnings = value.warnings;
  if (!Array.isArray(warnings) || !warnings.every((item) => typeof item === "string")) {
    throw invalidResponse("warnings");
  }
  return {
    preview_id: requiredString(value.preview_id, "preview_id"),
    actor_id: requiredString(value.actor_id, "actor_id"),
    node_id: requiredString(value.node_id, "node_id"),
    account_id: requiredString(value.account_id, "account_id"),
    account_alias: requiredString(value.account_alias, "account_alias"),
    profile: requiredString(value.profile, "profile"),
    action: requiredString(value.action, "action"),
    reason_code: requiredString(value.reason_code, "reason_code"),
    requested_at: requiredString(value.requested_at, "requested_at"),
    expires_at: requiredString(value.expires_at, "expires_at"),
    expected_state: requiredString(value.expected_state, "expected_state"),
    requested_state: requiredString(value.requested_state, "requested_state"),
    cancel_active_orders: optionalBoolean(value.cancel_active_orders),
    impact_hash: requiredString(value.impact_hash, "impact_hash"),
    confirmation_phrase: requiredString(value.confirmation_phrase, "confirmation_phrase"),
    warnings,
    facts: {
      active_orders: requiredNumber(facts.active_orders, "active_orders"),
      active_buy_orders: requiredNumber(facts.active_buy_orders, "active_buy_orders"),
      active_sell_orders: requiredNumber(facts.active_sell_orders, "active_sell_orders"),
      open_positions: requiredNumber(facts.open_positions, "open_positions"),
      qmt_connected: requiredBoolean(facts.qmt_connected, "qmt_connected"),
      central_connected: requiredBoolean(facts.central_connected, "central_connected"),
      reconciliation_complete: requiredBoolean(
        facts.reconciliation_complete,
        "reconciliation_complete",
      ),
      unknown_order: requiredBoolean(facts.unknown_order, "unknown_order"),
    },
  };
}

function decodeConfirmedControl(payload: unknown): ConfirmedControl {
  const value = record(payload);
  const template = record(value.command_template);
  const confirmationValue = template.confirmation;
  let confirmation: ControlConfirmation | null = null;
  if (confirmationValue !== null) {
    const item = record(confirmationValue);
    confirmation = {
      confirmation_id: requiredString(item.confirmation_id, "confirmation_id"),
      actor_id: requiredString(item.actor_id, "confirmation.actor_id"),
      confirmed_at: requiredString(item.confirmed_at, "confirmed_at"),
      impact_hash: requiredString(item.impact_hash, "confirmation.impact_hash"),
    };
  }
  return {
    authorization_token: requiredString(value.authorization_token, "authorization_token"),
    preview_id: requiredString(value.preview_id, "preview_id"),
    command_template: {
      action: requiredString(template.action, "action"),
      actor_id: requiredString(template.actor_id, "actor_id"),
      reason_code: requiredString(template.reason_code, "reason_code"),
      requested_at: requiredString(template.requested_at, "requested_at"),
      expected_state: requiredString(template.expected_state, "expected_state"),
      confirmation,
      cancel_active_orders: optionalBoolean(template.cancel_active_orders),
    },
  };
}

function decodeReceipt(payload: unknown): LocalControlReceipt {
  const value = record(payload);
  const blockers = value.blockers;
  if (!Array.isArray(blockers) || !blockers.every((item) => typeof item === "string")) {
    throw invalidResponse("blockers");
  }
  return {
    command_id: requiredString(value.command_id, "command_id"),
    action: requiredString(value.action, "action"),
    actor_id: requiredString(value.actor_id, "actor_id"),
    status: requiredString(value.status, "status"),
    final: requiredBoolean(value.final, "final"),
    current_state: optionalString(value.current_state),
    rejection_code: optionalString(value.rejection_code),
    blockers,
    error_code: optionalString(value.error_code),
    retryable: requiredBoolean(value.retryable, "retryable"),
    requires_reconciliation: requiredBoolean(
      value.requires_reconciliation,
      "requires_reconciliation",
    ),
    status_url: requiredString(value.status_url, "status_url"),
    poll_after_ms: optionalNumber(value.poll_after_ms),
  };
}

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw invalidResponse("object");
  }
  return value as Record<string, unknown>;
}

function requiredString(value: unknown, field: string): string {
  if (typeof value !== "string" || value.length === 0) throw invalidResponse(field);
  return value;
}

function optionalString(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== "string") throw invalidResponse("optional string");
  return value;
}

function requiredBoolean(value: unknown, field: string): boolean {
  if (typeof value !== "boolean") throw invalidResponse(field);
  return value;
}

function optionalBoolean(value: unknown): boolean | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== "boolean") throw invalidResponse("optional boolean");
  return value;
}

function requiredNumber(value: unknown, field: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) throw invalidResponse(field);
  return value;
}

function optionalNumber(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  return requiredNumber(value, "optional number");
}

function invalidResponse(field: string): LocalConsoleApiError {
  return new LocalConsoleApiError(
    0,
    "LOCAL_API_RESPONSE_INVALID",
    `本地控制服务响应字段无效: ${field}`,
  );
}

export const localConsoleApi: LocalConsoleGateway = new LocalConsoleApi();
