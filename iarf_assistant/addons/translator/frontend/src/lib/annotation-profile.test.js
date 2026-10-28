import { describe, expect, it } from "vitest";
import {
  annotationProfileLabel,
  annotationProfileUsesModelNotes,
  normalizeAnnotationProfile
} from "./annotation-profile.js";

describe("translator annotation profiles", () => {
  it("normalizes the closed profile vocabulary", () => {
    expect(normalizeAnnotationProfile("STUDY")).toBe("study");
    expect(normalizeAnnotationProfile("unknown")).toBe("none");
  });

  it("maps the legacy boolean to the restrained profile", () => {
    expect(normalizeAnnotationProfile(undefined, true)).toBe("restrained");
    expect(normalizeAnnotationProfile(undefined, false)).toBe("none");
  });

  it("labels profiles and distinguishes model-note generation", () => {
    expect(annotationProfileLabel("study")).toBe("Study translation");
    expect(annotationProfileUsesModelNotes("restrained")).toBe(true);
    expect(annotationProfileUsesModelNotes("none")).toBe(false);
  });
});
