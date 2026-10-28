export const ACTIVE_PROJECT_STATUSES = new Set([
  "extracting",
  "processing_pdf",
  "analyzing_structure",
  "analyzing_terminology",
  "translating",
  "exporting"
]);
export const PROJECT_STATUS_CONTRACT = "translator-project-status-v1";
export const MAX_CHUNK_SIZE_CHARS = 20_000;

export const SETUP_STATUSES = new Set(["setup", "analyzing_terminology"]);

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

export function validateProjectStatusPayload(payload, projectId) {
  if (!isRecord(payload)
    || Object.keys(payload).sort().join(",") !== "contract,latest_operation,project_id,status"
    || payload.contract !== PROJECT_STATUS_CONTRACT) {
    throw new Error("This project returned an incompatible status contract.");
  }
  if (typeof payload.project_id !== "string"
    || !payload.project_id
    || payload.project_id !== String(projectId)) {
    throw new Error("Invalid project status response: project mismatch.");
  }
  if (typeof payload.status !== "string" || !payload.status.trim()) {
    throw new Error("Invalid project status response: status.");
  }
  if (!isRecord(payload.latest_operation)) {
    throw new Error("Invalid project status response: latest operation.");
  }
  return payload;
}

export function formatStatus(status) {
  return String(status || "")
    .replace(/_/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

export function isApprovedStatus(status) {
  return status === "approved" || status === "human_approved";
}

export function normaliseModels(rawModels) {
  const rows = Array.isArray(rawModels)
    ? rawModels
    : (Array.isArray(rawModels?.models) ? rawModels.models : []);
  return rows
    .map((model) => ({
      id: model?.id || model?.model_id || model?.name || "",
      name: model?.name || model?.model_id || model?.id || "",
      provider: model?.provider_name || model?.provider || "Other",
      ready: model?.ready !== false && model?.configured !== false,
      readiness_reason: model?.readiness_reason || model?.reason || "",
      supports_reasoning: Boolean(model?.supports_reasoning),
      supported_reasoning_levels: Array.isArray(model?.supported_reasoning_levels)
        ? model.supported_reasoning_levels.map(String)
        : String(model?.supported_reasoning_levels || "").split(",").map((value) => value.trim()).filter(Boolean)
    }))
    .filter((model) => model.id && model.name && model.ready);
}

export function modelSetupMessage(models, loading = false) {
  if (loading) return "Checking configured models...";
  if (Array.isArray(models) && models.length) return "";
  return "No usable model is configured for direct API translation. Configure a provider in Model Settings, or complete passive jobs through MCP.";
}

export function modelSettingsHref() {
  return "/settings/models";
}

export function selectReadyModel(models, savedModel = "") {
  const options = Array.isArray(models) ? models : [];
  return options.some((model) => model.id === savedModel) ? savedModel : (options[0]?.id || "");
}

export function allowedExportFormats({ guestMode = false, isSubtitleProject = false } = {}) {
  if (guestMode) return [{ value: "html", label: "Raw HTML", extension: ".html" }];
  if (isSubtitleProject) {
    return [
      { value: "srt", label: "SubRip", extension: ".srt" },
      { value: "vtt", label: "WebVTT", extension: ".vtt" },
      { value: "subtitle_txt", label: "Plain Text", extension: ".txt" },
      { value: "subtitle_json", label: "Timed Cues", extension: ".json" }
    ];
  }
  return [
    { value: "html", label: "Raw HTML", extension: ".html" },
    { value: "plain", label: "Plain Text", extension: ".txt" },
    { value: "docx", label: "Microsoft Word", extension: ".docx" },
    { value: "epub", label: "EPUB", extension: ".epub" }
  ];
}

export function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function highlightGlossaryTerms(sourceText, glossaryItems) {
  const source = String(sourceText ?? "");
  const bySource = new Map();
  for (const term of Array.isArray(glossaryItems) ? glossaryItems : []) {
    const sourceTerm = String(term?.source_term ?? "").trim();
    const key = sourceTerm.toLocaleLowerCase();
    if (sourceTerm && !bySource.has(key)) bySource.set(key, { ...term, source_term: sourceTerm });
    if (bySource.size >= 1000) break;
  }
  const terms = [...bySource.values()].sort(
    (left, right) => right.source_term.length - left.source_term.length
  );
  if (!terms.length) return escapeHtml(source);

  const alternatives = terms.map((term) => escapeRegExp(term.source_term)).join("|");
  const matcher = new RegExp(
    `(^|[^\\p{L}\\p{N}_])(${alternatives})(?=$|[^\\p{L}\\p{N}_])`,
    "giu"
  );
  let rendered = "";
  let cursor = 0;
  for (let match = matcher.exec(source); match; match = matcher.exec(source)) {
    const matchedText = match[2];
    const termStart = match.index + match[1].length;
    const term = bySource.get(matchedText.toLocaleLowerCase());
    if (!term) continue;
    rendered += escapeHtml(source.slice(cursor, termStart));
    const safeText = escapeHtml(matchedText);
    const safeSource = escapeHtml(term.source_term);
    const safeTarget = escapeHtml(term?.target_term ?? "");
    const safeNotes = escapeHtml(term?.context_notes ?? "");
    const safeId = escapeHtml(term?.id ?? "");
    rendered += `<mark class="glossary-highlight" tabindex="0" role="button" aria-label="Glossary term ${safeSource}" data-glossary-id="${safeId}" data-glossary-source="${safeSource}" data-glossary-target="${safeTarget}" data-glossary-notes="${safeNotes}" title="Glossary: ${safeTarget}">${safeText}</mark>`;
    cursor = termStart + matchedText.length;
  }
  rendered += escapeHtml(source.slice(cursor));
  return rendered;
}

export function buildSegmentBlocks(segments, glossaryItems) {
  // Structural merge updates the visible parent source and hides children.
  // Children remain durable evidence but must not be rendered a second time.
  const visible = (segments || []).filter((segment) => !segment.merged_into_id);
  const blockMap = new Map();

  for (const segment of visible) {
    let fullSource = segment.original_text || "";
    const payload = {
      ...segment,
      full_source_text: fullSource,
      highlighted_source_text: highlightGlossaryTerms(fullSource, glossaryItems)
    };

    const key = `${segment.block_index}:${segment.html_tag || "segment"}`;
    const existing = blockMap.get(key) || {
      key,
      blockIndex: segment.block_index,
      label: segment.html_tag || "Segment",
      segments: []
    };
    existing.segments.push(payload);
    blockMap.set(key, existing);
  }

  return Array.from(blockMap.values()).sort((a, b) => {
    if (a.blockIndex === b.blockIndex) return a.key.localeCompare(b.key);
    return Number(a.blockIndex || 0) - Number(b.blockIndex || 0);
  });
}

export function formatCurrency(value, digits = 4) {
  const numeric = Number(value || 0);
  return Number.isFinite(numeric) ? numeric.toFixed(digits) : "0.0000";
}

export function formatInteger(value) {
  const numeric = Number(value || 0);
  return Number.isFinite(numeric) ? numeric.toLocaleString() : "0";
}

export function buildSplitParts(originalText, splitTail) {
  const source = String(originalText || "");
  const needle = String(splitTail || "");
  if (!needle) return null;
  const index = source.indexOf(needle);
  if (index < 0) return null;

  const splitIndex = index + needle.length;
  const part1 = source.slice(0, splitIndex).trim();
  const part2 = source.slice(splitIndex).trim();
  if (!part1 || !part2) return null;
  return { part1, part2 };
}

export function containsHtmlMarkup(value) {
  return /<\/?[A-Za-z][^>]*>/u.test(String(value || ""));
}
