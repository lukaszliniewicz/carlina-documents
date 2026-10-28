import assert from "node:assert/strict";
import test from "node:test";

import type {
  FrontendAutomationDefinition,
  FrontendAutomationTemplateSummary,
  FrontendAutomationWorkflow,
} from "@carlina/sdk";

import {
  automationDefinitionGraph,
  cloneAutomationDefinition,
  normalizeAutomationKey,
  parseAutomationList,
  templateSearchText,
  validateAutomationDefinition,
  workflowSearchText,
} from "../src/lib/automations.ts";

function definition(): FrontendAutomationDefinition {
  return {
    version: 1,
    name: "Editorial brief",
    description: "A restrained research workflow",
    steps: [
      {
        key: "research",
        title: "Research sources",
        type: "research",
        depends_on: [],
        assigned_model_role: "researcher",
        instructions: "Find primary sources.",
        capability_ids: ["web_search.research"],
        skill_ids: [],
        tool_ids: ["web_search.text"],
        provider_requirements: [],
        metadata: { completion_requirements: { minimum_sources: 3 } },
      },
      {
        key: "draft",
        title: "Draft brief",
        type: "write",
        depends_on: ["research"],
        assigned_model_role: "editor",
        instructions: "Compose a cited briefing.",
        capability_ids: [],
        skill_ids: [],
        tool_ids: [],
        provider_requirements: [],
      },
    ],
  };
}

test("normalizes stable keys and semantic binding lists", () => {
  assert.equal(normalizeAutomationKey("  Review & Publish  "), "review_publish");
  assert.deepEqual(
    parseAutomationList("web.search, documents.write\nweb.search"),
    ["web.search", "documents.write"],
  );
});

test("maps visual definitions to dependency graphs", () => {
  const graph = automationDefinitionGraph(definition());

  assert.equal(graph.nodes.length, 2);
  assert.deepEqual(graph.nodes[1]!.dependency_ids, ["research"]);
  assert.deepEqual(graph.edges, [
    {
      id: "research>draft",
      source: "research",
      target: "draft",
    },
  ]);
});

test("rejects missing, duplicate, and cyclic dependencies before save", () => {
  const cyclic = definition();
  cyclic.steps[0]!.depends_on = ["draft"];
  const cycleIssues = validateAutomationDefinition(cyclic);
  assert.match(
    cycleIssues.find((issue) => issue.field === "depends_on")?.message ?? "",
    /Dependency cycle/,
  );

  const invalid = definition();
  invalid.steps[1]!.key = "research";
  invalid.steps[1]!.title = "";
  const messages = validateAutomationDefinition(invalid).map(
    (issue) => issue.message,
  );
  assert.ok(messages.some((message) => message.includes("used more than once")));
  assert.ok(messages.some((message) => message.includes("needs a title")));
});

test("cloning a definition preserves execution fields without shared mutation", () => {
  const source = definition();
  const cloned = cloneAutomationDefinition(source);

  cloned.steps[0]!.title = "Changed";
  (
    cloned.steps[0]!.metadata as {
      completion_requirements: { minimum_sources: number };
    }
  ).completion_requirements.minimum_sources = 5;

  assert.equal(source.steps[0]!.title, "Research sources");
  assert.equal(
    (
      source.steps[0]!.metadata as {
        completion_requirements: { minimum_sources: number };
      }
    ).completion_requirements.minimum_sources,
    3,
  );
});

test("search text covers template scope and workflow requirements", () => {
  const template: FrontendAutomationTemplateSummary = {
    id: "template-1",
    title: "Editorial brief",
    description: "Muted autumn research",
    owner_id: 7,
    scope: "personal",
    editable: true,
    is_active: true,
    active_version: 2,
    version_count: 2,
    step_count: 2,
    definition_error: null,
    created_at: null,
    updated_at: null,
  };
  const workflow: FrontendAutomationWorkflow = {
    id: "web_search.research",
    name: "Research briefing",
    description: "Current source synthesis",
    addon_id: "web_search",
    status: { state: "active", reasons: [] },
    available: true,
    dry_run_supported: true,
    execution_supported: false,
    definition_error: null,
    steps: [],
    requirements: {
      addons: [],
      tools: ["web_search.text"],
      tool_views: [],
      helpers: [],
      providers: [],
      services: [],
      workers: [],
      auth_scopes: [],
      resource_scopes: [],
    },
    approval_gates: ["user.confirm_publish"],
    expected_jobs: [],
    expected_artifacts: [
      { id: "brief", label: "Cited briefing", type: "markdown" },
    ],
    links: [],
    launch_url: "/",
  };

  assert.match(templateSearchText(template), /personal/);
  assert.match(workflowSearchText(workflow), /web_search\.text/);
  assert.match(workflowSearchText(workflow), /cited briefing/);
});
