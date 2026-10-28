import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test, { type TestContext } from "node:test";

import { coreRouteAliases, frontendBuildProfile } from "../src/lib/frontend-build-profile.ts";

function fixture(context: TestContext): string {
  const root = mkdtempSync(join(tmpdir(), "carlina-frontend-profile-"));
  context.after(() => rmSync(root, { recursive: true, force: true }));
  return join(root, "distribution.json");
}

function documentsDescriptor(): Record<string, unknown> {
  return {
    format: "carlina.application-distribution",
    schema_version: 1,
    application_profile: "documents",
    source_revision: "a".repeat(40),
    source_date_epoch: 1791320000,
    addon_ids: ["doc_processor", "sentence_segmenter", "translator"],
  };
}

test("descriptor-free source builds select Workbench routes", (context) => {
  const path = fixture(context);
  assert.equal(frontendBuildProfile(path), "workbench");
  assert.ok(coreRouteAliases(path)["@carlina/core-routes"].endsWith("workbench-route-loaders.ts"));
});

test("generated Documents identity selects only the Documents route module", (context) => {
  const path = fixture(context);
  writeFileSync(path, JSON.stringify(documentsDescriptor()));
  assert.equal(frontendBuildProfile(path), "documents");
  assert.ok(coreRouteAliases(path)["@carlina/core-routes"].endsWith("documents-route-loaders.ts"));
});

test("invalid identity cannot silently select Workbench or arbitrary routes", (context) => {
  const path = fixture(context);
  const invalid: unknown[] = [
    null, [], {},
    { ...documentsDescriptor(), format: "unrecognized" },
    { ...documentsDescriptor(), schema_version: 2 },
    { ...documentsDescriptor(), schema_version: true },
    { ...documentsDescriptor(), application_profile: "workbench" },
    { ...documentsDescriptor(), application_profile: "../../private" },
    { ...documentsDescriptor(), source_revision: "A".repeat(40) },
    { ...documentsDescriptor(), source_date_epoch: 0 },
    { ...documentsDescriptor(), source_date_epoch: 1.5 },
    { ...documentsDescriptor(), source_date_epoch: true },
    { ...documentsDescriptor(), addon_ids: ["translator"] },
    { ...documentsDescriptor(), extra: "private descriptor content" },
  ];
  for (const value of invalid) {
    writeFileSync(path, JSON.stringify(value));
    assert.throws(() => coreRouteAliases(path), {
      message: "Application distribution descriptor is malformed.",
    });
  }
  for (const bytes of [Buffer.from("not JSON"), Buffer.alloc(4097, 32), Buffer.from([0xff])]) {
    writeFileSync(path, bytes);
    assert.throws(() => frontendBuildProfile(path), {
      message: "Application distribution descriptor is malformed.",
    });
  }
});

test("descriptor directories are rejected", (context) => {
  const path = fixture(context);
  mkdirSync(path);
  assert.throws(() => frontendBuildProfile(path), {
    message: "Application distribution descriptor is malformed.",
  });
});

test("descriptor symlinks are rejected", { skip: process.platform === "win32" }, (context) => {
  const path = fixture(context);
  const target = `${path}.original`;
  writeFileSync(target, JSON.stringify(documentsDescriptor()));
  symlinkSync(target, path);
  assert.throws(() => frontendBuildProfile(path), {
    message: "Application distribution descriptor is malformed.",
  });
});
