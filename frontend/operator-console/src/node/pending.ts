/** Retain only an uncertain request ID across tab refresh, never credentials.
 * Storage failure blocks confirmation so its recovery handle cannot be lost.
 */
export function pendingKey(node: string, account: string): string {
  return `ats:pending:${node}:${account}`;
}
export function readPending(key: string): string {
  const value = sessionStorage.getItem(key) ?? "";
  if (value && !/^control-[a-f0-9]{32}$/.test(value)) throw new Error("invalid request identity");
  return value;
}
export function writePending(key: string, value: string): void {
  if (value) {
    sessionStorage.setItem(key, value);
    if (sessionStorage.getItem(key) !== value) throw new Error("request identity not retained");
  } else sessionStorage.removeItem(key);
}
