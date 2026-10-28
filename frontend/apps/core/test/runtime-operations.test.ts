import assert from "node:assert/strict";
import test from "node:test";

import type { FrontendOperationsApproval } from "@carlina/sdk";

import {
  approvalHasDetail,
  matchesOperation,
  operationSearchText,
  operationsRoute,
  operationsViewFromRoute,
} from "../src/lib/runtime-operations.ts";

const approval: FrontendOperationsApproval = {
  id: "approval-1",
  title: "Publish autumn editorial",
  summary: "May affect External publish.",
  status: "pending",
  status_label: "Pending",
  subject_label: "Workflow run",
  tool_label: "WordPress publish",
  effects: ["External publish"],
  preview: { destination: "Editorial review" },
  resolution_note: "",
  created_at: "2026-07-26T12:00:00",
  expires_at: null,
  resolved_at: null,
  can_approve: true,
  can_reject: true,
  can_cancel: true,
  resolve_url: "/api/runtime/approvals/approval-1",
  run: {
    id: "run-1",
    title: "Autumn editorial",
    url: "/runs/run-1",
  },
  log_links: [],
};

test("derives safe operations views from document and query routes", () => {
  assert.equal(operationsViewFromRoute("/approvals", false), "approvals");
  assert.equal(
    operationsViewFromRoute("/runtime/processes?view=workers", true),
    "workers",
  );
  assert.equal(
    operationsViewFromRoute("/runtime/processes?view=workers", false),
    "overview",
  );
  assert.equal(
    operationsViewFromRoute("/runtime/processes?view=unknown", true),
    "overview",
  );
});

test("builds stable client routes for admin and personal operation views", () => {
  assert.equal(operationsRoute("overview", true), "/runtime/processes");
  assert.equal(
    operationsRoute("approvals", true),
    "/runtime/processes?view=approvals",
  );
  assert.equal(operationsRoute("approvals", false), "/approvals");
  assert.equal(operationsRoute("jobs", false), "/approvals?view=jobs");
});

test("searches semantic labels without relying on raw records", () => {
  const searchable = operationSearchText(approval);
  assert.ok(matchesOperation(searchable, "wordpress autumn"));
  assert.ok(matchesOperation(searchable, "external publish"));
  assert.equal(matchesOperation(searchable, "docker"), false);
  assert.equal(approvalHasDetail(approval), true);
});
