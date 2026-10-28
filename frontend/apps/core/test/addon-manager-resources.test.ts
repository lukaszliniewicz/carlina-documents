import assert from "node:assert/strict";
import test from "node:test";

import { get } from "svelte/store";

import { createLatestResource } from "../src/lib/addon-manager-resources.ts";

interface Deferred<Value> {
  promise: Promise<Value>;
  resolve(value: Value): void;
  reject(reason: unknown): void;
}

function deferred<Value>(): Deferred<Value> {
  let resolve!: (value: Value) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<Value>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

test("deduplicates the same active key and publishes successful data", async () => {
  const pending = deferred<string>();
  let calls = 0;
  const resource = createLatestResource(
    async (_signal, _key: string) => {
      calls += 1;
      return pending.promise;
    },
    { key: (key) => key, errorMessage: "Could not load." },
  );

  const first = resource.load("email");
  const second = resource.load("email");
  assert.equal(first, second);
  assert.equal(calls, 1);
  assert.deepEqual(get(resource), {
    data: null,
    loading: true,
    loaded: false,
    error: "",
    requestedKey: "email",
    resolvedKey: null,
  });

  pending.resolve("Email");
  assert.deepEqual(await first, { status: "success", data: "Email" });
  assert.deepEqual(get(resource), {
    data: "Email",
    loading: false,
    loaded: true,
    error: "",
    requestedKey: null,
    resolvedKey: "email",
  });
});

test("aborts superseded requests and rejects stale responses even when loaders ignore abort", async () => {
  const requests = new Map<string, Deferred<string>>();
  const signals = new Map<string, AbortSignal>();
  const resource = createLatestResource(
    async (signal, key: string) => {
      signals.set(key, signal);
      const pending = deferred<string>();
      requests.set(key, pending);
      return pending.promise;
    },
    { key: (key) => key, errorMessage: "Could not load." },
  );

  const first = resource.load("first");
  const second = resource.load("second");
  assert.equal(signals.get("first")?.aborted, true);
  requests.get("first")?.resolve("stale");
  requests.get("second")?.resolve("current");

  assert.deepEqual(await first, { status: "aborted" });
  assert.deepEqual(await second, { status: "success", data: "current" });
  assert.equal(get(resource).data, "current");
  assert.equal(get(resource).resolvedKey, "second");
});

test("preserves last-known-good data when a refresh fails", async () => {
  let fail = false;
  const resource = createLatestResource(
    async () => {
      if (fail) throw new Error("Network unavailable");
      return { value: 7 };
    },
    { errorMessage: "Could not load." },
  );

  assert.equal((await resource.load()).status, "success");
  fail = true;
  assert.deepEqual(await resource.load(), {
    status: "error",
    error: "Network unavailable",
  });
  assert.deepEqual(get(resource), {
    data: { value: 7 },
    loading: false,
    loaded: true,
    error: "Network unavailable",
    requestedKey: null,
    resolvedKey: "default",
  });
});

test("explicit abort and reset clear in-flight ownership without leaking stale writes", async () => {
  const pending = deferred<number>();
  const resource = createLatestResource(
    async () => pending.promise,
    { errorMessage: "Could not load." },
  );

  const request = resource.load();
  resource.abort();
  pending.resolve(42);
  assert.deepEqual(await request, { status: "aborted" });
  assert.equal(get(resource).loading, false);

  resource.reset();
  assert.deepEqual(get(resource), {
    data: null,
    loading: false,
    loaded: false,
    error: "",
    requestedKey: null,
    resolvedKey: null,
  });
  assert.equal(resource.reload(), null);
});
