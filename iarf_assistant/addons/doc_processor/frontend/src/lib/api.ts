import type { FrontendApiClient } from "@carlina/sdk";

import { appendProcessingSettings } from "./options";
import type {
  CanonicalDocumentMetadata,
  CompletedImageCrops,
  CropPagesPayload,
  CropPreviewPayload,
  DocumentUploadResponse,
  DocumentProject,
  EngineOption,
  ImageReviewPage,
  JobStatus,
  ModelOption,
  ProcessedDocument,
  ProcessingSettings,
  PromptSettings,
  PromptPreviewResponse,
  PromptEditorSection,
  ProjectRun,
  ReasoningSlots
} from "./types";
import { normalizeModels } from "./utils";

const ADDON_API_BASE = "/addons/doc_processor/api";
let scopedApi: FrontendApiClient | null = null;
let mountSignal: AbortSignal | null = null;

export function configureDocProcessorApi(
  api: FrontendApiClient | null,
  signal: AbortSignal | null = null
): () => void {
  const previousApi = scopedApi;
  const previousSignal = mountSignal;
  scopedApi = api;
  mountSignal = signal;
  return () => {
    if (scopedApi === api) scopedApi = previousApi;
    if (mountSignal === signal) mountSignal = previousSignal;
  };
}

function requestInit(
  init: RequestInit = {}
): RequestInit & { signal?: AbortSignal } {
  const { signal, ...rest } = init;
  if (signal) return { ...rest, signal };
  return mountSignal ? { ...rest, signal: mountSignal } : rest;
}

function scopedPath(url: string): string | null {
  const parsed = new URL(url, window.location.origin);
  if (
    parsed.origin !== window.location.origin ||
    !parsed.pathname.startsWith(`${ADDON_API_BASE}/`)
  ) {
    return null;
  }
  return `${parsed.pathname.slice("/addons/doc_processor".length)}${parsed.search}`;
}

async function parseResponse<T>(response: Response): Promise<T> {
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.text();

  if (!response.ok) {
    const message =
      typeof payload === "object" && payload && "error" in payload
        ? String((payload as { error?: unknown }).error || "Request failed.")
        : String(payload || "Request failed.");
    throw new Error(message);
  }

  return payload as T;
}

export async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const normalizedInit = requestInit(init);
  const path = scopedPath(url);
  if (scopedApi && path) {
    return scopedApi.request<T>(path, normalizedInit);
  }
  const response = await fetch(url, normalizedInit);
  return parseResponse<T>(response);
}

export async function loadEngines(): Promise<EngineOption[]> {
  return requestJson<EngineOption[]>("/addons/doc_processor/api/engines");
}

export async function loadModels(): Promise<ModelOption[]> {
  try {
    const payload = await requestJson<unknown>("/api/llm/models?ready_only=true");
    return normalizeModels(payload);
  } catch (error) {
    console.warn("Failed to load managed LLM models.", error);
    return [];
  }
}

export async function loadPromptEditor(): Promise<{ sections: PromptEditorSection[] }> {
  return requestJson<{ sections: PromptEditorSection[] }>("/addons/doc_processor/api/prompts");
}

export async function loadReasoningSettings(): Promise<ReasoningSlots> {
  const payload = await requestJson<{ slots?: ReasoningSlots }>(
    "/api/llm/addon-settings/doc_processor/reasoning"
  );
  return payload.slots || {};
}

export async function saveReasoningSettings(
  slots: ReasoningSlots
): Promise<ReasoningSlots> {
  const payload = await requestJson<{ slots?: ReasoningSlots }>(
    "/api/llm/addon-settings/doc_processor/reasoning",
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slots })
    }
  );
  return payload.slots || slots;
}

export async function savePromptSettings(settings: PromptSettings): Promise<{ success: boolean; sections: PromptEditorSection[] }> {
  return requestJson<{ success: boolean; sections: PromptEditorSection[] }>("/addons/doc_processor/api/prompts", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompts: settings })
  });
}

export async function previewPromptSettings(
  prompts: PromptSettings,
  settings: ProcessingSettings,
  assumePreviousContext: boolean
): Promise<PromptPreviewResponse> {
  return requestJson<PromptPreviewResponse>("/addons/doc_processor/api/prompts/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      prompts,
      processing_settings: settings,
      assume_previous_context: assumePreviousContext
    })
  });
}

export async function loadDocuments(signal?: AbortSignal): Promise<ProcessedDocument[]> {
  return requestJson<ProcessedDocument[]>("/addons/doc_processor/api/documents", { signal });
}

export async function uploadDocument(file: File, settings: ProcessingSettings): Promise<DocumentUploadResponse> {
  const formData = new FormData();
  formData.append("file", file);
  appendProcessingSettings(formData, settings);
  return requestJson<DocumentUploadResponse>("/addons/doc_processor/api/documents", {
    method: "POST",
    body: formData
  });
}

