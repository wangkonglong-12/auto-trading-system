import { describe, expect, it, vi } from "vitest";

import { LocalConsoleApi, LocalConsoleApiError, type ConfirmedControl } from "./localConsoleApi";

type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

const sessionPayload = {
  actor_id: "operator_demo",
  issued_at: "2026-09-04T10:00:00+08:00",
  expires_at: "2026-09-04T11:00:00+08:00",
  csrf_token: "csrf-token-value",
};

const previewPayload = {
  preview_id: `preview_${"1".repeat(32)}`,
  actor_id: "operator_demo",
  node_id: "node-dev-01",
  account_id: "acct_demo_001",
  account_alias: "demo",
  profile: "READ_ONLY",
  action: "EMERGENCY_HALT",
  reason_code: "LOCAL_EMERGENCY_HALT",
  requested_at: "2026-09-04T10:00:00+08:00",
  expires_at: "2026-09-04T10:01:30+08:00",
  expected_state: "AUTO",
  requested_state: "HALT",
  cancel_active_orders: true,
  impact_hash: `sha256:${"a".repeat(64)}`,
  confirmation_phrase: "确认紧急停止",
  warnings: ["活动委托将撤销"],
  facts: {
    active_orders: 2,
    active_buy_orders: 1,
    active_sell_orders: 1,
    open_positions: 3,
    qmt_connected: true,
    central_connected: true,
    reconciliation_complete: true,
    unknown_order: false,
  },
};

const confirmedPayload: ConfirmedControl = {
  authorization_token: "authorization-token-value-123456789",
  preview_id: previewPayload.preview_id,
  command_template: {
    action: "EMERGENCY_HALT",
    actor_id: "operator_demo",
    reason_code: "LOCAL_EMERGENCY_HALT",
    requested_at: "2026-09-04T10:00:00+08:00",
    expected_state: "AUTO",
    confirmation: {
      confirmation_id: `confirm_${"2".repeat(32)}`,
      actor_id: "operator_demo",
      confirmed_at: "2026-09-04T10:00:01+08:00",
      impact_hash: previewPayload.impact_hash,
    },
    cancel_active_orders: true,
  },
};

function receiptPayload(final: boolean) {
  return {
    command_id: "cmd_12345678",
    action: "EMERGENCY_HALT",
    actor_id: "operator_demo",
    status: final ? "APPLIED" : "ACCEPTED",
    final,
    current_state: final ? "HALT" : null,
    rejection_code: null,
    blockers: [],
    error_code: null,
    retryable: false,
    requires_reconciliation: false,
    status_url: "/api/v1/local/controls/cmd_12345678",
    poll_after_ms: final ? null : 1,
  };
}

function jsonResponse(payload: unknown, status = 200, headers?: HeadersInit): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json", ...Object.fromEntries(new Headers(headers)) },
  });
}

function queuedFetcher(...responses: Response[]): ReturnType<typeof vi.fn<Fetcher>> {
  const remaining = [...responses];
  return vi.fn<Fetcher>(async () => {
    const response = remaining.shift();
    if (!response) throw new Error("unexpected fetch");
    return response;
  });
}

