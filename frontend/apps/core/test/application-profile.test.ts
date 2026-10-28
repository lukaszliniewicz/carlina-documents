import assert from "node:assert/strict";
import test from "node:test";

import type { FrontendBootstrap } from "@carlina/sdk";

import {
  frontendApplication,
  frontendChatEnabled,
  frontendClientPaths,
  frontendCoreRoutes,
  frontendSettingsRoute,
} from "../src/lib/application-profile.ts";
import { routePageKey } from "../src/lib/route-page.ts";

function documents(): FrontendBootstrap {
  return {
    frontend_api_version: "1.0",
    supported_frontend_api_majors: [1],
    product: {
      name: "Carlina", short_name: "Carlina", descriptor: "Documents", mark: "C",
      distribution_name: "carlina", cli_name: "carlina", environment_prefix: "CARLINA_",
      import_package: "carlina", repository_url: "https://example.test/carlina",
    },
    current_path: "/addons/doc_processor/",
    application: {
      profile: "documents",
      label: "Documents",
      home_url: "/",
    },
    user: { id: "1", display_name: "Reader", is_admin: true },
    feature_flags: { "frontend.chat": true, "frontend.addon.doc_processor": true },
    navigation: [
      { id: "documents", label: "Documents", icon: "book", section: "primary", href: "/", client_route: true },
      { id: "settings", label: "Settings", icon: "settings", section: "utility", href: "/settings", client_route: true },
      { id: "doc_processor", label: "Documents", icon: "book", section: "primary", href: "/addons/doc_processor/", client_route: true },
      { id: "translator", label: "Translate", icon: "book", section: "primary", href: "/addons/translator/", client_route: true },
    ],
    addons: [{
      id: "doc_processor",
      name: "Document Processor", version: "2.5.24", tier: "compiled_workspace",
      active: true,
      compatible: true,
      module_url: "/addons/doc_processor/static/dist/workspace.js",
      frontend_api_range: "^1.0",
      legacy_routes: ["/addons/doc_processor/"],
      css_urls: [],
      build: { status: "built", rebuild_needed: false, log_source_id: null },
    }],
    capabilities: {},
    runtime_readiness: {
      state: "ready", phase: "ready", started_at: null, completed_at: null,
      elapsed_ms: 0, timings_ms: {}, details: {}, errors: [],
    },
    build: { core: { id: "synthetic", status: "built", entry_url: null, css_urls: [] } },
    ui_contributions: [], ui_components: [],
  };
}

test("Documents routing admits ready products and shared settings only", () => {
  const bootstrap = documents();
  assert.deepEqual(frontendClientPaths(bootstrap), ["/", "/settings", "/addons/doc_processor"]);
  assert.equal(frontendChatEnabled(bootstrap), false);
  assert.equal(routePageKey("/runs/retained", undefined, frontendCoreRoutes(bootstrap)), "");
  assert.equal(routePageKey("/settings/models", undefined, frontendCoreRoutes(bootstrap)), "/settings");

  bootstrap.addons[0].active = false;
  assert.deepEqual(frontendClientPaths(bootstrap), ["/", "/settings"]);
});

test("earlier API v1 bootstraps retain Workbench navigation and branding", () => {
  const bootstrap = documents();
  delete bootstrap.application;
  assert.deepEqual(frontendApplication(bootstrap), {
    profile: "workbench", label: "Workbench", home_url: "/",
  });
  assert.equal(frontendChatEnabled(bootstrap), true);
  assert.ok(frontendClientPaths(bootstrap).includes("/runtime/processes"));
  assert.equal(frontendSettingsRoute(bootstrap, "/settings/policy"), null);
});

test("Documents bookmarks resolve unsupported settings to usable model settings", () => {
  const bootstrap = documents();
  for (const section of ["guidance", "policy", "administration"]) {
    assert.equal(frontendSettingsRoute(bootstrap, `/settings/${section}?view=old`), "/settings/models");
    assert.equal(frontendSettingsRoute(bootstrap, `/settings/${section}/nested`), "/settings/models");
  }
  for (const section of ["models", "routing", "sharing", "addons"]) {
    assert.equal(frontendSettingsRoute(bootstrap, `/settings/${section}`), null);
  }
});
