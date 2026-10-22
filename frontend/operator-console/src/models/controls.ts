export type LocalControlAction =
  "PAUSE_BUY" | "SELL_ONLY" | "MANUAL_TAKEOVER" | "EMERGENCY_HALT" | "CLEAR_HALT" | "REQUEST_AUTO";

export interface OperatorControlDefinition {
  readonly action: LocalControlAction;
  readonly label: string;
  readonly reasonCode: string;
  readonly tone: "warning" | "error";
  readonly haltOrderDisposition: boolean;
}

export const OPERATOR_CONTROLS: readonly OperatorControlDefinition[] = Object.freeze([
  {
    action: "PAUSE_BUY",
    label: "暂停买入",
    reasonCode: "LOCAL_PAUSE_BUY",
    tone: "warning",
    haltOrderDisposition: false,
  },
  {
    action: "SELL_ONLY",
    label: "切换只卖",
    reasonCode: "LOCAL_SELL_ONLY",
    tone: "warning",
    haltOrderDisposition: false,
  },
  {
    action: "MANUAL_TAKEOVER",
    label: "人工接管",
    reasonCode: "LOCAL_MANUAL_TAKEOVER",
    tone: "warning",
    haltOrderDisposition: false,
  },
  {
    action: "EMERGENCY_HALT",
    label: "紧急停止",
    reasonCode: "LOCAL_EMERGENCY_HALT",
    tone: "error",
    haltOrderDisposition: true,
  },
  {
    action: "CLEAR_HALT",
    label: "解除停止",
    reasonCode: "LOCAL_CLEAR_HALT",
    tone: "warning",
    haltOrderDisposition: false,
  },
  {
    action: "REQUEST_AUTO",
    label: "申请恢复自动",
    reasonCode: "LOCAL_REQUEST_AUTO",
    tone: "warning",
    haltOrderDisposition: false,
  },
]);

export function controlDefinition(action: LocalControlAction): OperatorControlDefinition {
  const definition = OPERATOR_CONTROLS.find((item) => item.action === action);
  if (!definition) throw new Error(`未知本地控制: ${action}`);
  return definition;
}
