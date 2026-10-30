import type { DocumentProject, ModelOption, ProcessedDocument } from "./types";

export function needsDocumentAttention(status: string): boolean {
  return ["error", "needs_cropping", "reviewing_images", "paused", "suspended", "completed_with_errors"].includes(status);
}

export function normalizeModels(rawModels: unknown): ModelOption[] {
  if (!Array.isArray(rawModels)) {
    return [];
  }

  return rawModels
    .map((model) => {
      const item = model as Record<string, unknown>;
      return {
        id: String(item.id || item.model_id || item.name || ""),
        name: String(item.name || item.model_id || item.id || ""),
        provider: item.provider ? String(item.provider) : undefined,
        is_preset_fast: Boolean(item.is_preset_fast),
        is_preset_medium: Boolean(item.is_preset_medium),
        is_preset_powerful: Boolean(item.is_preset_powerful),
        supports_reasoning: Boolean(item.supports_reasoning),
        supports_vision: Boolean(item.supports_vision),
        supports_pdf: Boolean(item.supports_pdf),
        supported_reasoning_levels: Array.isArray(item.supported_reasoning_levels)
          ? item.supported_reasoning_levels.map(String)
          : String(item.supported_reasoning_levels || "").split(",").map((value) => value.trim()).filter(Boolean)
      };
    })
    .filter((model) => model.id && model.name);
}

export function selectDefaultModels(models: ModelOption[]): { parsingModel: string; metadataModel: string } {
  const visionModels = models.filter((model) => model.supports_vision);
  const usableModels = visionModels.length ? visionModels : models;
  const presetMedium = usableModels.find((model) => model.is_preset_medium);
  const parsing = presetMedium || usableModels.find((model) => model.id.toLowerCase().includes("pro")) || usableModels[0];
  const metadata =
    presetMedium ||
    usableModels.find((model) => model.id.toLowerCase().includes("flash")) ||
    usableModels.find((model) => model.id.toLowerCase().includes("pro")) ||
    usableModels[0];

  return {
    parsingModel: parsing?.id || "",
    metadataModel: metadata?.id || ""
  };
}

export function formatStatusLabel(status: string | undefined | null): string {
  const raw = String(status || "unknown");
  return raw
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function formatDate(value: string | undefined | null): string {
  if (!value) {
    return "Unknown";
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "Unknown" : parsed.toLocaleDateString();
}

export function formatDateTime(value: string | undefined | null): string {
  if (!value) {
    return "Unknown";
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "Unknown" : parsed.toLocaleString();
}

export function formatDuration(seconds: unknown): string | null {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value <= 0) {
    return null;
  }

  const rounded = Math.round(value);
  if (rounded < 60) {
    return `${rounded}s`;
  }

  const hours = Math.floor(rounded / 3600);
  const minutes = Math.floor((rounded % 3600) / 60);
  const secs = rounded % 60;
  const pad = (number: number) => String(number).padStart(2, "0");
  return hours > 0 ? `${pad(hours)}h ${pad(minutes)}m ${pad(secs)}s` : `${pad(minutes)}m ${pad(secs)}s`;
}

export function formatModelsUsed(settings: ProcessedDocument["settings"]): string {
  const parsingModel = settings?.parsing_model ? String(settings.parsing_model) : "";
  const metadataModel = settings?.metadata_model ? String(settings.metadata_model) : "";
  const models = [parsingModel, metadataModel].filter(Boolean);
  return Array.from(new Set(models)).join(", ");
}

export function isActiveDocumentStatus(status: string | undefined | null): boolean {
  return ["preparing_crop_review", "processing", "pausing"].includes(String(status || ""));
}

export function isActiveProjectStatus(status: string | undefined | null): boolean {
  return ["queued", "running", "pausing", "paused", "waiting_for_review", "stopping"].includes(String(status || ""));
}

export function projectHasCompletedDocument(project: DocumentProject | null | undefined): boolean {
  return Boolean(
    project?.documents.some((entry) =>
      ["completed", "completed_with_errors"].includes(entry.document?.status || "")
    )
  );
}

export function statusTone(status: string | undefined | null): string {
  const value = String(status || "");
  if (["completed", "completed_with_errors"].includes(value)) {
    return "success";
  }
  if (["processing", "running", "queued", "preparing_crop_review"].includes(value)) {
    return "info";
  }
  if (["needs_cropping", "reviewing_images", "waiting_for_review", "paused", "pausing", "suspended"].includes(value)) {
    return "warning";
  }
  if (["error", "missing_source", "failed"].includes(value)) {
    return "danger";
  }
  if (["cancelled", "stopping"].includes(value)) {
    return "muted";
  }
  return "neutral";
}

export function clamp(number: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, number));
}

export function debounce<T extends (...args: never[]) => void>(callback: T, delayMs: number): T & { cancel: () => void } {
  let timer: ReturnType<typeof setTimeout> | null = null;
  const debounced = ((...args: Parameters<T>) => {
    if (timer) {
      clearTimeout(timer);
    }
    timer = setTimeout(() => {
      timer = null;
      callback(...args);
    }, delayMs);
  }) as T & { cancel: () => void };

  debounced.cancel = () => {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
  };

  return debounced;
}

export function stopEvent(event: Event): void {
  event.stopPropagation();
}
