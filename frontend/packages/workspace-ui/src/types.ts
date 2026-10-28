import type { AddonMountContext } from "@carlina/sdk";

export interface LightboxImage {
  src: string;
  alt?: string;
  title?: string;
  downloadUrl?: string;
}

export interface WorkspaceOption {
  allowedProviders?: string[];
  disabled?: boolean;
  disabledReason?: string | null;
  incompatibleReason?: string | null;
  label: string;
  value: string;
}

export type WorkspaceFieldType =
  | "checkbox"
  | "component"
  | "date"
  | "file"
  | "number"
  | "select"
  | "text"
  | "textarea"
  | "url";

export interface WorkspaceField {
  accept?: string;
  componentId?: string;
  componentInput?: Readonly<Record<string, unknown>>;
  defaultValue?: unknown;
  description?: string;
  id: string;
  label: string;
  max?: number;
  min?: number;
  multiple?: boolean;
  options?: WorkspaceOption[];
  placeholder?: string;
  required?: boolean;
  rows?: number;
  step?: number;
  type: WorkspaceFieldType;
}

export interface WorkspaceAction {
  endpoint: string;
  label: string;
  method?: "GET" | "POST";
  payload?: (values: Record<string, unknown>) => unknown;
}

export interface WorkspaceDefinition {
  description: string;
  encoding?: "json" | "multipart";
  endpoint: string;
  eyebrow?: string;
  fields: WorkspaceField[];
  method?: "GET" | "POST";
  preparePayload?: (
    values: Record<string, unknown>,
    files: Record<string, File[]>,
  ) => unknown;
  response?: "json" | "sse";
  resultTitle?: string;
  secondaryActions?: WorkspaceAction[];
  submitLabel: string;
  title: string;
}

export interface DynamicField {
  default?: boolean | number | string | string[];
  description?: string;
  hiddenWhen?: Record<string, string | string[]>;
  id: string;
  label: string;
  max?: number;
  min?: number;
  options?: Array<WorkspaceOption | string>;
  placeholder?: string;
  required?: boolean;
  rows?: number;
  type: string;
}

export interface DynamicNotice {
  message: string;
  visibleWhen?: Record<string, string | string[]>;
}

export interface DynamicAction {
  disabled?: boolean;
  disabledReason?: string;
  description?: string;
  fields: DynamicField[];
  group?: string;
  id: string;
  label: string;
  notices?: DynamicNotice[];
}

export interface DynamicSchema {
  actions: DynamicAction[];
  providers?: unknown[];
  settings?: Record<string, unknown>;
}

export interface WorkspaceProps {
  context: AddonMountContext;
}
