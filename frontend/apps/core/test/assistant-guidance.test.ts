import assert from "node:assert/strict";
import test from "node:test";

import type { FrontendAssistantGuidanceDashboard } from "@carlina/sdk";

import {
  guidanceCustomizedPromptCount,
  guidanceHasChanges,
  guidancePromptDrafts,
  guidancePromptUsesDefault,
} from "../src/lib/assistant-guidance.ts";

function guidance(): FrontendAssistantGuidanceDashboard {
  return {
    frontend_api_version: "1.0",
    generated_at: "2026-07-26T12:00:00Z",
    summary: {
      organization_enabled: true,
      customized_prompts: 1,
      total_prompts: 3,
    },
    organization: {
      enabled: true,
      profile: "Independent editorial organization.",
      truncated: false,
    },
    prompts: [
      {
        id: "chief_assistant",
        name: "Chief assistant",
        description: "Coordinates complex work.",
        value: "Custom chief guidance.",
        default_value: "Default chief guidance.",
        customized: true,
        truncated: false,
      },
      {
        id: "junior_assistant_mandate",
        name: "Junior assistant mandate",
        description: "Guides delegated work.",
        value: "Default junior guidance.",
        default_value: "Default junior guidance.",
        customized: false,
        truncated: false,
      },
      {
        id: "direct_assistant_default",
        name: "Direct assistant",
        description: "Guides direct conversations.",
        value: "Default direct guidance.",
        default_value: "Default direct guidance.",
        customized: false,
        truncated: false,
      },
    ],
    actions: {
      update_url: "/api/frontend/settings/guidance",
    },
    limits: {
      organization_profile_chars: 40_000,
      prompt_chars: 100_000,
    },
  };
}

test("builds a complete prompt draft without leaking setting keys", () => {
  const drafts = guidancePromptDrafts(guidance());

  assert.deepEqual(drafts, {
    chief_assistant: "Custom chief guidance.",
    junior_assistant_mandate: "Default junior guidance.",
    direct_assistant_default: "Default direct guidance.",
  });
});

test("treats blank and maintained copy as the house default", () => {
  assert.equal(guidancePromptUsesDefault("", "House copy"), true);
  assert.equal(guidancePromptUsesDefault(" House copy ", "House copy"), true);
  assert.equal(guidancePromptUsesDefault("Custom copy", "House copy"), false);
});

test("tracks semantic guidance changes and custom prompt count", () => {
  const payload = guidance();
  const drafts = guidancePromptDrafts(payload);

  assert.equal(
    guidanceHasChanges(
      payload,
      payload.organization.enabled,
      payload.organization.profile,
      drafts,
    ),
    false,
  );
  assert.equal(guidanceCustomizedPromptCount(payload, drafts), 1);

  drafts.junior_assistant_mandate = "A narrower delegated mandate.";
  assert.equal(
    guidanceHasChanges(
      payload,
      payload.organization.enabled,
      payload.organization.profile,
      drafts,
    ),
    true,
  );
  assert.equal(guidanceCustomizedPromptCount(payload, drafts), 2);
});
