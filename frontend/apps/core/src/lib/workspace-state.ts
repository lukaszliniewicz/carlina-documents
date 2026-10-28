import type {
  FrontendAgentEvent,
  FrontendArtifactSummary,
} from "@carlina/sdk";

export type WorkspaceRefreshTarget =
  | "summary"
  | "runs"
  | "memory"
  | "context"
  | "artifacts";

export interface StructuredRow {
  path: string;
  label: string;
  value: string;
  kind: "text" | "number" | "boolean" | "empty";
  depth: number;
}

export interface ArtifactViewerDescriptor {
  id:
    | "image"
    | "audio"
    | "video"
    | "pdf"
    | "markdown"
    | "html"
    | "structured"
    | "text"
    | "download";
  label: string;
  needsContent: boolean;
  canEmbedDownload: boolean;
}

const VIEWERS: Readonly<Record<string, ArtifactViewerDescriptor>> = {
  image: {
    id: "image",
    label: "Image",
    needsContent: false,
    canEmbedDownload: true,
  },
  audio: {
    id: "audio",
    label: "Audio",
    needsContent: false,
    canEmbedDownload: true,
  },
  video: {
    id: "video",
    label: "Video",
    needsContent: false,
    canEmbedDownload: true,
  },
  pdf: {
    id: "pdf",
    label: "PDF",
    needsContent: false,
    canEmbedDownload: true,
  },
  markdown: {
    id: "markdown",
    label: "Markdown",
    needsContent: true,
    canEmbedDownload: false,
  },
  html: {
    id: "html",
    label: "HTML",
    needsContent: true,
    canEmbedDownload: false,
  },
  structured: {
    id: "structured",
    label: "Structured data",
    needsContent: true,
    canEmbedDownload: false,
  },
  text: {
    id: "text",
    label: "Text",
    needsContent: true,
    canEmbedDownload: false,
  },
  download: {
    id: "download",
    label: "File",
    needsContent: false,
    canEmbedDownload: false,
  },
};

export function humanizeWorkspaceLabel(value: string): string {
  const clean = value
    .replaceAll(/[_\-.]+/g, " ")
    .replaceAll(/([a-z0-9])([A-Z])/g, "$1 $2")
    .trim()
    .replaceAll(/\s+/g, " ");
  return clean ? clean[0]!.toUpperCase() + clean.slice(1) : "Value";
}

function scalarKind(value: unknown): StructuredRow["kind"] {
  if (value === null || value === undefined || value === "") return "empty";
  if (typeof value === "boolean") return "boolean";
  if (typeof value === "number") return "number";
  return "text";
}

export function formatStructuredScalar(value: unknown): string {
  if (value === null || value === undefined || value === "") return "Not set";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") {
    return Number.isInteger(value)
      ? value.toLocaleString()
      : value.toLocaleString(undefined, { maximumFractionDigits: 4 });
  }
  return String(value);
}

export function structuredRows(
  value: unknown,
  options: { maxRows?: number; maxDepth?: number } = {},
): StructuredRow[] {
  const maxRows = Math.max(1, options.maxRows ?? 60);
  const maxDepth = Math.max(1, options.maxDepth ?? 5);
  const rows: StructuredRow[] = [];
  let truncated = false;

  function visit(
    current: unknown,
    path: string,
    label: string,
    depth: number,
  ) {
    if (rows.length >= maxRows) {
      truncated = true;
      return;
    }
    if (
      current === null ||
      current === undefined ||
      ["string", "number", "boolean"].includes(typeof current)
    ) {
      rows.push({
        path: path || "value",
        label: humanizeWorkspaceLabel(label || "value"),
        value: formatStructuredScalar(current),
        kind: scalarKind(current),
        depth,
      });
      return;
    }
    if (depth >= maxDepth) {
      rows.push({
        path: path || "value",
        label: humanizeWorkspaceLabel(label || "value"),
        value: "Additional nested detail omitted",
        kind: "text",
        depth,
      });
      return;
    }
    if (Array.isArray(current)) {
      if (!current.length) {
        rows.push({
          path: path || "value",
          label: humanizeWorkspaceLabel(label || "value"),
          value: "None",
          kind: "empty",
          depth,
        });
        return;
      }
      const allScalar = current.every(
        (item) =>
          item === null ||
          item === undefined ||
          ["string", "number", "boolean"].includes(typeof item),
      );
      if (allScalar) {
        rows.push({
          path: path || "value",
          label: humanizeWorkspaceLabel(label || "value"),
          value: current.map(formatStructuredScalar).join(" · "),
          kind: "text",
          depth,
        });
        return;
      }
      current.forEach((item, index) => {
        visit(
          item,
          `${path}[${index}]`,
          `${label || "Item"} ${index + 1}`,
          depth + 1,
        );
      });
      return;
    }
    if (typeof current === "object") {
      const entries = Object.entries(current as Readonly<Record<string, unknown>>);
      if (!entries.length) {
        rows.push({
          path: path || "value",
          label: humanizeWorkspaceLabel(label || "value"),
          value: "No recorded fields",
          kind: "empty",
          depth,
        });
        return;
      }
      entries.forEach(([key, item]) => {
        visit(
          item,
          path ? `${path}.${key}` : key,
          key,
          depth + (path ? 1 : 0),
        );
      });
      return;
    }
    rows.push({
      path: path || "value",
      label: humanizeWorkspaceLabel(label || "value"),
      value: String(current),
      kind: "text",
      depth,
    });
  }

  visit(value, "", "value", 0);
  if (truncated) {
    rows[maxRows - 1] = {
      path: "additional",
      label: "Additional detail",
      value: "More fields are available in the source record",
      kind: "text",
      depth: 0,
    };
  }
  return rows;
}

