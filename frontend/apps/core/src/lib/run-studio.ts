import type { FrontendRunStudio, FrontendRunStepSummary } from "@carlina/sdk";

export type RunStudioTab =
  | "graph"
  | "timeline"
  | "steps"
  | "artifacts"
  | "sources"
  | "developer";

export const RUN_STUDIO_TABS: readonly RunStudioTab[] = [
  "graph",
  "timeline",
  "steps",
  "artifacts",
  "sources",
  "developer",
] as const;

export function runIdFromRoute(route: string): string | null {
  const pathname = new URL(route, "https://iarf.local").pathname;
  const match = pathname.match(/^\/runs\/([^/]+)\/?$/);
  return match ? decodeURIComponent(match[1]!) : null;
}

export function runTabFromRoute(route: string): RunStudioTab {
  const candidate = new URL(route, "https://iarf.local").searchParams.get("tab");
  return RUN_STUDIO_TABS.includes(candidate as RunStudioTab)
    ? (candidate as RunStudioTab)
    : "graph";
}

export function selectedStepFromRoute(route: string): string | null {
  return new URL(route, "https://iarf.local").searchParams.get("step");
}

export function runStudioRoute(
  runId: string,
  tab: RunStudioTab,
  stepId?: string | null,
): string {
  const params = new URLSearchParams();
  if (tab !== "graph") params.set("tab", tab);
  if (stepId) params.set("step", stepId);
  const query = params.toString();
  return `/runs/${encodeURIComponent(runId)}${query ? `?${query}` : ""}`;
}

export function formatRunDuration(milliseconds: number | null): string {
  if (milliseconds === null) return "Not started";
  const seconds = Math.max(0, Math.round(milliseconds / 1_000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

export function formatRunDate(value: string | null): string {
  if (!value) return "Not recorded";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "Not recorded";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

export function formatRunCost(amount: number, currency: string): string {
  if (amount === 0) return "No model cost";
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency,
    maximumFractionDigits: amount < 0.1 ? 4 : 2,
  }).format(amount);
}

export function dependencyLabels(
  studio: FrontendRunStudio,
  step: FrontendRunStepSummary,
): string[] {
  const titles = new Map(
    studio.graph.nodes.map((node) => [node.id, node.title]),
  );
  return step.dependency_ids.map((id) => titles.get(id) ?? id);
}
