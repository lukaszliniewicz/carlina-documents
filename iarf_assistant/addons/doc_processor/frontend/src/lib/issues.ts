import { requestJson } from "./api";

export type IssueStatus = "open" | "confirmed" | "resolved" | "dismissed";
export type IssueKind = "ambiguous_text" | "name_check" | "structure" | "diagram" | "source_damage" | "other";
export interface IssueReport {
  key: string; kind: IssueKind; excerpt: string; reason: string;
  suggested_readings: string[];
  references: { url: string; title: string; note: string }[];
}
export interface ExtractionIssue {
  id: string; document_id: string; page_number: number | null;
  source_revision: string; current_revision: string; context_stale: boolean;
  status: IssueStatus; version: number; origin: string; report: IssueReport;
  history: { status: IssueStatus; note: string; timestamp?: string; created_at?: string }[];
  created_at: string; updated_at: string;
}
export interface IssuePage {
  document_id: string; current_revision: string; items: ExtractionIssue[];
  total: number; counts: Record<IssueStatus, number>;
}
const base = (id: string) => `/addons/doc_processor/api/documents/${encodeURIComponent(id)}/issues`;
export function listIssues(id: string, status: string, offset: number, signal?: AbortSignal): Promise<IssuePage> {
  const query = new URLSearchParams({ offset: String(offset), limit: "25" });
  if (status) query.set("status", status);
  return requestJson(`${base(id)}?${query}`, { signal });
}
export function updateIssue(id: string, issue: ExtractionIssue, status: IssueStatus, note: string, key: string): Promise<ExtractionIssue> {
  return requestJson(`${base(id)}/${encodeURIComponent(issue.id)}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ expected_revision: issue.current_revision, expected_version: issue.version, idempotency_key: key, status, note }),
  });
}
export function reportIssue(id: string, revision: string, page: number | null, report: IssueReport, key: string): Promise<ExtractionIssue> {
  return requestJson(base(id), {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ expected_revision: revision, idempotency_key: key, page_number: page, report }),
  });
}