export async function deleteDocument(documentId: string): Promise<void> {
  await requestJson(`/addons/doc_processor/api/documents/${documentId}`, { method: "DELETE" });
}

/** Resume/retry using the document's durable settings, never current form choices. */
export async function processDocument(documentId: string): Promise<{ success: boolean; job_id: string }> {
  return requestJson(`/addons/doc_processor/api/documents/${documentId}/process`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({})
  });
}

export async function controlDocument(documentId: string, action: "pause" | "stop"): Promise<void> {
  await requestJson(`/addons/doc_processor/api/documents/${documentId}/control`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action })
  });
}

export async function getDocument(documentId: string, signal?: AbortSignal): Promise<ProcessedDocument> {
  return requestJson<ProcessedDocument>(`/addons/doc_processor/api/documents/${documentId}`, { signal });
}

export function getSourceFileUrl(documentId: string, disposition: "inline" | "attachment" = "inline"): string {
  return `/addons/doc_processor/api/documents/${documentId}/source-file?disposition=${disposition}`;
}

export async function updateDocumentContent(documentId: string, outputHtml: string): Promise<ProcessedDocument> {
  return requestJson<ProcessedDocument>(`/addons/doc_processor/api/documents/${documentId}/content`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ output_html: outputHtml })
  });
}

export async function updateDocumentMetadata(
  documentId: string,
  metadata: CanonicalDocumentMetadata,
  expectedUpdatedAt?: string | null
): Promise<ProcessedDocument> {
  return requestJson<ProcessedDocument>(`/addons/doc_processor/api/documents/${documentId}/metadata`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      metadata,
      ...(expectedUpdatedAt ? { expected_updated_at: expectedUpdatedAt } : {})
    })
  });
}

/** Re-extract with the durable metadata model selected for this document. */
export async function reextractDocumentMetadata(
  documentId: string,
  expectedUpdatedAt?: string | null
): Promise<ProcessedDocument> {
  return requestJson<ProcessedDocument>(`/addons/doc_processor/api/documents/${documentId}/metadata/reextract`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(
      expectedUpdatedAt ? { expected_updated_at: expectedUpdatedAt } : {}
    )
  });
}

export async function loadCropPages(documentId: string, signal?: AbortSignal): Promise<CropPagesPayload> {
  return requestJson<CropPagesPayload>(`/addons/doc_processor/api/documents/${documentId}/pdf-pages`, { signal });
}

export async function loadCropPreview(
  documentId: string,
  cropMode: "single" | "left_right",
  firstPageSide: "left" | "right",
  excludedPages: number[],
  signal?: AbortSignal
): Promise<CropPreviewPayload> {
  const params = new URLSearchParams({
    crop_mode: cropMode,
    first_page_side: firstPageSide,
    excluded_pages: excludedPages.slice().sort((a, b) => a - b).join(",")
  });
  return requestJson<CropPreviewPayload>(`/addons/doc_processor/api/documents/${documentId}/pdf-preview?${params}`, { signal });
}

export async function saveCrop(
  documentId: string,
  payload: Record<string, unknown>
): Promise<{ success: boolean; job_id: string }> {
  return requestJson(`/addons/doc_processor/api/documents/${documentId}/set-crop`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
}

export async function loadImageReviewPages(documentId: string, signal?: AbortSignal): Promise<ImageReviewPage[]> {
  return requestJson<ImageReviewPage[]>(`/addons/doc_processor/api/documents/${documentId}/pdf-images`, { signal });
}

export async function saveImageReviewPages(
  documentId: string,
  pages: ImageReviewPage[]
): Promise<{ success: boolean; job_id: string }> {
  return requestJson(`/addons/doc_processor/api/documents/${documentId}/pdf-images`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(pages)
  });
}

export async function loadCompletedImageCrops(documentId: string, signal?: AbortSignal): Promise<CompletedImageCrops> {
  return requestJson(`/addons/doc_processor/api/documents/${documentId}/image-crops`, { signal });
}

export async function saveCompletedImageCrops(
  documentId: string,
  pages: ImageReviewPage[],
  expectedUpdatedAt: string
): Promise<{ success: boolean }> {
  return requestJson(`/addons/doc_processor/api/documents/${documentId}/image-crops`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      expected_updated_at: expectedUpdatedAt,
      pages: pages.map((page) => ({
        page_index: page.page_index,
        ...(page.source_kind === "saved_image" && page.source_page_index !== undefined
          ? { source_page_index: page.source_page_index } : {}),
        images: page.images.map((image) => ({ img_idx: image.img_idx, bbox: image.bbox }))
      }))
    })
  });
}