describe("LocalConsoleApi", () => {
  it("runs the authenticated preview, confirmation, command and receipt workflow", async () => {
    const fetcher = queuedFetcher(
      jsonResponse(sessionPayload),
      jsonResponse(previewPayload),
      jsonResponse(confirmedPayload),
      jsonResponse(receiptPayload(false), 202),
      jsonResponse(receiptPayload(true)),
      new Response(null, { status: 204 }),
    );
    const api = new LocalConsoleApi(fetcher, (prefix) => `${prefix}_12345678`);

    const session = await api.login("operator-password");
    const preview = await api.createControlPreview({
      action: "EMERGENCY_HALT",
      reasonCode: "LOCAL_EMERGENCY_HALT",
      cancelActiveOrders: true,
    });
    const confirmed = await api.confirmControlPreview(
      preview.preview_id,
      preview.confirmation_phrase,
    );
    const accepted = await api.submitConfirmedControl(confirmed);
    const final = await api.getControlReceipt(accepted.status_url);
    await api.logout();

    expect(session.actor_id).toBe("operator_demo");
    expect(preview.facts.active_orders).toBe(2);
    expect(preview.cancel_active_orders).toBe(true);
    expect(final).toMatchObject({ final: true, current_state: "HALT", status: "APPLIED" });

    const previewHeaders = new Headers(fetcher.mock.calls[1]?.[1]?.headers);
    expect(previewHeaders.get("x-csrf-token")).toBe("csrf-token-value");
    expect(fetcher.mock.calls[1]?.[1]?.credentials).toBe("same-origin");
    const commandInit = fetcher.mock.calls[3]?.[1];
    const commandHeaders = new Headers(commandInit?.headers);
    expect(commandHeaders.get("x-control-authorization")).toBe(
      confirmedPayload.authorization_token,
    );
    expect(JSON.parse(String(commandInit?.body))).toMatchObject({
      command_id: "cmd_12345678",
      correlation_id: "corr_12345678",
      expected_state: "AUTO",
    });
  });

  it("restores a read session without treating it as mutation authority", async () => {
    const restored = { ...sessionPayload, csrf_token: null };
    const fetcher = queuedFetcher(jsonResponse(restored));
    const api = new LocalConsoleApi(fetcher);

    expect(await api.getSession()).toEqual(restored);
    await expect(
      api.createControlPreview({
        action: "EMERGENCY_HALT",
        reasonCode: "LOCAL_EMERGENCY_HALT",
        cancelActiveOrders: true,
      }),
    ).rejects.toMatchObject({
      code: "OPERATOR_REAUTH_REQUIRED",
      status: 401,
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("maps an unauthorized lookup to no session and decodes structured failures", async () => {
    const unauthorized = jsonResponse(
      { detail: { code: "OPERATOR_SESSION_REQUIRED", message: "session required" } },
      401,
    );
    const limited = jsonResponse(
      { detail: { code: "OPERATOR_LOGIN_RATE_LIMITED", message: "try later" } },
      429,
      { "Retry-After": "17" },
    );
    const fetcher = queuedFetcher(unauthorized, limited);
    const api = new LocalConsoleApi(fetcher);

    expect(await api.getSession()).toBeNull();
    await expect(api.login("wrong-value")).rejects.toEqual(
      new LocalConsoleApiError(429, "OPERATOR_LOGIN_RATE_LIMITED", "try later", 17),
    );
  });

  it("fails locally for empty credentials, missing CSRF and foreign receipt URLs", async () => {
    const fetcher = queuedFetcher(jsonResponse({ ...sessionPayload, csrf_token: null }));
    const api = new LocalConsoleApi(fetcher);

    await expect(api.login("")).rejects.toMatchObject({ code: "OPERATOR_PASSWORD_REQUIRED" });
    await expect(api.login("password")).rejects.toMatchObject({ code: "OPERATOR_CSRF_MISSING" });
    await expect(api.getControlReceipt("https://remote.example/receipt")).rejects.toMatchObject({
      code: "CONTROL_STATUS_URL_INVALID",
    });
  });

  it("rejects malformed successful payloads and opaque HTTP failures", async () => {
    const malformed = jsonResponse({ ...sessionPayload, actor_id: 12 });
    const opaque = new Response("bad gateway", { status: 502 });
    const fetcher = queuedFetcher(malformed, opaque);
    const api = new LocalConsoleApi(fetcher);

    await expect(api.login("password")).rejects.toMatchObject({
      code: "LOCAL_API_RESPONSE_INVALID",
    });
    await expect(api.login("password")).rejects.toMatchObject({
      code: "LOCAL_API_ERROR",
      status: 502,
    });
  });

  it("validates nested preview, confirmation and receipt fields at runtime", async () => {
    const fetcher = queuedFetcher(
      jsonResponse(sessionPayload),
      jsonResponse({ ...previewPayload, warnings: "invalid" }),
      jsonResponse({ ...previewPayload, cancel_active_orders: "yes" }),
      jsonResponse({ ...confirmedPayload, command_template: { confirmation: 12 } }),
      jsonResponse({ ...receiptPayload(true), blockers: "none" }),
    );
    const api = new LocalConsoleApi(fetcher);
    await api.login("password");

    await expect(
      api.createControlPreview({
        action: "EMERGENCY_HALT",
        reasonCode: "LOCAL_EMERGENCY_HALT",
        cancelActiveOrders: true,
      }),
    ).rejects.toMatchObject({
      code: "LOCAL_API_RESPONSE_INVALID",
    });
    await expect(
      api.createControlPreview({
        action: "EMERGENCY_HALT",
        reasonCode: "LOCAL_EMERGENCY_HALT",
        cancelActiveOrders: true,
      }),
    ).rejects.toMatchObject({
      code: "LOCAL_API_RESPONSE_INVALID",
    });
    await expect(
      api.confirmControlPreview(previewPayload.preview_id, "phrase"),
    ).rejects.toMatchObject({ code: "LOCAL_API_RESPONSE_INVALID" });
    await expect(api.submitConfirmedControl(confirmedPayload)).rejects.toMatchObject({
      code: "LOCAL_API_RESPONSE_INVALID",
    });
  });
});
