import js from "@eslint/js";
import { defineConfig, globalIgnores } from "eslint/config";
import svelte from "eslint-plugin-svelte";
import globals from "globals";
import ts from "typescript-eslint";

export default defineConfig(
  globalIgnores([
    "**/.codegraph/**",
    "**/.pixi/**",
    "**/build/**",
    "**/coverage/**",
    "**/dist/**",
    "**/env/**",
    "**/.venv/**",
    "**/venv/**",
    "**/graphify-out/**",
    "iarf_assistant/addons/*/lib/**",
    "**/node_modules/**",
    "**/static/dist/**",
    "iarf_assistant/static/frontend/**",
    "clients/compass-desktop/src-tauri/target/**",
  ]),
  js.configs.recommended,
  ts.configs.recommended,
  svelte.configs.recommended,
  {
    languageOptions: {
      globals: {
        ...globals.browser,
        ...globals.node,
      },
    },
    rules: {
      "no-unused-vars": "off",
      // This is formatting advice rather than a defect boundary.
      "prefer-const": "off",
      "@typescript-eslint/no-explicit-any": "off",
      "@typescript-eslint/no-unused-vars": [
        "error",
        {
          argsIgnorePattern: "^_",
          caughtErrorsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
        },
      ],
    },
  },
  {
    files: ["**/*.{ts,mts,cts}", "**/*.svelte"],
    rules: {
      // Type names and Svelte template bindings are resolved by TypeScript and
      // svelte-check, not JavaScript's global-variable rule.
      "no-undef": "off",
    },
  },
  {
    files: ["**/*.svelte", "**/*.svelte.js", "**/*.svelte.ts"],
    languageOptions: {
      parserOptions: {
        parser: ts.parser,
      },
    },
    rules: {
      // svelte-check owns unused template/script bindings. The generic
      // TypeScript rule currently crashes on valid Svelte 5 template scopes.
      "@typescript-eslint/no-unused-vars": "off",
      "@typescript-eslint/no-unused-expressions": "off",
      // ESLint's control-flow model does not follow Svelte reactive
      // assignments; svelte-check validates those bindings instead.
      "no-useless-assignment": "off",
      // These recommended rules are valuable migration inventories, but the
      // current UI intentionally has a large pre-existing baseline. Keep them
      // out of the blocking lane until each owning workspace is ratcheted.
      "svelte/prefer-svelte-reactivity": "off",
      "svelte/require-each-key": "off",
    },
  },
  {
    files: ["**/static/**/*.{js,mjs,cjs}"],
    rules: {
      // These compatibility scripts intentionally consume globals installed by
      // templates and separately loaded bundles. Svelte/TypeScript source does
      // not receive this exception.
      "no-undef": "off",
    },
  },
  {
    files: ["iarf_assistant/static/js/global_helpers.js"],
    rules: {
      // Keep the escaped closing tag safe if this compatibility loader is ever
      // embedded in an HTML script block.
      "no-useless-escape": "off",
    },
  },
  {
    files: [
      "clients/compass-desktop/browser-extension/src/background.js",
      "clients/compass-desktop/src/App.svelte",
    ],
    rules: {
      // These expressions deliberately reject the C0 control range.
      "no-control-regex": "off",
    },
  },
  {
    files: ["iarf_assistant/addons/foxscrape/app/js/**/*.js"],
    rules: {
      // Browser-evaluated function expressions are the public payload shape.
      "@typescript-eslint/no-unused-expressions": "off",
    },
  },
  {
    files: [
      "frontend/apps/core/src/components/chat/MessageCard.svelte",
      "frontend/apps/core/src/components/context/ArtifactViewer.svelte",
      "iarf_assistant/addons/doc_processor/frontend/src/components/DocumentPreviewModal.svelte",
      "iarf_assistant/addons/email/frontend/src/MailboxBrowser.svelte",
      "iarf_assistant/addons/translator/frontend/src/workspace/WorkspaceApp.svelte",
    ],
    rules: {
      // These established renderers sanitize or constrain HTML before the
      // template boundary. New renderers still fail the default rule.
      "svelte/no-at-html-tags": "off",
    },
  },
  {
    files: [
      "iarf_assistant/addons/doc_processor/frontend/src/components/CropModal.svelte",
      "iarf_assistant/addons/doc_processor/frontend/src/components/DocumentPreviewModal.svelte",
      "iarf_assistant/addons/doc_processor/frontend/src/components/PromptStudioModal.svelte",
    ],
    rules: {
      // Known legacy Svelte-4-style imperative/reactive boundaries. Their
      // behavior is covered by workspace checks/tests and remains migration
      // debt; new files receive the recommended rules.
      "svelte/infinite-reactive-loop": "off",
      "svelte/no-dom-manipulating": "off",
      "svelte/no-immutable-reactive-statements": "off",
    },
  },
);
