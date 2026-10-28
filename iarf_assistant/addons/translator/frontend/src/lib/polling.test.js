import { afterEach, describe, expect, it, vi } from "vitest";
import { abortableDelay, createPollingController, pollUntil } from "./polling.js";

describe("translator polling lifecycle", () => {
  afterEach(() => vi.useRealTimers());

  it("aborts and prevents a scheduled callback after stop", async () => {
    vi.useFakeTimers();
    const run = vi.fn();
    const poller = createPollingController({ run, delay: 100 });
    poller.start();
    poller.stop();
    await vi.advanceTimersByTimeAsync(500);
    expect(run).not.toHaveBeenCalled();
    expect(poller.active).toBe(false);
  });

  it("passes an abort signal to the active request", async () => {
    vi.useFakeTimers();
    let signal;
    const run = vi.fn((nextSignal) => { signal = nextSignal; return new Promise(() => {}); });
    const poller = createPollingController({ run, delay: 0 });
    poller.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(signal).toBeInstanceOf(AbortSignal);
    poller.stop();
    expect(signal.aborted).toBe(true);
  });

  it("treats partial completion as a successful result and terminal failures as errors", async () => {
    await expect(pollUntil({ load: vi.fn(async () => ({ status: "partial", result: { count: 2 } })) })).resolves.toMatchObject({ status: "partial" });
    await expect(pollUntil({ load: vi.fn(async () => ({ status: "cancelled", message: "Stopped" })) })).rejects.toMatchObject({ status: "cancelled", message: "Stopped" });
  });

  it("aborts the delay so a destroyed workspace cannot continue polling", async () => {
    const controller = new AbortController();
    const promise = abortableDelay(1000, controller.signal);
    controller.abort();
    await expect(promise).rejects.toMatchObject({ name: "AbortError" });
  });
});
