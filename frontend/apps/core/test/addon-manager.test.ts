import assert from "node:assert/strict";
import test from "node:test";

import type {
  FrontendAddonManagerItem,
  FrontendAddonManagerSummary,
  FrontendAddonOperation,
} from "@carlina/sdk";

import {
  addonNeedsAttention,
  addonOperationActive,
  addonOperationProgress,
  addonOperationStatusLabel,
  addonOperationTone,
  collectAddonFacets,
  filterAddonDirectory,
  summarizeAddonDirectory,
} from "../src/lib/addon-manager.ts";

function operation(
  overrides: Partial<FrontendAddonOperation> = {},
): FrontendAddonOperation {
  return {
    id: "addon-op-1",
    job_id: "addon-op-1",
    addon_id: "example",
    operation: "activate",
    status: "running",
    stage: "runtime_load",
    progress: 82,
    message: "Loading runtime.",
    error: null,
    bytes: { current: null, total: null },
    last_activity_at: "2026-08-03T12:00:00Z",
    queued_at: "2026-08-03T11:59:00Z",
    started_at: "2026-08-03T12:00:00Z",
    completed_at: null,
    updated_at: "2026-08-03T12:00:00Z",
    attempt: 1,
    recovered_count: 0,
    cancel_requested: false,
    cancellable: true,
    retryable: false,
    retry_expires_at: null,
    input_retained: null,
    admission: null,
    readiness: {},
    result: null,
    log_source_ids: [],
    log_links: [],
    status_url: "/api/addons/operations/addon-op-1",
    cancel_url: "/api/addons/operations/addon-op-1/cancel",
    retry_url: "/api/addons/operations/addon-op-1/retry",
    ...overrides,
  };
}

function addon(
  overrides: Partial<FrontendAddonManagerItem> = {},
): FrontendAddonManagerItem {
  return {
    id: "example",
    name: "Example",
    version: "1.0.0",
    author: "Carlina Project",
    description: "Example workspace",
    roles: ["workspace"],
    categories: ["content"],
    provider_domains: [],
    addon_type: ["automation"],
    dependencies: [],
    is_active: true,
    status: { state: "active", reasons: [] },
    frontend_tier: "compiled_workspace",
    frontend_build: { status: "built", rebuild_needed: false },
    worker_state: {
      status: "configured",
      configured: 1,
      running: 0,
      pids: [],
      workers: [],
      log_source_id: "worker.example",
    },
    scheduled_job_state: {
      status: "not_declared",
      declared: 0,
      registered: 0,
      paused: 0,
      jobs: [],
      declared_jobs: [],
      log_source_id: "job.example",
    },
    has_settings: false,
    has_prompts: false,
    delegatable: false,
    log_links: [],
    inferred_hub: {
      relation: "standalone",
      hub_id: null,
      hub_label: null,
      spoke_count: 0,
    },
    ...overrides,
  };
}

function addonSummary(
  overrides: Partial<FrontendAddonManagerSummary> = {},
): FrontendAddonManagerSummary {
  return {
    detail_level: "summary",
    detail_url: "/api/addons/example/detail",
    id: "example",
    name: "Example",
    version: "1.0.0",
    author: "Carlina Project",
    description: "Compact example summary",
    roles: ["workspace"],
    categories: ["content"],
    provider_domains: [],
    addon_type: ["automation"],
    dependencies: [],
    connection_count: 0,
    consumer_binding_count: 0,
    provider_binding_count: 0,
    search_terms: ["embedded semantic alias"],
    is_active: true,
    status: { state: "active", reasons: [] },
    frontend_tier: "compiled_workspace",
    frontend_status: "built",
    frontend_rebuild_needed: false,
    worker_running: 0,
    has_settings: false,
    has_prompts: false,
    delegatable: false,
    inferred_hub: {
      relation: "standalone",
      hub_id: null,
      hub_label: null,
      spoke_count: 0,
    },
    ...overrides,
  };
}

test("summarizes lifecycle health and running workers", () => {
  const addons = [
    addon({ worker_state: { ...addon().worker_state, running: 2 } }),
    addon({
      id: "inactive",
      name: "Inactive",
      is_active: false,
      worker_state: { ...addon().worker_state, running: 0 },
    }),
    addon({
      id: "stale",
      name: "Stale",
      frontend_build: { status: "stale", rebuild_needed: true },
    }),
  ];

  assert.deepEqual(summarizeAddonDirectory(addons), {
    total: 3,
    active: 2,
    inactive: 1,
    attention: 1,
    runningWorkers: 2,
  });
  assert.equal(addonNeedsAttention(addons[2]), true);
});

test("filters by semantic status and searchable relationships", () => {
  const addons = [
    addon(),
    addon({
      id: "drive",
      name: "Drive Picker",
      roles: ["provider"],
      categories: ["storage"],
      dependencies: ["google_auth"],
      provider_domains: ["document"],
    }),
    addon({
      id: "broken",
      name: "Broken",
      status: { state: "degraded", reasons: ["Dependency unavailable"] },
    }),
  ];

  assert.deepEqual(
    filterAddonDirectory(addons, {
      query: "google_auth",
      status: "all",
      role: "all",
      category: "all",
    }).map((item) => item.id),
    ["drive"],
  );
  assert.deepEqual(
    filterAddonDirectory(addons, {
      query: "",
      status: "attention",
      role: "all",
      category: "all",
    }).map((item) => item.id),
    ["broken"],
  );
  assert.deepEqual(collectAddonFacets(addons, "roles"), [
    "provider",
    "workspace",
  ]);
});

test("searches optional dependency license notices", () => {
  const addons = [
    addon({
      dependency_notices: [
        {
          id: "pdf-license",
          packages: ["PyMuPDF"],
          license: "GNU AGPL-3.0 or commercial license",
          purpose: "PDF rendering",
          review_required: true,
        },
      ],
    }),
  ];

  assert.deepEqual(
    filterAddonDirectory(addons, {
      query: "agpl",
      status: "all",
      role: "all",
      category: "all",
    }).map((item) => item.id),
    ["example"],
  );
});

test("uses compact inventory health, workers, and server-projected search terms", () => {
  const addons = [
    addonSummary({ worker_running: 3 }),
    addonSummary({
      id: "stale",
      detail_url: "/api/addons/stale/detail",
      frontend_status: "stale",
      frontend_rebuild_needed: true,
    }),
  ];

  assert.deepEqual(summarizeAddonDirectory(addons), {
    total: 2,
    active: 2,
    inactive: 0,
    attention: 1,
    runningWorkers: 3,
  });
  assert.deepEqual(
    filterAddonDirectory(addons, {
      query: "semantic alias",
      status: "all",
      role: "all",
      category: "all",
    }).map((item) => item.id),
    ["example", "stale"],
  );
});

test("normalizes lifecycle operation state for the manager UI", () => {
  const active = operation({ progress: 182 });
  assert.equal(addonOperationActive(active), true);
  assert.equal(addonOperationProgress(active), 100);
  assert.equal(addonOperationStatusLabel(active), "In progress");
  assert.equal(addonOperationTone(active), "neutral");

  const cancelling = operation({ cancel_requested: true });
  assert.equal(addonOperationStatusLabel(cancelling), "Cancelling");
  assert.equal(addonOperationTone(cancelling), "warning");

  const failed = operation({ status: "failed", progress: null });
  assert.equal(addonOperationActive(failed), false);
  assert.equal(addonOperationProgress(failed), 0);
  assert.equal(addonOperationTone(failed), "danger");
});
