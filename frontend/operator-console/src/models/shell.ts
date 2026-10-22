export type ConnectionState = "UNKNOWN" | "CONNECTED" | "DEGRADED" | "DISCONNECTED";

export interface ShellStatus {
  readonly accountAlias: string;
  readonly environment: string;
  readonly mode: string;
  readonly qmt: ConnectionState;
  readonly central: ConnectionState;
  readonly snapshotAt: string | null;
}

export const EMPTY_SHELL_STATUS: Readonly<ShellStatus> = Object.freeze({
  accountAlias: "未加载",
  environment: "环境未加载",
  mode: "BOOTING",
  qmt: "UNKNOWN",
  central: "UNKNOWN",
  snapshotAt: null,
});
