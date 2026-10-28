import assert from "node:assert/strict";
import test from "node:test";

import type { FrontendAssistantPolicyDashboard } from "@carlina/sdk";

import {
  assistantPolicyDraft,
  assistantPolicyDraftIsValid,
  assistantPolicyHasChanges,
  assistantPolicyNeedsFullAccessAcknowledgement,
} from "../src/lib/assistant-policy.ts";

function policy(): FrontendAssistantPolicyDashboard {
  return {
    frontend_api_version: "1.0",
    generated_at: "2026-07-27T12:00:00Z",
    summary: {
      approval_mode: "review",
      tool_budget: 30,
      user_hot_limit: 80,
      archive_recall_default: false,
      configuration_needs_review: false,
    },
    runtime: {
      approval_mode: "review",
      max_tool_calls: 30,
      effective_max_tool_calls: 30,
      tool_budget_supported: true,
    },
    memory: {
      user_hot_limit: 80,
      organization_hot_limit: 120,
      include_archive_by_default: false,
      fallback_categories: [
        { key: "projects", label: "Projects" },
        { key: "other", label: "Other" },
      ],
    },
    defaults: {
      runtime: {
        approval_mode: "review",
        max_tool_calls: 30,
      },
      memory: {
        user_hot_limit: 80,
        organization_hot_limit: 120,
        include_archive_by_default: false,
      },
    },
    limits: {
      max_tool_calls: { minimum: 1, maximum: 50 },
      hot_memory: { minimum: 10, maximum: 500 },
    },
    actions: {
      update_url: "/api/frontend/settings/policy",
    },
  };
}

test("builds the semantic policy draft and tracks changes", () => {
  const payload = policy();
  const draft = assistantPolicyDraft(payload);

  assert.deepEqual(draft, {
    approvalMode: "review",
    maxToolCalls: 30,
    userHotLimit: 80,
    organizationHotLimit: 120,
    includeArchiveByDefault: false,
  });
  assert.equal(assistantPolicyHasChanges(payload, draft), false);

  draft.organizationHotLimit = 160;
  assert.equal(assistantPolicyHasChanges(payload, draft), true);
});

test("validates integer bounds for runtime and memory limits", () => {
  const payload = policy();
  const draft = assistantPolicyDraft(payload);

  assert.equal(assistantPolicyDraftIsValid(payload, draft), true);
  draft.maxToolCalls = 51;
  assert.equal(assistantPolicyDraftIsValid(payload, draft), false);
  draft.maxToolCalls = 30;
  draft.userHotLimit = 10.5;
  assert.equal(assistantPolicyDraftIsValid(payload, draft), false);
});

test("requires acknowledgement only when newly enabling full access", () => {
  const payload = policy();
  const draft = assistantPolicyDraft(payload);
  draft.approvalMode = "full_access";

  assert.equal(
    assistantPolicyNeedsFullAccessAcknowledgement(payload, draft),
    true,
  );

  payload.runtime.approval_mode = "full_access";
  assert.equal(
    assistantPolicyNeedsFullAccessAcknowledgement(payload, draft),
    false,
  );
});
