import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

const source = readFileSync(
  resolve(process.cwd(), "src/routes/AddonApplicationPage.svelte"),
  "utf8",
);

test("owns the add-on viewport and lets the mounted app consume it", () => {
  assert.match(
    source,
    /\.addon-application\s*\{[\s\S]*height:\s*calc\(100vh - var\(--shell-header-height\)\);[\s\S]*min-height:\s*0;/,
  );
  assert.match(
    source,
    /\.addon-target\s*\{[\s\S]*height:\s*100%;[\s\S]*min-height:\s*0;/,
  );
  assert.match(
    source,
    /@supports \(height:\s*100dvh\)[\s\S]*\.addon-application[\s\S]*height:\s*calc\(100dvh - var\(--shell-header-height\)\)/,
  );
});
