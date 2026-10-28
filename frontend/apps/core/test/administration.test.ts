import assert from "node:assert/strict";
import test from "node:test";

import type {
  FrontendMemberRecord,
  FrontendRetrievalSettings,
} from "@carlina/sdk";

import {
  administrationViewFromRoute,
  administrationViewHref,
  filterMembers,
  retrievalDraft,
  retrievalDraftIsValid,
  retrievalHasChanges,
  retrievalNeedsPreflight,
  retrievalStructuralChanges,
  usageDailyMaximum,
} from "../src/lib/administration.ts";

function retrieval(): FrontendRetrievalSettings {
  return {
    configuration: {
      model_id: "provider/embedding",
      dimensions: 768,
      task_type: "search",
      top_k: 5,
      chunk_size: 5_000,
      chunk_overlap: 500,
    },
    models: [
      {
        id: "provider/embedding",
        name: "Embedding",
        provider: "provider",
        provider_label: "Provider",
        supports_mrl: true,
        default_dimensions: 1_536,
        prefix_style: "none",
      },
      {
        id: "provider/fixed",
        name: "Fixed",
        provider: "provider",
        provider_label: "Provider",
        supports_mrl: false,
        default_dimensions: 1_024,
        prefix_style: "none",
      },
    ],
    index: { documents: 12, chunks: 48 },
    validation: {
      model_available: true,
      dimensions: true,
      task_type: true,
      top_k: true,
      chunk_size: true,
      chunk_overlap: true,
    },
    configuration_needs_review: false,
    task_types: [
      { id: "search", label: "Search", description: "General retrieval." },
      { id: "qa", label: "Q&A", description: "Answers." },
    ],
    limits: {
      dimensions: { minimum: 128, maximum: 3_072 },
      top_k: { minimum: 1, maximum: 20 },
      chunk_size: { minimum: 500, maximum: 15_000 },
      chunk_overlap: { minimum: 0, maximum: 2_000 },
    },
    actions: {
      update_url: "/api/frontend/settings/administration/retrieval",
      preflight_url:
        "/api/frontend/settings/administration/retrieval/preflight",
      rebuild_url:
        "/api/frontend/settings/administration/retrieval/rebuild",
    },
  };
}

test("normalizes administration views and route hrefs", () => {
  assert.equal(
    administrationViewFromRoute(
      "/settings/administration?view=continuity",
    ),
    "continuity",
  );
  assert.equal(
    administrationViewFromRoute(
      "/settings/administration?view=unknown",
    ),
    "overview",
  );
  assert.equal(
    administrationViewHref("overview"),
    "/settings/administration",
  );
  assert.equal(
    administrationViewHref("members"),
    "/settings/administration?view=members",
  );
});

test("validates retrieval drafts and identifies structural changes", () => {
  const settings = retrieval();
  const draft = retrievalDraft(settings);
  assert.equal(retrievalHasChanges(settings, draft), false);
  assert.equal(retrievalDraftIsValid(settings, draft), true);

  draft.top_k = 8;
  assert.equal(retrievalHasChanges(settings, draft), true);
  assert.deepEqual(retrievalStructuralChanges(settings, draft), []);
  assert.equal(retrievalNeedsPreflight(settings, draft), false);

  draft.model_id = "provider/fixed";
  draft.dimensions = 768;
  assert.equal(retrievalDraftIsValid(settings, draft), false);
  draft.dimensions = 1_024;
  assert.equal(retrievalDraftIsValid(settings, draft), true);
  assert.deepEqual(retrievalStructuralChanges(settings, draft), [
    "Embedding model",
    "Vector dimensions",
  ]);
  assert.equal(retrievalNeedsPreflight(settings, draft), true);
});

test("filters members with role and current-user language", () => {
  const members: FrontendMemberRecord[] = [
    {
      id: 1,
      email: "owner@example.test",
      is_admin: true,
      is_current: true,
      can_delete: false,
      can_change_role: false,
    },
    {
      id: 2,
      email: "editor@example.test",
      is_admin: false,
      is_current: false,
      can_delete: true,
      can_change_role: true,
    },
  ];
  assert.deepEqual(
    filterMembers(members, "admin you").map((member) => member.id),
    [1],
  );
  assert.deepEqual(
    filterMembers(members, "editor").map((member) => member.id),
    [2],
  );
});

test("finds the daily chart maximum for the selected metric", () => {
  const records = [
    {
      date: "2026-07-01",
      source: "chat",
      source_label: "Chat",
      cost: 0.2,
      tokens: 2_000,
      requests: 4,
    },
    {
      date: "2026-07-02",
      source: "runs",
      source_label: "Runs",
      cost: 0.8,
      tokens: 7_500,
      requests: 2,
    },
  ];
  assert.equal(usageDailyMaximum(records, "cost"), 0.8);
  assert.equal(usageDailyMaximum(records, "tokens"), 7_500);
});
