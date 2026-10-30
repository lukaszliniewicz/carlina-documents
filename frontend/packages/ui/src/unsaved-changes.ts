import { mount, unmount } from "svelte";
import DiscardChangesDialog from "./DiscardChangesDialog.svelte";

export type UnsavedChanges = { dirty: boolean; busy?: boolean };
let pending: Promise<boolean> | null = null;
let cancelPending: (() => void) | null = null;

/** Shared by local dialogs and the host's client-side navigation boundary. */
export function confirmDiscardChanges({ dirty, busy = false }: UnsavedChanges): Promise<boolean> {
  if (busy) return Promise.resolve(false);
  if (!dirty) return Promise.resolve(true);
  if (pending) return pending;
  pending = new Promise(resolve => {
    const host = document.createElement("div");
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    document.body.append(host);
    let settled = false;
    let instance: ReturnType<typeof mount>;
    const finish = (discard: boolean) => {
      if (settled) return;
      settled = true;
      void unmount(instance).then(() => {
        host.remove();
        previous?.focus();
        pending = null;
        cancelPending = null;
        resolve(discard);
      });
    };
    instance = mount(DiscardChangesDialog, { target: host, props: { onresolve: finish } });
    cancelPending = () => finish(false);
  });
  return pending;
}

export function guardUnsavedChanges(_node: HTMLElement, initial: UnsavedChanges) {
  let state = initial;
  const beforeUnload = (event: BeforeUnloadEvent) => {
    if (!state.dirty && !state.busy) return;
    event.preventDefault();
    event.returnValue = "";
  };
  const beforeNavigate = (event: Event) => {
    if (event.defaultPrevented || (!state.dirty && !state.busy)) return;
    event.preventDefault();
    const resume = (event as CustomEvent<{ resume?: () => void }>).detail?.resume;
    if (!state.busy && resume) {
      void confirmDiscardChanges(state).then(discard => {
        if (discard && !state.busy) resume();
      });
    }
  };
  window.addEventListener("beforeunload", beforeUnload);
  window.addEventListener("carlina:before-navigate", beforeNavigate);
  return {
    update(next: UnsavedChanges) { state = next; },
    destroy() {
      if (state.dirty) cancelPending?.();
      window.removeEventListener("beforeunload", beforeUnload);
      window.removeEventListener("carlina:before-navigate", beforeNavigate);
    }
  };
}
