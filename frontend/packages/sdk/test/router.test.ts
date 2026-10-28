import assert from "node:assert/strict";
import test from "node:test";

import {
  createFrontendRouter,
  isClientRoute,
  relativeBrowserUrl,
} from "../src/router.ts";

function createWindow(path = "/workspaces") {
  const origin = "https://iarf.test";
  const listeners = new Set<() => void>();
  const assigned: string[] = [];
  const state = new URL(path, origin);
  const location = {
    get href() {
      return state.href;
    },
    get origin() {
      return state.origin;
    },
    get pathname() {
      return state.pathname;
    },
    get search() {
      return state.search;
    },
    get hash() {
      return state.hash;
    },
    assign(url: string) {
      assigned.push(url);
    },
  };
  const setUrl = (url: string | URL | null | undefined) => {
    if (url == null) return;
    const next = new URL(String(url), state);
    state.href = next.href;
  };
  return {
    window: {
      location,
      history: {
        pushState(_data: unknown, _unused: string, url?: string | URL | null) {
          setUrl(url);
        },
        replaceState(
          _data: unknown,
          _unused: string,
          url?: string | URL | null,
        ) {
          setUrl(url);
        },
      },
      addEventListener(_type: "popstate", listener: () => void) {
        listeners.add(listener);
      },
      removeEventListener(_type: "popstate", listener: () => void) {
        listeners.delete(listener);
      },
    },
    assigned,
    emitPopState() {
      listeners.forEach((listener) => listener());
    },
  };
}

test("normalizes exact client paths while preserving browser URL details", () => {
  assert.equal(isClientRoute("/settings/", ["/settings"]), true);
  assert.equal(isClientRoute("/settings/security", ["/settings"]), true);
  assert.equal(isClientRoute("/setting", ["/settings"]), false);
  assert.equal(
    relativeBrowserUrl({
      pathname: "/logs",
      search: "?source=app",
      hash: "#latest",
    }),
    "/logs?source=app#latest",
  );
});

test("uses History API only for registered same-origin routes", () => {
  const fixture = createWindow();
  const changes: string[] = [];
  const router = createFrontendRouter({
    window: fixture.window,
    clientPaths: ["/workspaces", "/logs", "/settings"],
    onRouteChange: (route) => changes.push(route),
  });

  router.start();
  assert.equal(router.navigate("/logs?source=app.legacy"), "client");
  assert.equal(changes.at(-1), "/logs?source=app.legacy");
  fixture.emitPopState();
  assert.equal(changes.at(-1), "/logs?source=app.legacy");

  assert.equal(router.navigate("/workflow-runs"), "document");
  assert.equal(fixture.assigned.at(-1), "https://iarf.test/workflow-runs");
  assert.equal(router.navigate("https://outside.test/"), "document");
  assert.equal(fixture.assigned.at(-1), "https://outside.test/");
  router.stop();
});

test("uses History API for descendants of a registered client route", () => {
  const fixture = createWindow("/runs");
  const changes: string[] = [];
  const router = createFrontendRouter({
    window: fixture.window,
    clientPaths: ["/", "/runs"],
    onRouteChange: (route) => changes.push(route),
  });

  assert.equal(router.navigate("/runs/run-123?tab=graph"), "client");
  assert.equal(changes.at(-1), "/runs/run-123?tab=graph");
  assert.equal(fixture.assigned.length, 0);
});
