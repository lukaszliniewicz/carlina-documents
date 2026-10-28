import type {
  FrontendOperationsApproval,
  FrontendOperationsContainer,
  FrontendOperationsJob,
  FrontendOperationsSchedule,
  FrontendOperationsWorker,
} from "@carlina/sdk";

export type RuntimeOperationsView =
  | "overview"
  | "approvals"
  | "jobs"
  | "workers"
  | "schedules"
  | "containers";

export const runtimeOperationsViews: RuntimeOperationsView[] = [
  "overview",
  "approvals",
  "jobs",
  "workers",
  "schedules",
  "containers",
];

const ADMIN_VIEWS = new Set<RuntimeOperationsView>([
  "workers",
  "schedules",
  "containers",
]);

function normalizedRoute(route: string): URL {
  return new URL(route, "https://iarf.local");
}

export function operationsViewFromRoute(
  route: string,
  isAdmin: boolean,
): RuntimeOperationsView {
  const url = normalizedRoute(route);
  const requested = url.searchParams.get("view") as RuntimeOperationsView | null;
  if (url.pathname.replace(/\/+$/, "") === "/approvals") {
    return requested === "jobs" ? "jobs" : "approvals";
  }
  if (
    requested &&
    runtimeOperationsViews.includes(requested) &&
    (isAdmin || !ADMIN_VIEWS.has(requested))
  ) {
    return requested;
  }
  return "overview";
}

export function operationsRoute(
  view: RuntimeOperationsView,
  isAdmin: boolean,
): string {
  if (!isAdmin) {
    return view === "jobs" ? "/approvals?view=jobs" : "/approvals";
  }
  return view === "overview"
    ? "/runtime/processes"
    : `/runtime/processes?view=${encodeURIComponent(view)}`;
}

export function formatOperationsDate(
  value: string | null | undefined,
): string {
  if (!value) return "Not recorded";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "Not recorded";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

export function relativeOperationsDate(
  value: string | null | undefined,
  now = Date.now(),
): string {
  if (!value) return "Not recorded";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "Not recorded";
  const differenceSeconds = Math.round((date.valueOf() - now) / 1_000);
  const absolute = Math.abs(differenceSeconds);
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  if (absolute < 60) return formatter.format(differenceSeconds, "second");
  if (absolute < 3_600) {
    return formatter.format(Math.round(differenceSeconds / 60), "minute");
  }
  if (absolute < 86_400) {
    return formatter.format(Math.round(differenceSeconds / 3_600), "hour");
  }
  return formatter.format(Math.round(differenceSeconds / 86_400), "day");
}

export function operationSearchText(
  value:
    | FrontendOperationsApproval
    | FrontendOperationsJob
    | FrontendOperationsWorker
    | FrontendOperationsSchedule
    | FrontendOperationsContainer,
): string {
  if ("subject_label" in value) {
    return [
      value.title,
      value.summary,
      value.status,
      value.subject_label,
      value.tool_label,
      ...value.effects,
      value.run?.title,
    ]
      .join(" ")
      .toLocaleLowerCase();
  }
  if ("source_label" in value) {
    return [
      value.title,
      value.summary,
      value.status,
      value.source_label,
      value.run?.title,
    ]
      .join(" ")
      .toLocaleLowerCase();
  }
  if ("configured" in value) {
    return [
      value.name,
      value.id,
      value.status,
      value.environment,
      value.entrypoint,
    ]
      .join(" ")
      .toLocaleLowerCase();
  }
  if ("addon_name" in value) {
    return [
      value.title,
      value.job_id,
      value.addon_name,
      value.status,
      value.trigger,
    ]
      .join(" ")
      .toLocaleLowerCase();
  }
  return [value.name, value.status, value.image]
    .join(" ")
    .toLocaleLowerCase();
}

export function matchesOperation(
  searchText: string,
  query: string,
): boolean {
  const terms = query
    .trim()
    .toLocaleLowerCase()
    .split(/\s+/)
    .filter(Boolean);
  return terms.every((term) => searchText.includes(term));
}

export function approvalHasDetail(
  approval: FrontendOperationsApproval,
): boolean {
  const preview = approval.preview;
  const note = approval.resolution_note;
  return (
    approval.effects.length > 0 ||
    (typeof preview === "string"
      ? preview.trim().length > 0
      : Boolean(preview && Object.keys(preview as object).length)) ||
    (typeof note === "string"
      ? note.trim().length > 0
      : Boolean(note && Object.keys(note as object).length))
  );
}
