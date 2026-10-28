import type {
  FrontendAdministrationView,
  FrontendMemberRecord,
  FrontendRetrievalConfiguration,
  FrontendRetrievalSettings,
  FrontendUsageDailyRecord,
} from "@carlina/sdk";

const ADMINISTRATION_VIEWS = new Set<FrontendAdministrationView>([
  "overview",
  "retrieval",
  "continuity",
  "members",
  "usage",
]);

export function administrationViewFromRoute(
  route: string,
): FrontendAdministrationView {
  const url = new URL(route, "https://iarf.local");
  const requested = url.searchParams.get("view") as FrontendAdministrationView;
  return ADMINISTRATION_VIEWS.has(requested) ? requested : "overview";
}

export function administrationViewHref(
  view: FrontendAdministrationView,
): string {
  return view === "overview"
    ? "/settings/administration"
    : `/settings/administration?view=${encodeURIComponent(view)}`;
}

export function retrievalDraft(
  settings: FrontendRetrievalSettings,
): FrontendRetrievalConfiguration {
  return { ...settings.configuration };
}

export function retrievalHasChanges(
  settings: FrontendRetrievalSettings,
  draft: FrontendRetrievalConfiguration,
): boolean {
  return (
    draft.model_id !== settings.configuration.model_id ||
    draft.dimensions !== settings.configuration.dimensions ||
    draft.task_type !== settings.configuration.task_type ||
    draft.top_k !== settings.configuration.top_k ||
    draft.chunk_size !== settings.configuration.chunk_size ||
    draft.chunk_overlap !== settings.configuration.chunk_overlap
  );
}

export function retrievalStructuralChanges(
  settings: FrontendRetrievalSettings,
  draft: FrontendRetrievalConfiguration,
): string[] {
  return [
    ["model_id", "Embedding model"],
    ["dimensions", "Vector dimensions"],
    ["task_type", "Embedding intent"],
    ["chunk_size", "Chunk size"],
    ["chunk_overlap", "Chunk overlap"],
  ]
    .filter(([field]) => {
      const key = field as keyof FrontendRetrievalConfiguration;
      return draft[key] !== settings.configuration[key];
    })
    .map(([, label]) => label);
}

export function retrievalNeedsPreflight(
  settings: FrontendRetrievalSettings,
  draft: FrontendRetrievalConfiguration,
): boolean {
  return (
    draft.model_id !== settings.configuration.model_id ||
    draft.dimensions !== settings.configuration.dimensions ||
    draft.task_type !== settings.configuration.task_type
  );
}

export function retrievalDraftIsValid(
  settings: FrontendRetrievalSettings,
  draft: FrontendRetrievalConfiguration,
): boolean {
  const within = (
    value: number,
    field: keyof FrontendRetrievalSettings["limits"],
  ) =>
    Number.isInteger(value) &&
    value >= settings.limits[field].minimum &&
    value <= settings.limits[field].maximum;
  const model = settings.models.find((item) => item.id === draft.model_id);
  return Boolean(
    model &&
      settings.task_types.some((item) => item.id === draft.task_type) &&
      within(draft.dimensions, "dimensions") &&
      within(draft.top_k, "top_k") &&
      within(draft.chunk_size, "chunk_size") &&
      within(draft.chunk_overlap, "chunk_overlap") &&
      draft.chunk_overlap < draft.chunk_size &&
      (model.supports_mrl ||
        model.default_dimensions === null ||
        model.default_dimensions === draft.dimensions),
  );
}

function normalized(value: unknown): string {
  return String(value ?? "").trim().toLocaleLowerCase();
}

export function filterMembers(
  members: FrontendMemberRecord[],
  query: string,
): FrontendMemberRecord[] {
  const terms = normalized(query).split(/\s+/).filter(Boolean);
  if (!terms.length) return members;
  return members.filter((member) => {
    const text = [
      member.email,
      member.is_admin ? "administrator admin" : "member",
      member.is_current ? "current you" : "",
    ]
      .map(normalized)
      .join(" ");
    return terms.every((term) => text.includes(term));
  });
}

export function usageDailyMaximum(
  records: FrontendUsageDailyRecord[],
  metric: "cost" | "tokens" | "requests",
): number {
  return records.reduce(
    (maximum, record) => Math.max(maximum, Number(record[metric]) || 0),
    0,
  );
}

export interface AdministrationStreamEvent {
  type: string;
  message: string;
  progress?: number;
  done?: boolean;
}

async function consumeAdministrationStream(
  response: Response,
  onEvent: (event: AdministrationStreamEvent) => void,
): Promise<void> {
  if (!response.ok) {
    const body = await response.text();
    let message = body || `Request failed (${response.status}).`;
    try {
      const payload = JSON.parse(body) as {
        error?: unknown;
        message?: unknown;
      };
      const detail = payload.error ?? payload.message;
      if (typeof detail === "string" && detail.trim()) message = detail;
    } catch {
      // Plain text is already the most useful fallback.
    }
    throw new Error(message);
  }
  if (!response.body) throw new Error("The server returned no event stream.");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const emit = (frame: string) => {
    const data = frame
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (!data.trim()) return;
    try {
      const payload = JSON.parse(data) as Record<string, unknown>;
      onEvent({
        type: String(payload.type ?? "progress"),
        message: String(payload.message ?? "Administration task updated."),
        ...(typeof payload.progress === "number"
          ? { progress: payload.progress }
          : {}),
        ...(typeof payload.done === "boolean"
          ? { done: payload.done }
          : {}),
      });
    } catch {
      onEvent({ type: "progress", message: data });
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    const frames = buffer.split(/\r?\n\r?\n/);
    buffer = frames.pop() ?? "";
    if (done && buffer.trim()) frames.push(buffer);
    frames.forEach(emit);
    if (done) return;
  }
}

export async function streamAdministrationEvents(
  path: string,
  body: Record<string, unknown>,
  onEvent: (event: AdministrationStreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const url = new URL(path, window.location.origin);
  if (
    url.origin !== window.location.origin ||
    !url.pathname.startsWith(
      "/api/frontend/settings/administration/",
    )
  ) {
    throw new TypeError(
      "Administration streams must use the same-origin administration API.",
    );
  }
  const response = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: {
      Accept: "text/event-stream",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
    signal,
  });
  await consumeAdministrationStream(response, onEvent);
}
