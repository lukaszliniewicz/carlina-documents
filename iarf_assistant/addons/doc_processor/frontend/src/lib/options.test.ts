import { describe, expect, it } from "vitest";
import { appendProcessingSettings, applyBookQualityProfile, defaultProcessingSettings, normalizeProcessingSettings } from "./options";

describe("document processing profiles", () => {
  it("defaults an interactive upload to the quality-first book profile", () => {
    const settings = defaultProcessingSettings();
    expect(settings).toMatchObject({
      processing_profile: "book_quality",
      engine: "llm_vision",
      append_previous: true,
      parallel_pages: 1,
      metadata_prepass: true,
      metadata_pages: 12,
      metadata_end_pages: 4,
      toc_handling: "omit",
      ignore_footnotes: false,
      evaluate_pdf: false,
      llm_boundary_merge: false,
      extract_images: true,
      correct_images: true,
      skip_cropping: false,
      prompt_cache: true,
      removehf: true
    });
  });

  it("submits the canonical profile and explicit quality settings", () => {
    const form = new FormData();
    const settings = applyBookQualityProfile({
      ...defaultProcessingSettings(),
      toc_handling: "preserve"
    });
    expect(settings.toc_handling).toBe("omit");
    appendProcessingSettings(form, settings);
    expect(form.get("processing_profile")).toBe("book_quality");
    expect(form.get("engine")).toBe("llm_vision");
    expect(form.get("parallel_pages")).toBe("1");
    expect(form.get("append_previous")).toBe("true");
    expect(form.get("metadata_pages")).toBe("12");
    expect(form.get("metadata_end_pages")).toBe("4");
    expect(form.get("toc_handling")).toBe("omit");
    expect(form.get("evaluate_pdf")).toBe("false");
    expect(form.get("llm_boundary_merge")).toBe("false");

    const evaluationForm = new FormData();
    appendProcessingSettings(
      evaluationForm,
      applyBookQualityProfile({ ...defaultProcessingSettings(), evaluate_pdf: true })
    );
    expect(evaluationForm.get("evaluate_pdf")).toBe("true");
  });

  it("preserves an explicit evaluation choice when switching to Book quality", () => {
    const enabled = applyBookQualityProfile({
      ...defaultProcessingSettings(),
      processing_profile: "custom",
      evaluate_pdf: true
    });
    const disabled = applyBookQualityProfile({
      ...defaultProcessingSettings(),
      processing_profile: "custom",
      evaluate_pdf: false
    });

    expect(enabled.processing_profile).toBe("book_quality");
    expect(enabled.evaluate_pdf).toBe(true);
    expect(disabled.processing_profile).toBe("book_quality");
    expect(disabled.evaluate_pdf).toBe(false);
  });

  it("normalizes a missing evaluation field to the safe default", () => {
    const settings = applyBookQualityProfile({
      ...defaultProcessingSettings(),
      processing_profile: "custom",
      evaluate_pdf: undefined as never
    });

    expect(settings.evaluate_pdf).toBe(false);
  });

  it.each([true, false, undefined])("preserves an optional boundary correction choice: %s", (choice) => {
    const settings = applyBookQualityProfile({
      ...defaultProcessingSettings(),
      processing_profile: "custom",
      llm_boundary_merge: choice as never
    });
    expect(settings.llm_boundary_merge).toBe(choice ?? false);
    const form = new FormData();
    appendProcessingSettings(form, settings);
    expect(form.get("llm_boundary_merge")).toBe(String(choice ?? false));
  });

  it("normalizes an invalid legacy/custom concurrency combination", () => {
    const settings = normalizeProcessingSettings({
      ...defaultProcessingSettings(),
      processing_profile: "custom",
      parallel_pages: 20,
      append_previous: true
    });
    expect(settings.parallel_pages).toBe(10);
    expect(settings.append_previous).toBe(false);
  });
});
