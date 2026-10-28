import assert from "node:assert/strict";
import test from "node:test";

import type {
  FrontendCredentialDelegation,
  FrontendModelProvider,
  FrontendModelRecord,
} from "@carlina/sdk";

import {
  filterDelegations,
  filterModelProviders,
  filterModels,
  imageDetailOptions,
  modelInputCapacityLabel,
  optionalNumber,
  optionalInteger,
  settingsRouteRedirect,
  settingsSectionFromRoute,
} from "../src/lib/model-settings.ts";

function provider(
  overrides: Partial<FrontendModelProvider> = {},
): FrontendModelProvider {
  return {
    id: 1,
    key: "openai",
    name: "OpenAI",
    scope: "global",
    scope_label: "Shared provider",
    kind: "native",
    kind_label: "Native",
    discovery: "automatic",
    discovery_label: "Automatic",
    status: "ready",
    status_label: "Ready",
    connection_count: 1,
    active_connection_count: 1,
    model_count: 2,
    active_model_count: 1,
    requires_endpoint: false,
    supports_custom_models: false,
    supports_discovery: true,
    permissions: {
      add_connection: true,
      add_model: false,
      delete_provider: false,
      sync_models: true,
    },
    actions: {
      detail_url: "/api/frontend/settings/intelligence/providers/1",
      connections_url: "/api/llm/providers/1/keys",
      models_url: "/api/llm/managed-models?provider_id=1",
      delete_url: "/api/llm/providers/1",
      sync_url: "/api/llm/sync-models",
    },
    ...overrides,
  };
}

function model(
  overrides: Partial<FrontendModelRecord> = {},
): FrontendModelRecord {
  return {
    db_id: 10,
    id: "openai/gpt-4.1",
    name: "GPT-4.1",
    provider_id: 1,
    provider_name: "OpenAI",
    scope: "global",
    scope_label: "Shared",
    active: true,
    manual: false,
    type: "chat",
    context_window: 128_000,
    max_output_tokens: 16_000,
    limits: {
      input: {
        detected_tokens: 128_000,
        override_tokens: null,
        effective_tokens: 128_000,
        usable_tokens: 126_720,
        known: true,
        source: "db_model_record",
        source_label: "Recorded model metadata",
        confidence: "high",
        provisional: false,
      },
      output: {
        detected_tokens: 16_000,
        override_tokens: null,
        effective_tokens: 16_000,
        usable_tokens: null,
        known: true,
        source: "db_model_record",
        source_label: "Recorded model metadata",
        confidence: "high",
        provisional: false,
      },
    },
    capabilities: {
      vision: true,
      pdf: true,
      reasoning: false,
      audio: false,
      reasoning_levels: [],
    },
    embedding: {
      default_dimensions: null,
      supports_mrl: false,
      prefix_style: "none",
      has_custom_query_prefix: false,
      has_custom_passage_prefix: false,
    },
    costs: {
      input: 2,
      cached_input: 0.5,
      output: 8,
    },
    permissions: {
      edit: true,
      delete: false,
      toggle_active: true,
      toggle_capabilities: true,
    },
    actions: {
      update_url: "/api/llm/models/10",
      delete_url: "/api/llm/models/10",
      toggle_active_url: "/api/llm/models/10/toggle-active",
      toggle_vision_url: "/api/llm/models/10/toggle-vision",
      toggle_pdf_url: "/api/llm/models/10/toggle-pdf",
      toggle_reasoning_url: "/api/llm/models/10/toggle-reasoning",
      toggle_audio_url: "/api/llm/models/10/toggle-audio",
    },
    updated_at: null,
    ...overrides,
  };
}

test("maps stable nested settings routes to their focused section", () => {
  assert.equal(settingsSectionFromRoute("/settings"), "profile");
  assert.equal(settingsSectionFromRoute("/settings/guidance"), "guidance");
  assert.equal(settingsSectionFromRoute("/settings/policy"), "policy");
  assert.equal(settingsSectionFromRoute("/settings/models?provider=2"), "models");
  assert.equal(settingsSectionFromRoute("/settings/routing"), "routing");
  assert.equal(settingsSectionFromRoute("/settings/sharing"), "sharing");
  assert.equal(settingsSectionFromRoute("/settings/addons/example"), "addons");
  assert.equal(
    settingsSectionFromRoute("/settings/administration?view=usage"),
    "administration",
  );
});

