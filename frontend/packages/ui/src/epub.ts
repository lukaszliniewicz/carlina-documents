export interface EpubOptions {
  profile?: "publishing-v1" | "publishing-v2";
  chapter_level?: number | null;
  note_placement?: "source" | "chapter" | "document";
  group_endnotes?: boolean;
  direction?: "source" | "ltr" | "rtl";
  roles?: Record<string, string>;
  page_labels?: Record<string, string>;
  table_headers?: Record<string, string>;
  image_descriptions?: Record<string, string>;
  decorative_images?: string[];
  accessibility_evaluation?: Record<string, unknown> | null;
  image_policy?: "balanced" | "preserve";
  photographic_images?: string[];
  long_descriptions?: Record<string, string>;
  front_matter_order?: string[];
  omitted_front_matter?: Record<string, string>;
  front_matter_edits?: Record<string, { sha256: string; text: string }>;
  front_matter_breaks?: Record<string, { sha256: string; role: string }>;
}

export interface EpubQuality {
  schema: "epub-quality-v1";
  profile: "publishing-v1" | "publishing-v2";
  input_sha256: string;
  options_sha256: string;
  artifact_sha256: string;
  content_sha256: string;
  checks: Record<"structure" | "epubcheck" | "ace" | "manual", string>;
  counts: Record<string, number>;
  issues: { code: string; severity: "warning" | "review"; target: string }[];
  issue_count: number;
  epubcheck_version: string | null;
  ace_version?: string | null;
  image_bytes?: { source: number; packaged: number };
}

export type EpubImageMode = "auto" | "jpeg" | "webp";

export interface EpubRecipe {
  epub_options: EpubOptions;
  epub_cover: { mode?: string; page?: number; crop?: number[] };
  epub_image_mode: EpubImageMode;
}

export interface EpubEdition {
  version: number;
  recipe: EpubRecipe | null;
  effective_recipe: EpubRecipe | null;
  source_revision: string | null;
  current_source_revision: string | null;
  stale: boolean;
}

export interface EpubPreflight {
  review_hash: string;
  options: EpubOptions;
  suggested_chapter_level: number | null;
  revision?: string;
  edition_revision?: string;
  numbering_revision?: number;
  outline: {
    input_sha256: string;
    headings: { id: string; level: number; title: string; role: string | null }[];
    tables: { id: string; caption: string; has_headers: boolean }[];
    images: { id: string; alt: string; decorative: boolean; is_cover?: boolean; is_selected_cover?: boolean; description_sha256?: string | null; description_id?: string | null }[];
    pages: { id: string; label: string }[];
    front_matter?: { id: string; source_anchor?: string; role: string; title: string; sha256: string; blocks: { id: string; text: string; sha256: string; editable: boolean; truncated?: boolean; can_start_section?: boolean; duplicate_of: string | null }[] }[];
  };
  quality: EpubQuality;
}

/** Reject incomplete responses before they replace a usable publication review. */
export function requireEpubPreflight(value: unknown): EpubPreflight {
  const object = (item: unknown): item is Record<string, unknown> => Boolean(item) && typeof item === "object" && !Array.isArray(item);
  const hash = (item: unknown) => typeof item === "string" && /^[a-f0-9]{64}$/.test(item);
  const rows = (item: unknown, limit: number, check: (row: Record<string, unknown>) => boolean) =>
    Array.isArray(item) && item.length <= limit && item.every((row) => object(row) && typeof row.id === "string" && check(row));
  const invalid = () => { throw new Error("The EPUB review response is incomplete. Review again before exporting."); };
  if (!object(value) || !hash(value.review_hash) || !object(value.options) || !object(value.outline) || !object(value.quality)) return invalid();
  const outline = value.outline;
  const quality = value.quality;
  if (!hash(outline.input_sha256) || !hash(quality.input_sha256) || !hash(quality.options_sha256) || !hash(quality.artifact_sha256) || !hash(quality.content_sha256)
      || quality.schema !== "epub-quality-v1" || !["publishing-v1", "publishing-v2"].includes(String(quality.profile))
      || !object(quality.checks) || !object(quality.counts) || !Array.isArray(quality.issues) || quality.issues.length > 200
      || !Number.isInteger(quality.issue_count) || Number(quality.issue_count) < quality.issues.length
      || !(quality.epubcheck_version === null || typeof quality.epubcheck_version === "string")
      || !(quality.ace_version === undefined || quality.ace_version === null || (typeof quality.ace_version === "string" && quality.ace_version.length <= 128))
      || (quality.image_bytes !== undefined && (!object(quality.image_bytes) || !["source", "packaged"].every((name) => Number.isInteger((quality.image_bytes as Record<string, unknown>)[name]) && Number((quality.image_bytes as Record<string, unknown>)[name]) >= 0 && Number((quality.image_bytes as Record<string, unknown>)[name]) <= 1e12)))) return invalid();
  if (!["structure", "epubcheck", "ace"].every((name) => ["passed", "failed", "not_run"].includes(String((quality.checks as Record<string, unknown>)[name])))
      || !["reviewed", "not_reviewed"].includes(String(quality.checks.manual))
      || !quality.issues.every((issue) => object(issue) && typeof issue.code === "string" && ["warning", "review"].includes(String(issue.severity)) && typeof issue.target === "string")
      || !Object.values(quality.counts).every((count) => Number.isInteger(count) && Number(count) >= 0)) return invalid();
  if (!rows(outline.headings, 512, (row) => Number.isInteger(row.level) && Number(row.level) >= 1 && Number(row.level) <= 6 && typeof row.title === "string" && (row.role === null || typeof row.role === "string"))
      || !rows(outline.tables, 512, (row) => typeof row.caption === "string" && typeof row.has_headers === "boolean")
      || !rows(outline.images, 512, (row) => typeof row.alt === "string" && typeof row.decorative === "boolean" && (row.is_cover === undefined || typeof row.is_cover === "boolean") && (row.is_selected_cover === undefined || typeof row.is_selected_cover === "boolean") && (row.description_sha256 === undefined || row.description_sha256 === null || hash(row.description_sha256)) && (row.description_id === undefined || row.description_id === null || typeof row.description_id === "string"))
      || !rows(outline.pages, 2048, (row) => typeof row.label === "string")
      || !(value.suggested_chapter_level === null || (Number.isInteger(value.suggested_chapter_level) && Number(value.suggested_chapter_level) >= 1 && Number(value.suggested_chapter_level) <= 6))
      || (value.revision !== undefined && !hash(value.revision))
      || (value.edition_revision !== undefined && !hash(value.edition_revision))) return invalid();
  if (outline.front_matter !== undefined && !rows(outline.front_matter, 64, (row) => typeof row.role === "string" && typeof row.title === "string" && hash(row.sha256) && (row.source_anchor === undefined || typeof row.source_anchor === "string")
      && rows(row.blocks, 512, (block) => typeof block.text === "string" && hash(block.sha256) && typeof block.editable === "boolean" && (block.truncated === undefined || typeof block.truncated === "boolean") && (block.can_start_section === undefined || typeof block.can_start_section === "boolean") && (block.duplicate_of === null || typeof block.duplicate_of === "string")))) return invalid();
  return value as unknown as EpubPreflight;
}

