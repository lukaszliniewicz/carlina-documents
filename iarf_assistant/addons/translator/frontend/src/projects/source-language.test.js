import { describe, expect, it } from "vitest";
import { defaultSourceLanguage, sourceLanguageForDocument } from "./source-language.js";

describe("document source language compatibility", () => {
  it("prefers the validated top-level language", () => {
    expect(sourceLanguageForDocument({ language: "pl", metadata: { language: "en" } })).toBe("pl");
  });

  it("falls back to metadata and normalises a BCP 47 tag", () => {
    expect(sourceLanguageForDocument({ metadata: { language: "EN_us" } })).toBe("en-US");
  });

  it("keeps auto-detect for absent or invalid metadata", () => {
    expect(sourceLanguageForDocument({ language: "not a language", metadata: { language: "???" } })).toBe("Auto-detect");
  });

  it("does not overwrite a language explicitly entered by the user", () => {
    expect(defaultSourceLanguage({ language: "pl" }, "English")).toBe("English");
  });
});
