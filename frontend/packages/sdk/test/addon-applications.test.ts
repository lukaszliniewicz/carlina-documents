import assert from "node:assert/strict";
import test from "node:test";

import {
  addonApplicationMountKey,
  addonApplicationRoutes,
  findAddonApplication,
  mountAddonApplication,
} from "../src/addon-applications.ts";
import type {
  AddonMountContext,
  FrontendBootstrap,
} from "../src/contracts.ts";

function bootstrap(
  addonEnabled = true,
): FrontendBootstrap {
  return {
    frontend_api_version: "1.0",
    supported_frontend_api_majors: [1],
    product: {
      name: "Carlina",
      short_name: "Carlina",
      descriptor: "Local-first agent workbench",
      mark: "C",
      distribution_name: "carlina",
      cli_name: "carlina",
      environment_prefix: "CARLINA_",
      import_package: "iarf_assistant",
      repository_url:
        "https://github.com/lukaszliniewicz/iarf_assistant-Refactor",
    },
    current_path: "/addons/stt_dataset/",
    user: {
      id: "1",
      display_name: "Editor",
      is_admin: false,
    },
    feature_flags: {
      "frontend.shell": true,
      "frontend.addon.stt_dataset": addonEnabled,
    },
    navigation: [],
    capabilities: {},
    build: {
      core: {
        id: "core-test",
        status: "built",
        entry_url: "/static/frontend/assets/core.js",
        css_urls: [],
      },
    },
    addons: [
      {
        id: "stt_dataset",
        name: "TTS Dataset",
        version: "0.1.0",
        active: true,
        tier: "compiled_workspace",
        frontend_api_range: "^1.0",
        compatible: true,
        module_url: "/addons/stt_dataset/static/dist/workspace.js",
        css_urls: ["/addons/stt_dataset/static/dist/workspace.css"],
        legacy_routes: ["/addons/stt_dataset/"],
        build: {
          status: "built",
          rebuild_needed: false,
          log_source_id: "addon.stt_dataset.build",
        },
      },
    ],
    ui_contributions: [],
    ui_components: [],
  };
}

class FakeTarget {
  readonly attributes = new Map<string, string>();
  readonly ownerDocument = {
    head: {},
    location: { origin: "http://127.0.0.1:5003" },
  };
  childCount = 1;

  removeAttribute(name: string): void {
    this.attributes.delete(name);
  }

  replaceChildren(): void {
    this.childCount = 0;
  }

  setAttribute(name: string, value: string): void {
    this.attributes.set(name, value);
  }
}

test("enabled addon applications become client routes and match nested paths", () => {
  const enabled = bootstrap();
  assert.deepEqual(addonApplicationRoutes(enabled), [
    "/addons/stt_dataset",
  ]);
  assert.equal(
    findAddonApplication(
      enabled,
      "/addons/stt_dataset/projects/42?panel=audio",
    )?.addon.id,
    "stt_dataset",
  );

  const disabled = bootstrap(false);
  assert.deepEqual(addonApplicationRoutes(disabled), []);
  assert.equal(
    findAddonApplication(disabled, "/addons/stt_dataset/"),
    undefined,
  );
});

test("inactive, incompatible, and stale addon applications stay document routes", () => {
  const cases = [
    (payload: FrontendBootstrap) => {
      payload.addons[0].active = false;
    },
    (payload: FrontendBootstrap) => {
      payload.addons[0].compatible = false;
    },
    (payload: FrontendBootstrap) => {
      payload.addons[0].build.status = "stale";
      payload.addons[0].build.rebuild_needed = true;
    },
  ];

  for (const disable of cases) {
    const payload = bootstrap();
    disable(payload);
    assert.deepEqual(addonApplicationRoutes(payload), []);
    assert.equal(
      findAddonApplication(payload, "/addons/stt_dataset/"),
      undefined,
    );
  }
});

test("addon mount identity stays stable across nested routes", () => {
  const payload = bootstrap();
  const root = findAddonApplication(payload, "/addons/stt_dataset/");
  const nested = findAddonApplication(
    payload,
    "/addons/stt_dataset/projects/42?panel=audio",
  );

  assert.ok(root);
  assert.ok(nested);
  assert.equal(
    addonApplicationMountKey(root, payload),
    addonApplicationMountKey(nested, payload),
  );

  const changed = structuredClone(payload);
  changed.addons[0].module_url = "/addons/stt_dataset/static/dist/workspace-v2.js";
  const changedMatch = findAddonApplication(
    changed,
    "/addons/stt_dataset/projects/42",
  );
  assert.ok(changedMatch);
  assert.notEqual(
    addonApplicationMountKey(root, payload),
    addonApplicationMountKey(changedMatch, changed),
  );
});

test("addon host scopes APIs and aborts before unmount cleanup", async () => {
  const payload = bootstrap();
  const match = findAddonApplication(payload, payload.current_path);
  assert.ok(match);

  const target = new FakeTarget();
  let context: AddonMountContext | undefined;
  let unmounted = false;
  let updatedPath = "";
  let requestedUrl = "";
  const handle = await mountAddonApplication(
    match,
    target as unknown as Element,
    payload,
    "/addons/stt_dataset/?project=7",
    {
      applicationOrigin: "http://127.0.0.1:5003",
      document: target.ownerDocument as unknown as Document,
      fetchImplementation: async (input) => {
        requestedUrl = String(input);
        return new Response(JSON.stringify({ items: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
      importModule: async () => ({
        apiVersion: "1.0",
        mount: (_target: Element, mountContext: AddonMountContext) => {
          context = mountContext;
          return {
            updateCurrentPath(currentPath) {
              updatedPath = currentPath;
            },
            unmount() {
              assert.equal(mountContext.signal.aborted, true);
              unmounted = true;
            },
          };
        },
      }),
      loadStylesheets: async (urls) => {
        assert.deepEqual(urls, [
          "/addons/stt_dataset/static/dist/workspace.css",
        ]);
      },
      navigate: () => "client",
    },
  );

  assert.equal(context?.addonId, "stt_dataset");
  assert.equal(context?.basePath, "/addons/stt_dataset");
  assert.equal(context?.currentPath, "/addons/stt_dataset/?project=7");
  assert.equal(context?.signal.aborted, false);
  await context?.api.request("/api/projects");
  assert.equal(
    requestedUrl,
    "http://127.0.0.1:5003/addons/stt_dataset/api/projects",
  );
  assert.equal(
    target.attributes.get("data-carlina-addon-target"),
    "stt_dataset",
  );
  assert.equal(
    target.attributes.get("data-iarf-addon-target"),
    "stt_dataset",
  );
  await handle.updateCurrentPath?.("/addons/stt_dataset/projects/8");
  assert.equal(updatedPath, "/addons/stt_dataset/projects/8");

  await handle.unmount();
  assert.equal(unmounted, true);
  assert.equal(context?.signal.aborted, true);
  assert.equal(target.attributes.size, 0);
  assert.equal(target.childCount, 0);
  await handle.updateCurrentPath?.("/addons/stt_dataset/projects/9");
  assert.equal(updatedPath, "/addons/stt_dataset/projects/8");
});
