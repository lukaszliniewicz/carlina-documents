export type ModelOption = {
  id: string;
  name: string;
  provider?: string;
  is_preset_fast?: boolean;
  is_preset_medium?: boolean;
  is_preset_powerful?: boolean;
  supports_reasoning?: boolean;
  supports_vision?: boolean;
  supports_pdf?: boolean;
  supported_reasoning_levels?: string[];
};

export type EngineOption = {
  id: string;
  name: string;
};

export type DocumentMetrics = {
  duration_seconds?: number;
  cost?: number;
  failed_pages?: unknown[];
  prompt_tokens?: number;
  completion_tokens?: number;
  cached_prompt_tokens?: number;
  boundary_llm_reviews?: number;
};

export type PdfOptions = {
  removehf?: boolean;
  ignore_footnotes?: boolean;
  simple_footnotes?: boolean;
  prompt_cache?: boolean;
  metadata_prepass?: boolean;
  metadata_pages?: number;
  metadata_end_pages?: number;
  metadata_delete_flagged_pages?: boolean;
  toc_handling?: "omit" | "preserve";
  append_previous?: boolean;
  evaluate?: boolean;
  extract_images?: boolean;
  correct_images?: boolean;
  llm_boundary_merge?: boolean;
  parallel_pages?: number;
  page_range?: string;
  skip_cropping?: boolean;
  excluded_pages?: number[];
  metadata_suggested_excluded_pages?: number[];
  metadata_review_applied?: boolean;
};

export type MetadataContributors = {
  authors?: string[];
  editors?: string[];
  translators?: string[];
  other?: Array<{ role: string; name: string }>;
};

export type MetadataTocEntry = {
  title: string;
  level?: number;
  page?: string;
  page_label?: string;
  pdf_page?: number;
};

export type MetadataPageRemovalRecommendation = {
  page: number;
  reason?: string;
  safe_to_remove?: boolean;
  confidence?: number;
};

export type HeadingStructureRole = {
  role: string;
  label?: string;
  depth?: number;
  html_level?: number;
  confidence?: number;
  extractor_ready?: boolean;
};

export type HeadingStructureEvidence = {
  validation?: {
    status?: string;
    toc_only_roles?: string[];
    unverified_roles?: string[];
    invalid_relationships?: string[];
  };
  roles?: HeadingStructureRole[];
  running_matter?: Array<{
    kind?: string;
    position?: string;
    examples?: string[];
    pages?: number[];
    observation?: string;
    confidence?: number;
  }>;
  document_specific_notes?: Array<{
    kind?: string;
    observation?: string;
    applies_to?: string;
    examples?: string[];
    confidence?: number;
  }>;
  uncertainties?: string[];
};

export type CanonicalDocumentMetadata = {
  cover_candidate?: { page: number; kind: "cover" | "title_page"; evidence: string };
  title?: string;
  /** Legacy display projection; editable contributors are canonical. */
  author?: string;
  subtitle?: string;
  publication_type?: string;
  contributors?: MetadataContributors;
  publisher?: string;
  publication_date?: string;
  language?: string;
  isbn?: string;
  issn?: string;
  journal_or_series?: string;
  volume?: string;
  issue?: string;
  rights?: string;
  source?: string;
  subjects?: string[];
  description?: string;
  table_of_contents?: MetadataTocEntry[];
  pages_to_remove?: MetadataPageRemovalRecommendation[];
  extraction_notes?: string[];
  heading_structure?: HeadingStructureEvidence;
};

export type EditableMetadataForm = {
  title: string;
  subtitle: string;
  publication_type: string;
  authors: string;
  editors: string;
  translators: string;
  publisher: string;
  publication_date: string;
  language: string;
  isbn: string;
  issn: string;
  journal_or_series: string;
  volume: string;
  issue: string;
  rights: string;
  source: string;
  subjects: string;
  description: string;
};

export type DocumentSettings = {
  mcp_recovery?: { reason?: { error_code?: string } };
  engine_id?: string;
  parsing_model?: string;
  metadata_model?: string;
  pdf_options?: PdfOptions;
  engine_options?: Record<string, unknown>;
  document_metadata?: CanonicalDocumentMetadata;
  metadata_stale?: boolean;
  pdf_state?: Record<string, unknown>;
  active_job_id?: string;
  active_project_run_document_id?: string;
  progress?: number;
  progress_msg?: string;
  metrics?: DocumentMetrics;
};

export type ProcessedDocument = {
  id: string;
  filename: string;
  status: string;
  settings?: DocumentSettings | null;
  error_message?: string | null;
  source_available?: boolean;
  created_at: string;
  updated_at?: string | null;
  output_html?: string | null;
  preview_html?: string | null;
  preview_warning?: string | null;
  output_markdown?: string | null;
  metadata?: CanonicalDocumentMetadata;
  document_metadata?: CanonicalDocumentMetadata;
};

export type DocumentUploadResponse = ProcessedDocument & {
  job_id?: string;
};

export type ProjectRunDocument = {
  id: string;
  document_id: string;
  position: number;
  status: string;
  job_id?: string | null;
  error_message?: string | null;
  document?: ProcessedDocument | null;
};

export type ProjectRun = {
  id: string;
  project_id: string;
  status: string;
  concurrency_limit: number;
  pause_requested: boolean;
  stop_requested: boolean;
  counts?: Record<string, number>;
  progress?: number;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  documents?: ProjectRunDocument[];
};

export type ProjectDocument = {
  id: string;
  document_id: string;
  position: number;
  settings_overrides?: Record<string, unknown>;
  document?: ProcessedDocument | null;
};

