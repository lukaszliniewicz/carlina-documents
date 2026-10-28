import type {
  CanonicalDocumentMetadata,
  EditableMetadataForm,
  MetadataContributors,
  ProcessedDocument
} from "./types";

const editableFields = [
  "title",
  "subtitle",
  "publication_type",
  "publisher",
  "publication_date",
  "language",
  "isbn",
  "issn",
  "journal_or_series",
  "volume",
  "issue",
  "rights",
  "source",
  "description"
] as const;

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === "string") : [];
}

function contributorList(contributors: MetadataContributors | undefined, role: keyof MetadataContributors): string {
  return stringList(contributors?.[role]).join("\n");
}

/** Merge the durable metadata evidence, favouring the canonical record fields. */
export function canonicalDocumentMetadata(document: ProcessedDocument | null | undefined): CanonicalDocumentMetadata {
  const settingsMetadata = document?.settings?.document_metadata || {};
  const documentMetadata = document?.document_metadata || {};
  const metadata = document?.metadata || {};
  return { ...settingsMetadata, ...documentMetadata, ...metadata };
}

export function metadataFormFromDocument(document: ProcessedDocument | null | undefined): EditableMetadataForm {
  const metadata = canonicalDocumentMetadata(document);
  const form = {
    title: text(metadata.title),
    subtitle: text(metadata.subtitle),
    publication_type: text(metadata.publication_type),
    authors: contributorList(metadata.contributors, "authors") || text(metadata.author),
    editors: contributorList(metadata.contributors, "editors"),
    translators: contributorList(metadata.contributors, "translators"),
    publisher: text(metadata.publisher),
    publication_date: text(metadata.publication_date),
    language: text(metadata.language),
    isbn: text(metadata.isbn),
    issn: text(metadata.issn),
    journal_or_series: text(metadata.journal_or_series),
    volume: text(metadata.volume),
    issue: text(metadata.issue),
    rights: text(metadata.rights),
    source: text(metadata.source),
    subjects: stringList(metadata.subjects).join("\n"),
    description: text(metadata.description)
  };
  return form;
}

function splitEntries(value: string): string[] {
  // Commas are valid inside personal names ("Doe, Jane") and subject labels.
  // A line break or semicolon is an explicit separator in the review form.
  return value.split(/[\n;]/).map((entry) => entry.trim()).filter(Boolean);
}

/** Preserve explicit empty values so a deliberate Save can clear a field. */
export function metadataPayloadFromForm(form: EditableMetadataForm): CanonicalDocumentMetadata {
  const metadata: CanonicalDocumentMetadata = {
    // Clear the legacy fallback too: otherwise an old `author` projection can
    // repopulate intentionally emptied contributor lists on the server.
    author: "",
    contributors: {
      authors: splitEntries(form.authors),
      editors: splitEntries(form.editors),
      translators: splitEntries(form.translators)
    },
    subjects: splitEntries(form.subjects)
  };
  for (const field of editableFields) {
    metadata[field] = form[field].trim();
  }
  return metadata;
}