test("redirects legacy provider-local routing URLs to the stable settings route", () => {
  assert.equal(
    settingsRouteRedirect("/settings/models?provider=2&view=routing"),
    "/settings/routing",
  );
  assert.equal(
    settingsRouteRedirect("/settings/models?tab=routing"),
    "/settings/routing",
  );
  assert.equal(settingsRouteRedirect("/settings/models?view=models"), null);
});

test("normalizes optional numeric form bindings", () => {
  assert.equal(optionalNumber(""), null);
  assert.equal(optionalNumber("   "), null);
  assert.equal(optionalNumber(null), null);
  assert.equal(optionalNumber(undefined), null);
  assert.equal(optionalNumber(0), 0);
  assert.equal(optionalNumber(0.25), 0.25);
  assert.equal(optionalNumber("1.5"), 1.5);
  assert.equal(optionalNumber("not-a-number"), null);
});

test("normalizes optional positive integer token overrides", () => {
  assert.equal(optionalInteger(""), null);
  assert.equal(optionalInteger("  "), null);
  assert.equal(optionalInteger("128000"), 128000);
  assert.equal(optionalInteger("1.5"), null);
  assert.equal(optionalInteger(0), null);
  assert.equal(modelInputCapacityLabel(model()), "128,000 input tokens · Recorded model metadata");
});

test("filters providers and models through semantic labels and capabilities", () => {
  const providers = [
    provider(),
    provider({
      id: 2,
      key: "custom_u1_editorial",
      name: "Editorial vLLM",
      kind: "openai_compatible",
      kind_label: "OpenAI-compatible endpoint",
      status: "attention",
      status_label: "Connection needed",
    }),
  ];
  assert.deepEqual(
    filterModelProviders(providers, "compatible connection").map(
      (item) => item.id,
    ),
    [2],
  );

  const models = [
    model(),
    model({
      db_id: 11,
      id: "gemini/text-embedding-004",
      name: "Text Embedding 004",
      provider_id: 2,
      provider_name: "Gemini",
      active: false,
      type: "embedding",
      capabilities: {
        vision: false,
        pdf: false,
        reasoning: false,
        audio: false,
        reasoning_levels: [],
      },
      embedding: {
        default_dimensions: 768,
        supports_mrl: true,
        prefix_style: "none",
        has_custom_query_prefix: false,
        has_custom_passage_prefix: false,
      },
    }),
  ];
  assert.deepEqual(
    filterModels(models, {
      query: "matryoshka",
      type: "embedding",
      state: "inactive",
    }).map((item) => item.db_id),
    [11],
  );
});

test("offers provider-appropriate image detail levels", () => {
  assert.deepEqual(
    imageDetailOptions(model()).map((option) => option.value),
    ["preserve", "omit", "auto", "low", "high"],
  );
  assert.deepEqual(
    imageDetailOptions(
      model({
        id: "gemini/gemini-3-pro",
        provider_name: "Google Vertex",
      }),
    ).map((option) => option.value),
    [
      "preserve",
      "omit",
      "auto",
      "low",
      "medium",
      "high",
      "ultra_high",
    ],
  );
});

test("searches credential delegations without credential payloads", () => {
  const delegations: FrontendCredentialDelegation[] = [
    {
      provider_id: "wordpress",
      provider_name: "WordPress",
      credential_id: 7,
      credential_name: "Editorial account",
      delegatee_id: 2,
      delegatee_email: "editor@example.test",
    },
    {
      provider_id: "stripe",
      provider_name: "Stripe",
      credential_id: 8,
      credential_name: "Finance",
      delegatee_id: 3,
      delegatee_email: "treasurer@example.test",
    },
  ];
  assert.deepEqual(
    filterDelegations(delegations, "editorial editor").map(
      (item) => item.provider_id,
    ),
    ["wordpress"],
  );
});