export async function waitForJob(jobId: string, onUpdate?: (status: JobStatus) => void): Promise<JobStatus> {
  for (let attempt = 0; attempt < 240; attempt += 1) {
    const status = await requestJson<JobStatus>(`/addons/doc_processor/api/jobs/${jobId}`);
    onUpdate?.(status);

    if (status.status === "completed") {
      return status;
    }
    if (status.status === "failed") {
      throw new Error(status.error || "Worker job failed.");
    }

    await new Promise((resolve) => setTimeout(resolve, 2000));
  }

  throw new Error("Timed out waiting for worker job.");
}

export async function loadProjects(signal?: AbortSignal): Promise<DocumentProject[]> {
  return requestJson<DocumentProject[]>("/addons/doc_processor/api/projects", { signal });
}

export async function createProject(name: string, documentConcurrency = 2): Promise<DocumentProject> {
  return requestJson<DocumentProject>("/addons/doc_processor/api/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, document_concurrency: documentConcurrency })
  });
}

export async function updateProject(projectId: string, payload: Record<string, unknown>): Promise<void> {
  await requestJson(`/addons/doc_processor/api/projects/${projectId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
}

export async function deleteProject(projectId: string): Promise<void> {
  await requestJson(`/addons/doc_processor/api/projects/${projectId}`, { method: "DELETE" });
}

export async function addProjectDocuments(projectId: string, documentIds: string[]): Promise<void> {
  await requestJson(`/addons/doc_processor/api/projects/${projectId}/documents`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ document_ids: documentIds })
  });
}

export async function removeProjectDocument(projectId: string, documentId: string): Promise<void> {
  await requestJson(`/addons/doc_processor/api/projects/${projectId}/documents/${documentId}`, {
    method: "DELETE"
  });
}

export async function reorderProjectDocuments(projectId: string, documentIds: string[]): Promise<void> {
  await requestJson(`/addons/doc_processor/api/projects/${projectId}/documents/order`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ document_ids: documentIds })
  });
}

export async function uploadProjectDocuments(
  projectId: string,
  files: FileList | File[],
  settings: ProcessingSettings
): Promise<DocumentProject> {
  const formData = new FormData();
  Array.from(files).forEach((file) => formData.append("files", file));
  appendProcessingSettings(formData, settings);
  const payload = await requestJson<{ project: DocumentProject }>(
    `/addons/doc_processor/api/projects/${projectId}/documents/upload`,
    {
      method: "POST",
      body: formData
    }
  );
  return payload.project;
}

export async function startProjectRun(projectId: string): Promise<ProjectRun> {
  return requestJson<ProjectRun>(`/addons/doc_processor/api/projects/${projectId}/runs`, {
    method: "POST"
  });
}

export async function controlProjectRun(runId: string, action: "pause" | "resume" | "stop"): Promise<ProjectRun> {
  return requestJson<ProjectRun>(`/addons/doc_processor/api/project-runs/${runId}/control`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action })
  });
}

export interface CleanupResult {
  revision: string;
  proposed_revision?: string;
  change_id?: string;
  changed?: boolean;
  undo_available?: boolean;
  cleanup?: {
    removed_count: number; skipped_count: number;
    examples: Array<{ role: string; page_numbers: number[]; text: string }>;
    examples_truncated: boolean;
  };
}

export type BoundaryAction = "join_with_space" | "dehyphenate" | "keep_hyphen";
export interface BoundaryCandidate {
  id: string; page_number: number; suggested_action: BoundaryAction;
  reason: string; left: string; right: string; preview: string;
}
export interface BoundaryRepairResult extends CleanupResult {
  boundary_repair?: {
    candidate_count: number; selected_count: number; offset: number; limit: number;
    has_more: boolean; candidates: BoundaryCandidate[];
    skipped_count: number; skipped_reasons: Record<string, number>; policy: string;
  };
}
export function repairDocumentBoundaries(documentId: string, options: {
  decisions?: Array<{ id: string; action: BoundaryAction }>;
  page_numbers?: number[]; offset?: number; limit?: number;
  expected_revision?: string; idempotency_key?: string; dry_run: boolean;
}): Promise<BoundaryRepairResult> {
  return requestJson(`${ADDON_API_BASE}/documents/${encodeURIComponent(documentId)}/repair-boundaries`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(options)
  });
}

export function cleanupDocument(documentId: string, options: {
  roles: string[]; page_numbers?: number[]; expected_revision?: string;
  idempotency_key?: string; dry_run: boolean;
}): Promise<CleanupResult> {
  return requestJson(`${ADDON_API_BASE}/documents/${encodeURIComponent(documentId)}/cleanup`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(options)
  });
}

export function undoDocumentCleanup(documentId: string, changeId: string, revision: string, key: string): Promise<CleanupResult> {
  return requestJson(`${ADDON_API_BASE}/documents/${encodeURIComponent(documentId)}/cleanup/undo`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ change_id: changeId, expected_revision: revision, idempotency_key: key })
  });
}
