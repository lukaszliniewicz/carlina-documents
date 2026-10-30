import { afterEach, describe, expect, it, vi } from "vitest";
import { processDocument, reextractDocumentMetadata, saveCompletedImageCrops, updateDocumentMetadata, updateDocumentContent } from "./api";

describe("processDocument", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("retries with the document's durable backend settings rather than upload-form models", async () => {
    const fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ success: true, job_id: "job-1" }), {
        headers: { "Content-Type": "application/json" }
      })
    );
    vi.stubGlobal("fetch", fetch);

    await expect(processDocument("document-1")).resolves.toEqual({ success: true, job_id: "job-1" });
    expect(fetch).toHaveBeenCalledWith("/addons/doc_processor/api/documents/document-1/process", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    });
  });
});

describe("document metadata API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("saves only the explicit canonical metadata payload", async () => {
    const fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: "document-1" }), { headers: { "Content-Type": "application/json" } })
    );
    vi.stubGlobal("fetch", fetch);

    await updateDocumentMetadata(
      "document-1",
      { title: "Reviewed title", contributors: { authors: ["A. Author"] } },
      "2026-08-26T10:00:00"
    );
    expect(fetch).toHaveBeenCalledWith("/addons/doc_processor/api/documents/document-1/metadata", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        metadata: { title: "Reviewed title", contributors: { authors: ["A. Author"] } },
        expected_updated_at: "2026-08-26T10:00:00"
      })
    });
  });

  it("re-extracts with the document's stored metadata model", async () => {
    const fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: "document-1" }), { headers: { "Content-Type": "application/json" } })
    );
    vi.stubGlobal("fetch", fetch);

    await reextractDocumentMetadata("document-1", "2026-08-26T10:00:00");
    expect(fetch).toHaveBeenCalledWith("/addons/doc_processor/api/documents/document-1/metadata/reextract", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_updated_at: "2026-08-26T10:00:00" })
    });
  });
});

it("sends a revision and crop coordinates without page images, URLs or captions", async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ success: true })));
  vi.stubGlobal("fetch", fetch);
  try {
    await saveCompletedImageCrops("document-1", [{
      page_index: 0, image_url: "/uploads/owned-page.png", label: "Image 1", source_kind: "saved_image", source_page_index: 2,
      images: [{ img_idx: 0, bbox: [10, 20, 800, 900], caption: "Existing caption", keep: true }]
    }], "revision-1");
    expect(fetch).toHaveBeenCalledWith("/addons/doc_processor/api/documents/document-1/image-crops", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_updated_at: "revision-1", pages: [{ page_index: 0, source_page_index: 2, images: [{ img_idx: 0, bbox: [10, 20, 800, 900] }] }] })
    });
  } finally { vi.unstubAllGlobals(); }
});

it("sends the extraction revision and retains conflict details", async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: "Changed elsewhere", code: "revision_conflict" }), { status:409, headers:{ "Content-Type":"application/json" } }));
  vi.stubGlobal("fetch",fetch);
  try {
    await expect(updateDocumentContent("document-1","<p>Correction</p>","revision-1")).rejects.toMatchObject({ status:409, payload:{ code:"revision_conflict" } });
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ output_html:"<p>Correction</p>",expected_updated_at:"revision-1" });
  } finally { vi.unstubAllGlobals(); }
});
