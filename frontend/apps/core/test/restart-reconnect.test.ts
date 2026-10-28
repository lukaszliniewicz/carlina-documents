import assert from "node:assert/strict";
import test from "node:test";

import {
  readRestartStatus,
  startRestartReconnect,
  type RestartStatusReadOptions,
  type RestartStatus,
} from "../src/lib/restart-reconnect.ts";

function manualTimers() {
  let nextTimer = 0;
  const callbacks = new Map<number, () => void>();
  return {
    schedule(callback: () => void): number {
      nextTimer += 1;
      callbacks.set(nextTimer, callback);
      return nextTimer;
    },
    cancel(timer: number): void {
      callbacks.delete(timer);
    },
    get pendingCount(): number {
      return callbacks.size;
    },
    async runNext(): Promise<void> {
      const entry = callbacks.entries().next().value as
        | [number, () => void]
        | undefined;
      assert.ok(entry, "a restart probe should be scheduled");
      callbacks.delete(entry[0]);
      entry[1]();
      await new Promise<void>((resolve) => setImmediate(resolve));
    },
  };
}

test("reconnects when a fast successor completes the exact durable handoff", async () => {
  const timers = manualTimers();
  let reloads = 0;

  startRestartReconnect({
    handoffId: "handoff-requested-by-this-tab",
    readStatus: async (): Promise<RestartStatus> => ({
      pending: false,
      status: "completed",
      completed_handoff_id: "handoff-requested-by-this-tab",
    }),
    onSuccessor: () => {
      reloads += 1;
    },
    onTimeout: assert.fail,
    schedule: timers.schedule,
    cancel: timers.cancel,
  });

  await timers.runNext();
  assert.equal(reloads, 1);
});

test("does not reload before the matching handoff is marked completed", async () => {
  const timers = manualTimers();
  let reloads = 0;
  const responses: RestartStatus[] = [
    {
      pending: true,
      status: "restart_requested",
      completed_handoff_id: "handoff-requested-by-this-tab",
    },
    {
      pending: false,
      status: "completed",
      completed_handoff_id: "handoff-requested-by-this-tab",
    },
  ];

  startRestartReconnect({
    handoffId: "handoff-requested-by-this-tab",
    readStatus: async () => responses.shift() as RestartStatus,
    onSuccessor: () => {
      reloads += 1;
    },
    onTimeout: assert.fail,
    schedule: timers.schedule,
    cancel: timers.cancel,
  });

  await timers.runNext();
  assert.equal(reloads, 0);
  await timers.runNext();
  assert.equal(reloads, 1);
});

test("different workers and other completed handoffs cannot satisfy this tab's restart", async () => {
  const timers = manualTimers();
  let reloads = 0;
  const observed: RestartStatus[] = [];
  const responses: Array<RestartStatus | Error> = [
    { pending: true, status: "restart_requested", completed_handoff_id: null },
    { pending: false, status: "completed", completed_handoff_id: "another-handoff" },
    new Error("not ready"),
    {
      pending: false,
      status: "completed",
      completed_handoff_id: "handoff-requested-by-this-tab",
    },
  ];

  startRestartReconnect({
    handoffId: "handoff-requested-by-this-tab",
    readStatus: async () => {
      const response = responses.shift();
      if (response instanceof Error) throw response;
      assert.ok(response);
      return response;
    },
    onSuccessor: () => {
      reloads += 1;
    },
    onStatus: (status) => observed.push(status),
    onTimeout: assert.fail,
    schedule: timers.schedule,
    cancel: timers.cancel,
  });

  await timers.runNext();
  assert.equal(reloads, 0);
  await timers.runNext();
  assert.equal(reloads, 0);
  await timers.runNext();
  assert.equal(reloads, 0);
  await timers.runNext();
  assert.equal(reloads, 1);
  assert.equal(observed[0]?.status, "restart_requested");
  assert.equal(observed[1]?.completed_handoff_id, "another-handoff");
  assert.equal(
    observed[2]?.completed_handoff_id,
    "handoff-requested-by-this-tab",
  );
});

test("restart-status reads bypass HTTP caches", async () => {
  let path = "";
  let options: RestartStatusReadOptions | undefined;

  await readRestartStatus(async (nextPath, nextOptions) => {
    path = nextPath;
    options = nextOptions;
    return { pending: false, status: "ready", completed_handoff_id: null };
  });

  assert.equal(path, "/api/system/restart-status");
  assert.deepEqual(options, { cache: "no-store" });
});

test("stopping a reconnect probe cancels its pending timer", () => {
  const timers = manualTimers();
  const stop = startRestartReconnect({
    handoffId: "handoff-requested-by-this-tab",
    readStatus: async () => ({ pending: true, status: "restart_requested" }),
    onSuccessor: assert.fail,
    onTimeout: assert.fail,
    schedule: timers.schedule,
    cancel: timers.cancel,
  });

  assert.equal(timers.pendingCount, 1);
  stop();
  assert.equal(timers.pendingCount, 0);
});

test("timing out a reconnect probe leaves no additional timer behind", async () => {
  const timers = manualTimers();
  let timeouts = 0;
  startRestartReconnect({
    handoffId: "handoff-requested-by-this-tab",
    readStatus: async () => ({ pending: true, status: "restart_requested" }),
    onSuccessor: assert.fail,
    onTimeout: () => {
      timeouts += 1;
    },
    schedule: timers.schedule,
    cancel: timers.cancel,
    maxAttempts: 1,
  });

  await timers.runNext();
  assert.equal(timeouts, 1);
  assert.equal(timers.pendingCount, 0);
});
