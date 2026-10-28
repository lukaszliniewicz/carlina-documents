import { fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";

import { EpubCoverCrop, EpubEditionControls, EpubReviewPanel, requireEpubEdition, requireEpubPreflight, resetEpubCoverReview, withEpubReviewAnchors, type EpubPreflight } from "@carlina/ui";

function review(): EpubPreflight {
  return {
    options: {}, revision: "a".repeat(64), review_hash: "c".repeat(64), suggested_chapter_level: 2,
    outline: {
      input_sha256: "a".repeat(64), headings: [{ id: "chapter", level: 2, title: "Chapter", role: "chapter" }],
      tables: [], images: [{ id: "art", alt: "A source description", decorative: false }], pages: [],
    },
    quality: {
      schema: "epub-quality-v1", profile: "publishing-v1", input_sha256: "a".repeat(64), options_sha256: "b".repeat(64),
      artifact_sha256: "0".repeat(64), content_sha256: "0".repeat(64), epubcheck_version: null,
      counts: { headings: 1 }, issue_count: 1, issues: [{ code: "image-description-review", severity: "review", target: "art" }],
      checks: { structure: "not_run", epubcheck: "not_run", ace: "not_run", manual: "not_reviewed" },
    },
  };
}

describe("EPUB publication review", () => {
  it("retires only cover decisions when a replacement is selected", () => {
    const preflight = review();
    preflight.outline.images.push({ id: "old-cover", alt: "Old cover", decorative: false, is_cover: true });
    const options = { chapter_level: 2, image_descriptions: { "old-cover": "Old painting", art: "Interior art" }, decorative_images: ["old-cover"], accessibility_evaluation: { evaluator: "Example" } };
    expect(resetEpubCoverReview(options, preflight)).toEqual({
      ...options, image_descriptions: { art: "Interior art" }, decorative_images: [], long_descriptions: {}, photographic_images: [], accessibility_evaluation: null,
    });
    expect(options.image_descriptions["old-cover"]).toBe("Old painting");
  });

  it("identifies the selected cover and does not open an unrelated source image", async () => {
    const preflight = review();
    preflight.outline.images[0] = { id: "art", alt: "Edition — Cover", decorative: false, is_cover: true, is_selected_cover: true };
    render(EpubReviewPanel, { props: { preflight, onreview: vi.fn(), oninspect: vi.fn() } });
    await fireEvent.click(screen.getByText("Warnings and review items (1)"));
    expect(screen.queryByRole("button", { name: "View affected section" })).toBeNull();
    await fireEvent.click(screen.getByText("Image descriptions (1)"));
    expect(screen.getByLabelText("Cover alt text")).toBeTruthy();
    expect(screen.getByText(/A replacement needs its own description/)).toBeTruthy();
  });
  it("keeps checks that have not run distinct from publication approval and inspects the issue", async () => {
    const oninspect = vi.fn();
    render(EpubReviewPanel, { props: { preflight: review(), onreview: vi.fn(), oninspect } });
    expect(screen.getAllByText("Not run")).toHaveLength(3);
    expect(screen.getByText("Needs human evaluation")).toBeTruthy();
    await fireEvent.click(screen.getByText("Warnings and review items (1)"));
    await fireEvent.click(screen.getByRole("button", { name: "View affected section" }));
    expect(oninspect).toHaveBeenCalledWith("art");
  });

  it("invalidates prior evaluation when choices change and can reset to a fresh source review", async () => {
    const onreview = vi.fn(async (options) => ({ ...review(), options }));
    render(EpubReviewPanel, { props: { options: { accessibility_evaluation: { evaluator: "Example" } }, preflight: review(), onreview } });
    await fireEvent.change(screen.getByLabelText("Note placement"), { target: { value: "chapter" } });
    expect(screen.getByText("Choices have changed. Review again before exporting.")).toBeTruthy();
    await fireEvent.click(screen.getByRole("button", { name: "Review changed choices" }));
    await waitFor(() => expect(onreview).toHaveBeenCalledWith({ note_placement: "chapter", accessibility_evaluation: null }));
    await fireEvent.click(screen.getByRole("button", { name: "Reset choices and review current edition" }));
    await waitFor(() => expect(onreview).toHaveBeenLastCalledWith({}));
  });

  it("can confirm and then undo a decorative-image choice", async () => {
    const onreview = vi.fn(async (options) => ({ ...review(), options }));
    render(EpubReviewPanel, { props: { preflight: review(), onreview } });
    await fireEvent.click(screen.getByText("Image descriptions (1)"));
    const checkbox = screen.getByRole("checkbox", { name: "Confirm decorative or fully described in nearby text" });
    await fireEvent.click(checkbox);
    expect((screen.getByLabelText("Image 1 alt text") as HTMLTextAreaElement).disabled).toBe(true);
    await fireEvent.click(checkbox);
    expect((screen.getByLabelText("Image 1 alt text") as HTMLTextAreaElement).disabled).toBe(false);
    await fireEvent.click(screen.getByRole("button", { name: "Review changed choices" }));
    await waitFor(() => expect(onreview).toHaveBeenCalledWith(expect.objectContaining({ decorative_images: [] })));
  });

  it("opens the full source description and confirms short alt without claiming a completed accessibility evaluation", async () => {
    const preflight = review();
    preflight.outline.images[0] = { id: "diagram1", alt: "Decision flowchart. Structure follows.", decorative: false };
    const oninspect = vi.fn();
    const onreview = vi.fn(async (options) => ({ ...preflight, options }));
    render(EpubReviewPanel, { props: { preflight, onreview, oninspect } });
    await fireEvent.click(screen.getByText("Image descriptions (1)"));
    await fireEvent.click(screen.getByRole("button", { name: "View source and full description" }));
    expect(oninspect).toHaveBeenCalledWith("diagram1");
    await fireEvent.click(screen.getByRole("button", { name: "Confirm this alt text" }));
    expect(screen.getByText("Needs human evaluation")).toBeTruthy();
    await fireEvent.click(screen.getByRole("button", { name: "Review changed choices" }));
    await waitFor(() => expect(onreview).toHaveBeenCalledWith({ image_descriptions: { diagram1: "Decision flowchart. Structure follows." }, accessibility_evaluation: null }));
  });

  it("shows an incomplete response as a recoverable error", async () => {
    const onreview = vi.fn().mockResolvedValueOnce({ options: {} }).mockResolvedValueOnce(review());
    render(EpubReviewPanel, { props: { onreview } });
    await fireEvent.click(screen.getByRole("button", { name: "Review EPUB" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("incomplete"));
    await fireEvent.click(screen.getByRole("button", { name: "Review EPUB" }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });

  it("rejects corrupt checker states and non-array outlines before rendering", () => {
    expect(requireEpubPreflight(review())).toEqual(review());
    const invalid = review();
    invalid.quality.checks.ace = "approved";
    expect(() => requireEpubPreflight(invalid)).toThrow("incomplete");
    expect(() => requireEpubPreflight({ ...review(), outline: {} })).toThrow("incomplete");
  });

  it("adds temporary, collision-free anchors while preserving authored anchors", () => {
    const html = '<main><h2 id="epub-heading-1">Authored</h2><h2>New</h2><table><tr><td>Cell</td></tr></table><img class="cover" alt="Cover"><img alt="Art"></main>';
    const document = new DOMParser().parseFromString(withEpubReviewAnchors(html), "text/html");
    expect([...document.querySelectorAll("h2")].map((tag) => tag.id)).toEqual(["epub-heading-1", "epub-heading-1-2"]);
    expect(document.querySelector("table")?.id).toBe("epub-table-1");
    expect([...document.querySelectorAll("img")].map((tag) => tag.id)).toEqual(["epub-cover-image", "epub-image-1"]);
  });

  it("confirms long descriptions separately from short alts", async () => {
    const preflight = review();
    preflight.options = { profile: "publishing-v2" };
    preflight.quality.profile = "publishing-v2";
    preflight.outline.images[0].description_sha256 = "d".repeat(64);
    preflight.outline.images[0].description_id = "art-description";
    const onreview = vi.fn(async (options) => ({ ...preflight, options }));
    render(EpubReviewPanel, { props: { preflight, options: preflight.options, onreview } });
    await fireEvent.click(screen.getByText("Image descriptions (1)"));
    await fireEvent.click(screen.getByRole("button", { name: "Confirm this alt text" }));
    const checkbox = screen.getByRole("checkbox", { name: /I checked the full description/ });
    expect((checkbox as HTMLInputElement).checked).toBe(false);
    await fireEvent.click(checkbox);
    await fireEvent.click(screen.getByRole("button", { name: "Review changed choices" }));
    await waitFor(() => expect(onreview).toHaveBeenCalledWith(expect.objectContaining({ image_descriptions: { art: "A source description" }, long_descriptions: { art: "d".repeat(64) }, accessibility_evaluation: null })));
    expect(screen.getByText("Needs human evaluation")).toBeTruthy();
  });

  it("drops conflicting paragraph edits when the entire section is omitted", async () => {
    const preflight = review();
    preflight.options = { profile: "publishing-v2" };
    preflight.quality.profile = "publishing-v2";
    preflight.outline.front_matter = [{ id: "title", role: "titlepage", title: "Edition title", sha256: "d".repeat(64), blocks: [{ id: "credit", text: "Edited by Someone", sha256: "e".repeat(64), editable: true, duplicate_of: null }] }];
    const onreview = vi.fn(async (options) => ({ ...preflight, options }));
    render(EpubReviewPanel, { props: { preflight, options: preflight.options, onreview } });
    await fireEvent.click(screen.getByText("Front matter (1 sections)"));
    await fireEvent.change(screen.getByLabelText("EPUB paragraph"), { target: { value: "Reviewed credit" } });
    await fireEvent.click(screen.getByRole("checkbox", { name: "Omit this section from the EPUB" }));
    expect((screen.getByLabelText("EPUB paragraph") as HTMLTextAreaElement).disabled).toBe(true);
    await fireEvent.click(screen.getByRole("button", { name: "Review changed choices" }));
    await waitFor(() => expect(onreview).toHaveBeenCalledWith(expect.objectContaining({ omitted_front_matter: { title: "d".repeat(64) }, front_matter_edits: {}, front_matter_order: [] })));
  });

  it("offers keyboard crop controls and rejects an empty crop", async () => {
    render(EpubCoverCrop, { props: { src: "/synthetic-cover.png" } });
    await fireEvent.click(screen.getByRole("button", { name: "Right half" }));
    expect((screen.getByLabelText("Left (%)") as HTMLInputElement).value).toBe("50");
    await fireEvent.input(screen.getByLabelText("Right (%)"), { target: { value: "50" } });
    expect(screen.getByRole("alert").textContent).toContain("at least 1%");
    await fireEvent.click(screen.getByRole("button", { name: "Full visible page" }));
    expect(screen.queryByRole("alert")).toBeNull();
    expect((screen.getByLabelText("Left (%)") as HTMLInputElement).value).toBe("0");
  });

  it("binds a new copyright section to the paragraph witness and retires old section order", async () => {
    const preflight = review();
    preflight.options = { profile: "publishing-v2", front_matter_order: ["title"] };
    preflight.quality.profile = "publishing-v2";
    preflight.outline.front_matter = [{ id: "title", source_anchor: "authored-title", role: "titlepage", title: "Title and legal matter", sha256: "d".repeat(64), blocks: [{ id: "legal", text: "Copyright 2026.", sha256: "e".repeat(64), editable: true, can_start_section: true, duplicate_of: null }] }];
    const onreview = vi.fn(async (options) => ({ ...preflight, options }));
    const oninspect = vi.fn();
    render(EpubReviewPanel, { props: { preflight, options: preflight.options, onreview, oninspect } });
    await fireEvent.click(screen.getByText("Front matter (1 sections)"));
    await fireEvent.change(screen.getByLabelText("Start a section at this paragraph"), { target: { value: "copyright-page" } });
    await fireEvent.click(screen.getByRole("button", { name: "View source" }));
    expect(oninspect).toHaveBeenCalledWith("authored-title");
    await fireEvent.click(screen.getByRole("button", { name: "Review changed choices" }));
    await waitFor(() => expect(onreview).toHaveBeenCalledWith(expect.objectContaining({ front_matter_breaks: { legal: { sha256: "e".repeat(64), role: "copyright-page" } }, front_matter_order: [], accessibility_evaluation: null })));
  });

  it("reports save conflicts and permits a fresh load without pretending the save succeeded", async () => {
    const edition = requireEpubEdition({ version: 0, recipe: null, effective_recipe: null, source_revision: null, current_source_revision: "d".repeat(64), stale: false });
    const onsave = vi.fn().mockRejectedValue(new Error("Choices changed in another window. Reload them."));
    const onload = vi.fn().mockResolvedValue(undefined);
    render(EpubEditionControls, { props: { edition, onsave, onload, onclear: vi.fn() } });
    await fireEvent.click(screen.getByRole("button", { name: "Save edition choices" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("another window"));
    expect(screen.queryByText("Edition choices saved.")).toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: "Load saved choices" }));
    await waitFor(() => expect(screen.getByRole("status").textContent).toBe("Saved choices loaded."));
    expect(onload).toHaveBeenCalledOnce();
  });

  it("rejects malformed long-description witnesses and image byte receipts", () => {
    const preflight = review();
    preflight.outline.images[0].description_sha256 = "invalid";
    expect(() => requireEpubPreflight(preflight)).toThrow("incomplete");
    preflight.outline.images[0].description_sha256 = null;
    preflight.quality.image_bytes = { source: -1, packaged: 42 };
    expect(() => requireEpubPreflight(preflight)).toThrow("incomplete");
  });

  it("permits forgetting saved choices when the source is unavailable", async () => {
    const recipe = { epub_options: {}, epub_cover: {}, epub_image_mode: "auto" };
    const edition = requireEpubEdition({ version: 2, recipe, effective_recipe: recipe, source_revision: "d".repeat(64), current_source_revision: null, stale: true });
    const onclear = vi.fn().mockResolvedValue(undefined);
    render(EpubEditionControls, { props: { edition, onsave: vi.fn(), onload: vi.fn(), onclear } });
    expect(screen.getByText(/source is not ready for export/)).toBeTruthy();
    await fireEvent.click(screen.getByRole("button", { name: "Forget saved choices" }));
    await waitFor(() => expect(onclear).toHaveBeenCalledOnce());
    expect(screen.getByText("Saved choices removed.")).toBeTruthy();
  });

  it.each([null, true, ["jpeg"], "JPEG", "avif"])("rejects a malformed saved image mode %j", (mode) => {
    const recipe = { epub_options: {}, epub_cover: {}, epub_image_mode: mode };
    expect(() => requireEpubEdition({ version: 1, recipe, effective_recipe: recipe, source_revision: "d".repeat(64), current_source_revision: "d".repeat(64), stale: false })).toThrow("incomplete");
  });

  it("labels a shortened front-matter preview and prevents partial-text editing", async () => {
    const preflight = review();
    preflight.options = { profile: "publishing-v2" };
    preflight.quality.profile = "publishing-v2";
    preflight.outline.front_matter = [{ id: "rights", role: "copyright-page", title: "Copyright", sha256: "d".repeat(64), blocks: [{ id: "legal", text: "A long paragraph excerpt…", sha256: "e".repeat(64), editable: false, truncated: true, duplicate_of: null }] }];
    render(EpubReviewPanel, { props: { preflight, options: preflight.options, onreview: vi.fn(), oninspect: vi.fn() } });
    await fireEvent.click(screen.getByText("Front matter (1 sections)"));
    expect(screen.getByText(/Excerpt of a long paragraph/)).toBeTruthy();
    expect(screen.queryByLabelText("EPUB paragraph")).toBeNull();
    expect(screen.getByText("A long paragraph excerpt…")).toBeTruthy();
  });
});
