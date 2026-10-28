import assert from "node:assert/strict";
import test from "node:test";

import { installLegacyFrontendAdapter } from "../src/legacy.ts";
import {
  loadVersionedFrontendModule,
  resolveSameOriginModuleUrl,
} from "../src/loader.ts";


test("module URL validation accepts only platform-owned same-origin paths", () => {
  assert.equal(
    resolveSameOriginModuleUrl(
      "/static/frontend/assets/core.js",
      "http://127.0.0.1:5003/",
    ),
    "http://127.0.0.1:5003/static/frontend/assets/core.js",
  );
  assert.throws(
    () =>
      resolveSameOriginModuleUrl(
        "https://example.test/addon.js",
        "http://127.0.0.1:5003/",
      ),
    /same origin|application origin/i,
  );
  assert.throws(
    () =>
      resolveSameOriginModuleUrl(
        "/uploads/untrusted.js",
        "http://127.0.0.1:5003/",
      ),
    /platform static or addon path/i,
  );
  assert.throws(
    () =>
      resolveSameOriginModuleUrl(
        "/addons/example/%2e%2e/other/app.js",
        "http://127.0.0.1:5003/",
      ),
    /path traversal/i,
  );
});


test("unsupported majors are rejected before a module is downloaded", async () => {
  let imported = false;
  await assert.rejects(
    () =>
      loadVersionedFrontendModule(
        {
          id: "future-addon",
          module_url: "/addons/future/static/app.js",
          frontend_api_range: "^2.0",
        },
        "http://127.0.0.1:5003/",
        async () => {
          imported = true;
          return {};
        },
      ),
    /unsupported frontend API major/i,
  );
  assert.equal(imported, false);
});


test("compatible modules expose the versioned mount contract", async () => {
  const loaded = await loadVersionedFrontendModule(
    {
      id: "example-addon",
      module_url: "/addons/example/static/app.js",
      frontend_api_range: "^1.0",
    },
    "http://127.0.0.1:5003/",
    async (url) => {
      assert.equal(
        url,
        "http://127.0.0.1:5003/addons/example/static/app.js",
      );
      return {
        apiVersion: "1.0",
        mount: () => ({ unmount() {} }),
      };
    },
  );
  assert.equal(loaded.apiVersion, "1.0");
  assert.equal(typeof loaded.mount, "function");
});


test("legacy bridge preserves an existing UI component loader", () => {
  const legacyLoader = async () => ({ open() {} });
  const fakeWindow = {
    IARF_AI: { loadUiComponent: legacyLoader },
    location: { origin: "http://127.0.0.1:5003" },
  } as unknown as Window;

  const namespace = installLegacyFrontendAdapter(
    fakeWindow,
    async () => ({
      apiVersion: "1.0",
      mount: () => ({ unmount() {} }),
    }),
  );

  assert.equal(namespace.loadUiComponent, legacyLoader);
  assert.equal(fakeWindow.CARLINA, namespace);
  assert.equal(fakeWindow.IARF_AI, namespace);
  assert.equal(namespace.frontend?.apiVersion, "1.0");
  assert.equal(
    namespace.frontend?.resolveModuleUrl("/addons/example/static/app.js"),
    "http://127.0.0.1:5003/addons/example/static/app.js",
  );
});
