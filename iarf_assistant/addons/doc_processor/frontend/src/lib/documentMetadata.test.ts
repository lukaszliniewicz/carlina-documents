import { describe, expect, it } from "vitest";
import { canonicalDocumentMetadata, metadataFormFromDocument, metadataPayloadFromForm } from "./documentMetadata";
import type { ProcessedDocument } from "./types";

const document: ProcessedDocument = {
  id: "document-1",
  filename: "source.pdf",
  status: "completed",
  created_at: "2026-08-26T00:00:00Z",
  settings: {
    document_metadata: {
      title: "Settings title",
      heading_structure: { validation: { status: "provisional" } },
      extraction_notes: ["Keep the front-matter order."]
    }
  },
  metadata: {
    title: "Canonical title",
    contributors: { authors: ["Ada Author"], editors: ["Ed Editor"] },
    subjects: ["History", "Archives"]
  }
};

describe("document metadata helpers", () => {
  it("keeps canonical record fields while retaining settings-only extraction evidence", () => {
    expect(canonicalDocumentMetadata(document)).toMatchObject({
      title: "Canonical title",
      heading_structure: { validation: { status: "provisional" } },
      extraction_notes: ["Keep the front-matter order."]
    });
  });

  it("maps contributor and subject lists to an explicit editable form and payload", () => {
    const form = metadataFormFromDocument(document);
    expect(form.authors).toBe("Ada Author");
    expect(form.editors).toBe("Ed Editor");
    expect(form.subjects).toBe("History\nArchives");

    const payload = metadataPayloadFromForm({
      ...form,
      title: "  Reviewed title  ",
      authors: "Doe, Jane; Beatrice Writer",
      editors: "",
      translators: "T. Translator\nU. Translator",
      subjects: "History, archives; Religious studies"
    });
    expect(payload).toMatchObject({
      title: "Reviewed title",
      author: "",
      contributors: {
        authors: ["Doe, Jane", "Beatrice Writer"],
        editors: [],
        translators: ["T. Translator", "U. Translator"]
      },
      subjects: ["History, archives", "Religious studies"]
    });
  });

  it("retains deliberate empty values so Save can clear a metadata field", () => {
    const payload = metadataPayloadFromForm(metadataFormFromDocument(document));
    expect(payload.subtitle).toBe("");
    expect(payload.contributors?.translators).toEqual([]);
  });
});
