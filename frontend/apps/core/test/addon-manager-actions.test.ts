import assert from "node:assert/strict";
import test from "node:test";

import type { FrontendAddonManagerItem } from "@carlina/sdk";

import {
  createAddonJobAction,
  createAddonLifecycleAction,
  createAddonWorkerAction,
  createServerRestartAction,
} from "../src/lib/addon-manager-actions.ts";

function addon(
  overrides: Partial<FrontendAddonManagerItem> = {},
): FrontendAddonManagerItem {
  return {
    id: "email",
    name: "Email",
    dependency_notices: [],
    ...overrides,
  } as FrontendAddonManagerItem;
}

test("builds activation review from only dependencies that require approval", () => {
  const action = createAddonLifecycleAction(
    "activate",
    addon({
      dependency_notices: [
        {
          id: "imap-license",
          packages: ["imapclient"],
          license: "BSD-3-Clause",
          purpose: "IMAP access",
          review_required: true,
        },
        {
          id: "already-owned",
          packages: ["stdlib"],
          license: "PSF",
          purpose: "Core runtime",
          review_required: false,
        },
      ],
    }),
  );

  assert.deepEqual(action.command, {
    kind: "activate",
    addonId: "email",
    dependencyReview: ["imap-license"],
  });
  assert.equal(action.requiresIdentifier, true);
  assert.equal(action.confirmationPhrase, "email");
  assert.match(action.description, /imapclient \(BSD-3-Clause\)/);
  assert.doesNotMatch(action.description, /stdlib/);
});

test("keeps purge confirmation distinct from code removal", () => {
  const remove = createAddonLifecycleAction("uninstall", addon());
  const purge = createAddonLifecycleAction("purge", addon());

  assert.equal(remove.confirmationPhrase, "email");
  assert.deepEqual(remove.command, { kind: "uninstall", addonId: "email" });
  assert.equal(purge.confirmationPhrase, "PURGE email");
  assert.deepEqual(purge.command, {
    kind: "purge",
    addonId: "email",
    confirmation: "PURGE email",
  });
  assert.match(remove.description, /data.*preserved/i);
  assert.match(purge.description, /permanently removes/i);
});

test("builds worker, schedule, and server commands without transport paths", () => {
  assert.deepEqual(createAddonWorkerAction("restart-workers", addon()).command, {
    kind: "restart-workers",
    addonId: "email",
  });
  assert.deepEqual(createAddonJobAction("run-job", addon(), "sync/daily").command, {
    kind: "run-job",
    addonId: "email",
    jobId: "sync/daily",
  });
  assert.deepEqual(createServerRestartAction().command, {
    kind: "restart-server",
  });
});
