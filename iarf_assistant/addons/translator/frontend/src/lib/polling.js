import { isAbortError } from "./api.js";

/**
 * One-shot polling controller. It owns its timer and abort controller so a
 * destroyed Svelte component cannot keep issuing requests in the background.
 */
export function createPollingController({
  run,
  shouldPoll = () => true,
  delay = 3000,
  onError = () => {}
} = {}) {
  let timer = null;
  let controller = null;
  let stopped = true;
  let generation = 0;

  function clear() {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
    controller?.abort();
    controller = null;
  }

  function stop() {
    stopped = true;
    generation += 1;
    clear();
  }

  function schedule(nextDelay = delay) {
    if (stopped || !shouldPoll()) return;
    const current = ++generation;
    clear();
    timer = setTimeout(async () => {
      timer = null;
      if (stopped || current !== generation) return;
      controller = new AbortController();
      try {
        await run(controller.signal);
      } catch (error) {
        if (!isAbortError(error) && !stopped && current === generation) {
          onError(error);
        }
      } finally {
        controller = null;
        if (!stopped && current === generation) schedule();
      }
    }, nextDelay);
  }

  function start() {
    stopped = false;
    schedule(0);
  }

  return {
    start,
    schedule,
    stop,
    clear,
    get active() {
      return !stopped;
    }
  };
}

function abortError() {
  const error = new Error("Operation cancelled.");
  error.name = "AbortError";
  return error;
}

export function abortableDelay(milliseconds, signal = null) {
  if (signal?.aborted) return Promise.reject(abortError());
  return new Promise((resolve, reject) => {
    let timer = setTimeout(cleanupAndResolve, milliseconds);
    const onAbort = () => {
      clearTimeout(timer);
      cleanup();
      reject(abortError());
    };
    const cleanup = () => signal?.removeEventListener("abort", onAbort);
    function cleanupAndResolve() {
      cleanup();
      resolve();
    }
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export async function pollUntil({
  load,
  signal = null,
  maxAttempts = 900,
  intervalMs = 1000,
  successStates = ["completed", "partial", "completed_with_errors"],
  errorStates = ["cancelled", "failed", "error"]
} = {}) {
  const successes = new Set(successStates);
  const failures = new Set(errorStates);
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    if (signal?.aborted) throw abortError();
    const payload = await load(signal);
    if (signal?.aborted) throw abortError();
    const state = String(
      payload?.status || payload?.state || payload?.operation?.state || payload?.operation?.status || ""
    ).toLowerCase();
    if (successes.has(state)) return payload;
    if (failures.has(state)) {
      const error = new Error(payload?.error || payload?.message || `Background operation ${state}.`);
      error.status = state;
      error.payload = payload;
      throw error;
    }
    await abortableDelay(intervalMs, signal);
  }
  throw new Error("Background operation timed out.");
}
