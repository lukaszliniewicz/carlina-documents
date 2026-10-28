export const RESTART_REQUIRED_EVENT = "carlina:restart-required";

/** Notify the shell after a lifecycle result has durably required a restart. */
export function announceRestartRequired(): void {
  window.dispatchEvent(new CustomEvent(RESTART_REQUIRED_EVENT));
}
