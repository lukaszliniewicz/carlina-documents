import type { ProcessingSettings } from "./types";

export const ACCEPTED_DOCUMENT_TYPES =
  ".pdf,.txt,.epub,.fb2,.docx,.doc,.html,.htm,.odt,.rtf,.csv,.msg";

export function defaultProcessingSettings(): ProcessingSettings {
  return {
    processing_profile: "book_quality",
    engine: "llm_vision",
    parsing_model: "",
    metadata_model: "",
    removehf: true,
    ignore_footnotes: false,
    simple_footnotes: false,
    prompt_cache: true,
    metadata_prepass: true,
    metadata_pages: 12,
    metadata_end_pages: 4,
    metadata_delete_flagged_pages: false,
    // Printed TOCs are metadata evidence. Canonical content omits them and
    // exporters generate navigation from validated heading anchors.
    toc_handling: "omit",
    append_previous: true,
    // Deterministic semantic validation is the release baseline. Evaluation
    // remains an explicit, optional second pass for defense in depth.
    evaluate_pdf: false,
    extract_images: true,
    correct_images: true,
    llm_boundary_merge: false,
    parallel_pages: 1,
    page_range: "",
    skip_cropping: false,
    marker_force_ocr: false,
    marker_paginate_output: false,
    pymupdf_use_ocr: true,
    pymupdf_force_ocr: false,
    pymupdf_paginate_output: false,
    pymupdf_exclude_footers: true,
    pymupdf_embed_images: false,
    paddle_batch_size: 4,
    paddle_use_doc_unwarping: false,
    paddle_use_doc_orientation_classify: false,
    paddle_remove_hf: true,
    paddle_preserve_footnotes: false,
    paddle_chart_rec: false,
    paddle_seal_rec: false,
    paddle_merge_tables: true,
    paddle_relevel_titles: true,
    paddle_concatenate_pages: false,
    paddle_layout_shape_mode: "auto",
    store_debug_artifacts: false
  };
}

/**
 * Quality-first PDF extraction settings.  They are expanded into the ordinary
 * request fields as well as being labelled with `processing_profile`; the
 * profile is consequently useful to both older and newer service versions.
 */
export function applyBookQualityProfile(settings: ProcessingSettings): ProcessingSettings {
  return normalizeProcessingSettings({
    ...settings,
    processing_profile: "book_quality",
    engine: "llm_vision",
    removehf: true,
    ignore_footnotes: false,
    simple_footnotes: false,
    prompt_cache: true,
    metadata_prepass: true,
    metadata_pages: 12,
    metadata_end_pages: 4,
    metadata_delete_flagged_pages: false,
    toc_handling: "omit",
    append_previous: true,
    // Preserve an explicit user choice when switching profiles. A missing
    // value from an older saved form defaults to the no-evaluation baseline.
    evaluate_pdf: settings.evaluate_pdf ?? false,
    extract_images: true,
    correct_images: true,
    llm_boundary_merge: settings.llm_boundary_merge ?? false,
    parallel_pages: 1,
    skip_cropping: false
  });
}

export function normalizeProcessingSettings(settings: ProcessingSettings): ProcessingSettings {
  const next = { ...settings };
  next.processing_profile = next.processing_profile === "book_quality" ? "book_quality" : "custom";
  next.evaluate_pdf = next.evaluate_pdf ?? false;
  next.llm_boundary_merge = next.llm_boundary_merge ?? false;
  next.parallel_pages = Math.max(1, Math.min(10, Number(next.parallel_pages) || 1));
  next.metadata_pages = Math.max(1, Math.min(100, Number(next.metadata_pages) || 20));
  next.metadata_end_pages = Math.max(0, Math.min(100, Number(next.metadata_end_pages) || 0));
  next.paddle_batch_size = Math.max(1, Math.min(50, Number(next.paddle_batch_size) || 4));
  next.toc_handling = "omit";

  if (next.parallel_pages > 1) {
    next.append_previous = false;
  }

  if (!next.metadata_prepass || !next.skip_cropping) {
    next.metadata_delete_flagged_pages = false;
  }

  if (next.ignore_footnotes) {
    next.simple_footnotes = false;
  }

  return next;
}

export function appendProcessingSettings(formData: FormData, settings: ProcessingSettings): void {
  const normalized = normalizeProcessingSettings(settings);

  formData.append("processing_profile", normalized.processing_profile);
  formData.append("engine", normalized.engine);
  formData.append("parsing_model", normalized.parsing_model || "");
  formData.append("metadata_model", normalized.metadata_model || "");
  formData.append("removehf", String(normalized.removehf));
  formData.append("ignore_footnotes", String(normalized.ignore_footnotes));
  formData.append("simple_footnotes", String(normalized.simple_footnotes));
  formData.append("prompt_cache", String(normalized.prompt_cache));
  formData.append("metadata_prepass", String(normalized.metadata_prepass));
  formData.append("metadata_pages", String(normalized.metadata_pages));
  formData.append("metadata_end_pages", String(normalized.metadata_end_pages));
  formData.append("metadata_delete_flagged_pages", String(normalized.metadata_delete_flagged_pages));
  formData.append("toc_handling", normalized.toc_handling);
  formData.append("append_previous", String(normalized.append_previous));
  formData.append("evaluate_pdf", String(normalized.evaluate_pdf));
  formData.append("extract_images", String(normalized.extract_images));
  formData.append("correct_images", String(normalized.correct_images));
  formData.append("llm_boundary_merge", String(normalized.llm_boundary_merge));
  formData.append("parallel_pages", String(normalized.parallel_pages));
  formData.append("page_range", normalized.page_range || "");
  formData.append("skip_cropping", String(normalized.skip_cropping));
  formData.append("store_debug_artifacts", String(normalized.store_debug_artifacts));

  if (normalized.engine === "marker") {
    formData.append("force_ocr", String(normalized.marker_force_ocr));
    formData.append("paginate_output", String(normalized.marker_paginate_output));
  } else if (normalized.engine === "paddleocr") {
    formData.append("paddle_batch_size", String(normalized.paddle_batch_size));
    formData.append("use_doc_unwarping", String(normalized.paddle_use_doc_unwarping));
    formData.append("use_doc_orientation_classify", String(normalized.paddle_use_doc_orientation_classify));
    formData.append("paddle_remove_hf", String(normalized.paddle_remove_hf));
    formData.append("paddle_preserve_footnotes", String(normalized.paddle_preserve_footnotes));
    formData.append("paddle_chart_rec", String(normalized.paddle_chart_rec));
    formData.append("paddle_seal_rec", String(normalized.paddle_seal_rec));
    formData.append("merge_tables", String(normalized.paddle_merge_tables));
    formData.append("relevel_titles", String(normalized.paddle_relevel_titles));
    formData.append("concatenate_pages", String(normalized.paddle_concatenate_pages));
    formData.append("layout_shape_mode", normalized.paddle_layout_shape_mode || "auto");
  } else if (normalized.engine === "pymupdf") {
    formData.append("use_ocr", String(normalized.pymupdf_use_ocr));
    formData.append("force_ocr", String(normalized.pymupdf_force_ocr));
    formData.append("paginate_output", String(normalized.pymupdf_paginate_output));
    formData.append("exclude_footers", String(normalized.pymupdf_exclude_footers));
    formData.append("embed_images", String(normalized.pymupdf_embed_images));
  }
}