export type DocumentProject = {
  id: string;
  name: string;
  settings: {
    document_concurrency?: number;
    [key: string]: unknown;
  };
  document_count: number;
  created_at: string;
  updated_at: string;
  documents: ProjectDocument[];
  latest_run?: ProjectRun | null;
};

export type ProcessingSettings = {
  /** A named UI preset. The individual settings remain explicit in every request. */
  processing_profile: "book_quality" | "custom";
  engine: string;
  parsing_model: string;
  metadata_model: string;
  removehf: boolean;
  ignore_footnotes: boolean;
  simple_footnotes: boolean;
  prompt_cache: boolean;
  metadata_prepass: boolean;
  metadata_pages: number;
  metadata_end_pages: number;
  metadata_delete_flagged_pages: boolean;
  toc_handling: "omit" | "preserve";
  append_previous: boolean;
  evaluate_pdf: boolean;
  extract_images: boolean;
  correct_images: boolean;
  llm_boundary_merge: boolean;
  parallel_pages: number;
  page_range: string;
  skip_cropping: boolean;
  marker_force_ocr: boolean;
  marker_paginate_output: boolean;
  pymupdf_use_ocr: boolean;
  pymupdf_force_ocr: boolean;
  pymupdf_paginate_output: boolean;
  pymupdf_exclude_footers: boolean;
  pymupdf_embed_images: boolean;
  paddle_batch_size: number;
  paddle_use_doc_unwarping: boolean;
  paddle_use_doc_orientation_classify: boolean;
  paddle_remove_hf: boolean;
  paddle_preserve_footnotes: boolean;
  paddle_chart_rec: boolean;
  paddle_seal_rec: boolean;
  paddle_merge_tables: boolean;
  paddle_relevel_titles: boolean;
  paddle_concatenate_pages: boolean;
  paddle_layout_shape_mode: string;
  store_debug_artifacts?: boolean;
};

export type PromptSettings = Record<string, string>;

export type CropBox = [number, number, number, number];

export type CropPage = {
  page_index: number;
  page_number: number;
  in_page_range?: boolean;
  excluded?: boolean;
  metadata_suggested_exclusion?: boolean;
  side?: "left" | "right" | null;
  thumbnail_url: string;
};

export type CropSettings = {
  crop_box?: CropBox | null;
  crop_boxes?: {
    mode?: "single" | "left_right";
    left?: CropBox | null;
    right?: CropBox | null;
    first_page_side?: "left" | "right";
  };
  excluded_pages?: number[];
  metadata_suggested_excluded_pages?: number[];
  metadata_suggestions_applied?: boolean;
  page_range?: string;
};

export type CropPagesPayload = {
  success: boolean;
  total_pages: number;
  pages: CropPage[];
  crop_settings: CropSettings;
};

export type CropPreviewData = {
  image_data?: string;
  image_url?: string;
  width?: number;
  height?: number;
  used_pages?: number[];
  used_page_count?: number;
  used_page_total?: number;
  excluded_page_count?: number;
  is_density_stack?: boolean;
  suggested_crop_box?: CropBox | null;
  empty_reason?: "no_pages_for_stack" | "all_pages_excluded" | "stack_unavailable";
};

export type CropPreviewPayload = {
  success: boolean;
  crop_mode: "single" | "left_right";
  first_page_side: "left" | "right";
  total_pages: number;
  page_range_count: number;
  included_page_count: number;
  stacking_mode: string;
  previews: Partial<Record<"single" | "left" | "right", CropPreviewData | null>>;
};

export type ImageReviewItem = {
  img_idx: number;
  bbox: [number, number, number, number];
  caption?: string;
  keep?: boolean;
};

export type ImageReviewPage = {
  page_index: number;
  image_url: string;
  images: ImageReviewItem[];
  label?: string;
  source_kind?: "page" | "saved_image";
  source_page_index?: number;
};

export type CompletedImageCrops = {
  pages: ImageReviewPage[];
  expected_updated_at: string;
  page_count: number;
};

export type JobStatus = {
  status: string;
  message?: string;
  error?: string;
  result?: {
    kind?: string;
    download_url?: string;
    [key: string]: unknown;
  };
};

export type Notice = {
  kind: "success" | "error" | "info";
  message: string;
};

export type ReasoningSlots = Record<string, { model_id: string; level: string }>;

export type PromptField = {
  id: string;
  name: string;
  description: string;
  default_text: string;
  resolved_text: string;
  override_text?: string;
};

export type PromptEditorSection = {
  title: string;
  description: string;
  fields: PromptField[];
};

export type PromptFragmentPreview = {
  name: string;
  source: string;
};

export type ExtractionPreview = {
  system_prompt: string;
  user_prompt: string;
  placeholder_context_used?: boolean;
  active_fragments: PromptFragmentPreview[];
};

export type TemplatePreview = {
  template: string;
};

export type InstructionPromptPreview = {
  system_prompt: string;
  base_system_prompt?: string;
  system_prompt_parts?: Array<{
    id: string;
    source: "fixed" | "editable";
    editable: boolean;
    text: string;
  }>;
  editable_prompt_ids?: string[];
  fixed_prompt_ids?: string[];
  instruction_prefix: string;
  prompt: string;
};

export type PromptPreviewResponse = {
  extraction: ExtractionPreview;
  metadata: InstructionPromptPreview;
  boundary: InstructionPromptPreview;
  evaluation: TemplatePreview;
  txt_structuring: TemplatePreview;
};