/** Retire only cover-specific decisions when the selected picture changes. */
export function resetEpubCoverReview(options: EpubOptions, preflight: EpubPreflight | null): EpubOptions {
  const covers = new Set((preflight?.outline.images || []).filter((image) => image.is_cover).map((image) => image.id));
  return {
    ...options,
    image_descriptions: Object.fromEntries(Object.entries(options.image_descriptions || {}).filter(([id]) => !covers.has(id))),
    decorative_images: (options.decorative_images || []).filter((id) => !covers.has(id)),
    ...(options.profile !== "publishing-v1" ? {
      long_descriptions: Object.fromEntries(Object.entries(options.long_descriptions || {}).filter(([id]) => !covers.has(id))),
      photographic_images: (options.photographic_images || []).filter((id) => !covers.has(id)),
    } : {}),
    accessibility_evaluation: null,
  };
}

export function requireEpubEdition(value: unknown): EpubEdition {
  const object = (item: unknown): item is Record<string, unknown> => Boolean(item) && typeof item === "object" && !Array.isArray(item);
  const hash = (item: unknown) => typeof item === "string" && /^[a-f0-9]{64}$/.test(item);
  const recipe = (item: unknown) => item === null || (object(item) && object(item.epub_options) && object(item.epub_cover) && typeof item.epub_image_mode === "string" && ["auto", "jpeg", "webp"].includes(item.epub_image_mode));
  if (!object(value) || !Number.isInteger(value.version) || Number(value.version) < 0 || !(value.current_source_revision === null || hash(value.current_source_revision))
      || !(value.source_revision === null || hash(value.source_revision)) || typeof value.stale !== "boolean" || !recipe(value.recipe) || !recipe(value.effective_recipe)) {
    throw new Error("Saved edition choices are incomplete. Refresh and load them again.");
  }
  return value as unknown as EpubEdition;
}

/** Add the same temporary review anchors as the publishing projection. */
export function withEpubReviewAnchors(html: string): string {
  if (typeof DOMParser === "undefined") return html;
  const document = new DOMParser().parseFromString(html, "text/html");
  const used = new Set([...document.querySelectorAll("[id]")].map((tag) => tag.id));
  for (const [kind, selector] of [["heading", "h1,h2,h3,h4,h5,h6"], ["table", "table"], ["image", "img"]]) {
    let ordinal = 0;
    for (const tag of document.querySelectorAll(selector)) {
      if (tag.id) continue;
      const cover = kind === "image" && (tag.classList.contains("cover") || (tag.getAttribute("epub:type") || "").split(/\s+/).includes("cover"));
      const base = cover ? "epub-cover-image" : `epub-${kind}-${++ordinal}`;
      let candidate = base;
      let suffix = 2;
      while (used.has(candidate)) candidate = `${base}-${suffix++}`;
      tag.id = candidate;
      used.add(candidate);
    }
  }
  return /<html(?:\s|>)/i.test(html) ? document.documentElement.outerHTML : document.body.innerHTML;
}
