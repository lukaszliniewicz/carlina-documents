import type {
  FrontendAutomationDefinition,
  FrontendAutomationStepDefinition,
  FrontendAutomationTemplateSummary,
  FrontendAutomationWorkflow,
  FrontendRunGraph,
} from "@carlina/sdk";

export interface AutomationValidationIssue {
  field: string;
  message: string;
  stepKey: string | null;
}

export interface AutomationTemplateSaveInput {
  title: string;
  description: string;
  is_active: boolean;
  definition: FrontendAutomationDefinition;
  change_summary: string;
}

const EMPTY_OBJECT: Readonly<Record<string, unknown>> = {};

function cloneJsonValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(cloneJsonValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, cloneJsonValue(item)]),
    );
  }
  return value;
}

function cloneJsonObject(
  value: Readonly<Record<string, unknown>> | undefined,
): Readonly<Record<string, unknown>> {
  return cloneJsonValue(value ?? EMPTY_OBJECT) as Readonly<
    Record<string, unknown>
  >;
}

export function humanizeAutomation(value: unknown): string {
  const text = String(value ?? "").trim();
  if (!text) return "Not declared";
  return text
    .replace(/[._-]+/g, " ")
    .replace(/\s+/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

export function normalizeAutomationKey(value: unknown): string {
  return String(value ?? "")
    .trim()
    .toLocaleLowerCase()
    .replace(/[^a-z0-9_.-]+/g, "_")
    .replace(/-/g, "_")
    .split("_")
    .filter(Boolean)
    .join("_")
    .slice(0, 128);
}

export function parseAutomationList(value: unknown): string[] {
  const seen = new Set<string>();
  return String(value ?? "")
    .split(/[,\n]/)
    .map((entry) => entry.trim())
    .filter((entry) => {
      if (!entry || seen.has(entry)) return false;
      seen.add(entry);
      return true;
    });
}

export function automationListText(values: readonly string[] | undefined): string {
  return (values ?? []).join(", ");
}

export function newAutomationStep(
  index: number,
): FrontendAutomationStepDefinition {
  const number = Math.max(1, index);
  return {
    key: `step_${number}`,
    title: `Step ${number}`,
    type: "task",
    sequence: number,
    depends_on: [],
    assigned_model_role: null,
    instructions: "",
    capability_ids: [],
    skill_ids: [],
    tool_ids: [],
    provider_requirements: [],
    input_schema: EMPTY_OBJECT,
    output_schema: EMPTY_OBJECT,
    input: EMPTY_OBJECT,
    metadata: EMPTY_OBJECT,
  };
}

export function newAutomationDefinition(): FrontendAutomationDefinition {
  return {
    version: 1,
    name: "Untitled automation",
    description: "",
    steps: [newAutomationStep(1)],
  };
}

export function cloneAutomationDefinition(
  definition: FrontendAutomationDefinition,
): FrontendAutomationDefinition {
  return {
    version: Number(definition.version || 1),
    name: String(definition.name || ""),
    description: String(definition.description || ""),
    steps: (definition.steps ?? []).map((step, index) => ({
      ...step,
      key: String(step.key || `step_${index + 1}`),
      title: String(step.title || ""),
      type: String(step.type || "task"),
      sequence: step.sequence ?? index + 1,
      depends_on: [...(step.depends_on ?? [])],
      assigned_model_role: step.assigned_model_role ?? null,
      instructions: step.instructions ?? "",
      capability_ids: [...(step.capability_ids ?? [])],
      skill_ids: [...(step.skill_ids ?? [])],
      tool_ids: [...(step.tool_ids ?? [])],
      provider_requirements: [...(step.provider_requirements ?? [])],
      input_schema: cloneJsonObject(step.input_schema),
      output_schema: cloneJsonObject(step.output_schema),
      input: cloneJsonObject(step.input),
      metadata: cloneJsonObject(step.metadata),
    })),
  };
}

export function templateSearchText(
  template: FrontendAutomationTemplateSummary,
): string {
  return [
    template.title,
    template.description,
    template.scope,
    template.is_active ? "active" : "inactive",
  ]
    .join(" ")
    .toLocaleLowerCase();
}

export function workflowSearchText(
  workflow: FrontendAutomationWorkflow,
): string {
  return [
    workflow.name,
    workflow.description,
    workflow.id,
    workflow.addon_id,
    workflow.status.state,
    ...Object.values(workflow.requirements).flat(),
    ...workflow.approval_gates,
    ...workflow.expected_jobs.flatMap((item) => [item.id, item.label]),
    ...workflow.expected_artifacts.flatMap((item) => [item.id, item.label]),
  ]
    .join(" ")
    .toLocaleLowerCase();
}

export function automationDefinitionGraph(
  definition: Pick<FrontendAutomationDefinition, "steps">,
): FrontendRunGraph {
  const keys = new Set(
    definition.steps.map(
      (step, index) => step.key || `step_${index + 1}`,
    ),
  );
  const nodes = definition.steps.map((step, index) => {
    const key = step.key || `step_${index + 1}`;
    return {
      id: key,
      key,
      title: step.title || humanizeAutomation(key),
      purpose:
        String(step.instructions || "").trim() ||
        `${humanizeAutomation(step.type)} step`,
      type: step.type || "task",
      status: "planned",
      status_label: "Planned",
      sequence: step.sequence ?? index + 1,
      role: String(step.assigned_model_role || ""),
      parent_id: null,
      dependency_ids: (step.depends_on ?? []).filter((value) =>
        keys.has(value),
      ),
      duration_ms: null,
      progress: null,
      needs_attention: false,
      artifact_count: 0,
      created_at: null,
      started_at: null,
      completed_at: null,
      updated_at: null,
    };
  });
  const edges = nodes.flatMap((node) =>
    node.dependency_ids.map((dependency) => ({
      id: `${dependency}>${node.id}`,
      source: dependency,
      target: node.id,
    })),
  );
  return {
    nodes,
    edges,
    large: nodes.length > 24,
    truncated: false,
    layout_direction: "RIGHT",
  };
}

export function validateAutomationDefinition(
  definition: FrontendAutomationDefinition,
): AutomationValidationIssue[] {
  const issues: AutomationValidationIssue[] = [];
  if (!String(definition.name || "").trim()) {
    issues.push({
      field: "name",
      message: "Give the automation a name.",
      stepKey: null,
    });
  }
  if (!definition.steps.length) {
    issues.push({
      field: "steps",
      message: "Add at least one workflow step.",
      stepKey: null,
    });
    return issues;
  }

  const keys = new Set<string>();
  for (const [index, step] of definition.steps.entries()) {
    const key = normalizeAutomationKey(step.key);
    if (!key) {
      issues.push({
        field: "key",
        message: `Step ${index + 1} needs a stable key.`,
        stepKey: step.key || null,
      });
    } else if (keys.has(key)) {
      issues.push({
        field: "key",
        message: `The key “${key}” is used more than once.`,
        stepKey: key,
      });
    }
    keys.add(key);
    if (!String(step.title || "").trim()) {
      issues.push({
        field: "title",
        message: `Step ${index + 1} needs a title.`,
        stepKey: key || null,
      });
    }
    if (!String(step.type || "").trim()) {
      issues.push({
        field: "type",
        message: `Step ${index + 1} needs a type.`,
        stepKey: key || null,
      });
    }
  }

  const dependencies = new Map<string, string[]>();
  for (const step of definition.steps) {
    const key = normalizeAutomationKey(step.key);
    const values = [...new Set(step.depends_on ?? [])];
    dependencies.set(key, values);
    for (const dependency of values) {
      if (dependency === key) {
        issues.push({
          field: "depends_on",
          message: `“${step.title || key}” cannot depend on itself.`,
          stepKey: key,
        });
      } else if (!keys.has(dependency)) {
        issues.push({
          field: "depends_on",
          message: `“${step.title || key}” depends on a missing step.`,
          stepKey: key,
        });
      }
    }
  }

  const visiting = new Set<string>();
  const complete = new Set<string>();
  const path: string[] = [];
  let cycleReported = false;
  const visit = (key: string) => {
    if (cycleReported || complete.has(key) || !keys.has(key)) return;
    if (visiting.has(key)) {
      const cycleStart = path.indexOf(key);
      const cycle = [...path.slice(cycleStart), key];
      issues.push({
        field: "depends_on",
        message: `Dependency cycle: ${cycle.join(" → ")}.`,
        stepKey: key,
      });
      cycleReported = true;
      return;
    }
    visiting.add(key);
    path.push(key);
    for (const dependency of dependencies.get(key) ?? []) {
      visit(dependency);
    }
    path.pop();
    visiting.delete(key);
    complete.add(key);
  };
  for (const key of keys) visit(key);

  return issues;
}

export function automationRequirementCount(
  workflow: FrontendAutomationWorkflow,
): number {
  return Object.values(workflow.requirements).reduce(
    (total, values) => total + values.length,
    0,
  );
}
