import assert from "node:assert/strict";
import test from "node:test";

import type { FrontendAddonSettingField } from "@carlina/sdk";

import {
  addonSettingOptions,
  buildAddonSettingsDraft,
  changedAddonSettings,
  groupAddonSettings,
} from "../src/lib/addon-settings.ts";

function setting(
  overrides: Partial<FrontendAddonSettingField> = {},
): FrontendAddonSettingField {
  return {
    id: "tone",
    name: "Tone",
    description: "",
    kind: "text",
    scope: "user",
    scope_label: "Your account",
    section: "Writing",
    placeholder: "",
    required: false,
    sensitive: false,
    configured: false,
    editable: true,
    value: "muted",
    options: [],
    options_source: null,
    minimum: null,
    maximum: null,
    step: null,
    rows: 5,
    requires_restart: false,
    mapping: null,
    ...overrides,
  };
}

test("builds isolated drafts and reports only editable changes", () => {
  const fields = [
    setting(),
    setting({
      id: "headers",
      name: "Headers",
      kind: "mapping",
      value: [{ key: "Accept", value: "application/json" }],
      mapping: {
        key: {
          name: "Header",
          kind: "text",
          placeholder: "",
          options: [],
          options_source: null,
        },
        value: {
          name: "Value",
          kind: "text",
          placeholder: "",
          options: [],
          options_source: null,
        },
        add_label: "Add header",
      },
    }),
    setting({
      id: "shared",
      scope: "global",
      scope_label: "Shared installation",
      editable: false,
      value: "admin-owned",
    }),
  ];
  const draft = buildAddonSettingsDraft(fields);

  assert.deepEqual(changedAddonSettings(fields, draft), {});
  (draft.headers as Array<Record<string, string>>)[0].value = "text/plain";
  draft.shared = "attempted-change";

  assert.deepEqual(changedAddonSettings(fields, draft), {
    headers: [{ key: "Accept", value: "text/plain" }],
  });
  assert.deepEqual(fields[1].value, [
    { key: "Accept", value: "application/json" },
  ]);
});

test("keeps configured secrets write-only until a replacement is entered", () => {
  const secret = setting({
    id: "api_key",
    name: "API key",
    kind: "password",
    sensitive: true,
    configured: true,
    value: null,
  });
  const draft = buildAddonSettingsDraft([secret]);

  assert.equal(draft.api_key, "");
  assert.deepEqual(changedAddonSettings([secret], draft), {});

  draft.api_key = "replacement";
  assert.deepEqual(changedAddonSettings([secret], draft), {
    api_key: "replacement",
  });
});

test("groups fields semantically and merges model choices without duplicates", () => {
  const fields = [
    setting(),
    setting({
      id: "model",
      name: "Model",
      section: "Intelligence",
      kind: "model_select",
      value: "provider/current",
      options_source: "llm_models",
      options: [{ value: "provider/fast", label: "Fast" }],
    }),
  ];

  assert.deepEqual(
    groupAddonSettings(fields).map((group) => group.name),
    ["Writing", "Intelligence"],
  );
  assert.deepEqual(
    addonSettingOptions(fields[1], [
      { value: "provider/fast", label: "Fast duplicate" },
      { value: "provider/large", label: "Large" },
    ]),
    [
      { value: "provider/current", label: "Current · provider/current" },
      { value: "provider/fast", label: "Fast" },
      { value: "provider/large", label: "Large" },
    ],
  );
});