export function resolveArtifactViewer(
  artifact: Pick<
    FrontendArtifactSummary,
    "preview_kind" | "mime_type" | "label"
  >,
): ArtifactViewerDescriptor {
  const declared = VIEWERS[artifact.preview_kind];
  if (declared) return declared;
  const mime = artifact.mime_type.toLowerCase();
  const label = artifact.label.toLowerCase();
  if (mime.startsWith("image/")) return VIEWERS.image!;
  if (mime.startsWith("audio/")) return VIEWERS.audio!;
  if (mime.startsWith("video/")) return VIEWERS.video!;
  if (mime === "application/pdf" || label.endsWith(".pdf")) return VIEWERS.pdf!;
  if (mime.includes("json") || label.endsWith(".json")) return VIEWERS.structured!;
  if (mime.includes("markdown") || label.endsWith(".md")) return VIEWERS.markdown!;
  if (mime.startsWith("text/")) return VIEWERS.text!;
  return VIEWERS.download!;
}

export function trustedArtifactAssetUrl(
  value: string | null | undefined,
  origin = "https://iarf.local",
): string | null {
  if (!value) return null;
  try {
    const target = new URL(value, origin);
    if (
      target.origin !== new URL(origin).origin ||
      !target.pathname.startsWith("/api/runtime/artifacts/")
    ) {
      return null;
    }
    return `${target.pathname}${target.search}`;
  } catch {
    return null;
  }
}

function objectValue(
  value: unknown,
): Readonly<Record<string, unknown>> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Readonly<Record<string, unknown>>)
    : null;
}

export function workspaceRefreshTargets(
  event: FrontendAgentEvent | Readonly<Record<string, unknown>>,
): Set<WorkspaceRefreshTarget> {
  const targets = new Set<WorkspaceRefreshTarget>(["summary"]);
  const kind = String(event.kind ?? "").toLowerCase();
  const subject = objectValue(event.subject);
  const subjectType = String(subject?.type ?? "").toLowerCase();
  const payload = objectValue(event.payload);
  const references =
    objectValue(payload?.runtime_references) ??
    objectValue(payload?.references);

  const combined = `${kind} ${subjectType}`;
  if (/(artifact|document|file_output)/.test(combined)) {
    targets.add("artifacts");
  }
  if (/(memory|context\.working)/.test(combined)) {
    targets.add("memory");
  }
  if (/(context_item|context\.item|context\.lifecycle|context\.materialization)/.test(combined)) {
    targets.add("context");
  }
  if (/(run|workflow|step|approval|interaction|job)/.test(combined)) {
    targets.add("runs");
  }

  const artifactIds =
    references?.artifact_ids ??
    payload?.artifact_ids ??
    (subjectType === "artifact" ? [subject?.id] : []);
  if (Array.isArray(artifactIds) && artifactIds.some(Boolean)) {
    targets.add("artifacts");
  }
  if (
    references?.run_id ||
    references?.workflow_run_id ||
    payload?.run_id ||
    payload?.workflow_run_id
  ) {
    targets.add("runs");
  }
  return targets;
}

export function mergeWorkspacePage<T extends { id: string }>(
  current: T[],
  incoming: T[],
): T[] {
  const byId = new Map(current.map((item) => [item.id, item]));
  incoming.forEach((item) => byId.set(item.id, item));
  return [...byId.values()];
}

export function formatWorkspaceBytes(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "Size unavailable";
  if (value < 1_024) return `${value} B`;
  if (value < 1_048_576) return `${(value / 1_024).toFixed(1)} KB`;
  if (value < 1_073_741_824) {
    return `${(value / 1_048_576).toFixed(1)} MB`;
  }
  return `${(value / 1_073_741_824).toFixed(1)} GB`;
}
