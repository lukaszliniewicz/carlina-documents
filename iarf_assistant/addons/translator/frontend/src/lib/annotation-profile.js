export const ANNOTATION_PROFILES = Object.freeze([
  {
    value: "none",
    label: "None",
    description: "Do not ask the model for new notes. Existing and human-authored notes remain available."
  },
  {
    value: "restrained",
    label: "Restrained",
    description: "Add an occasional note only when the translation cannot carry essential cultural or conceptual context itself."
  },
  {
    value: "study",
    label: "Study translation",
    description: "Treat concise explanatory notes for allusions, technical terms, retained source language, and consequential ambiguity as part of the edition."
  }
]);

const VALUES = new Set(ANNOTATION_PROFILES.map((profile) => profile.value));

export function normalizeAnnotationProfile(value, legacyEnabled = false) {
  if (typeof value === "string" && VALUES.has(value.trim().toLowerCase())) {
    return value.trim().toLowerCase();
  }
  return legacyEnabled === true ? "restrained" : "none";
}

export function annotationProfileUsesModelNotes(value) {
  return normalizeAnnotationProfile(value) !== "none";
}

export function annotationProfileLabel(value) {
  const normalized = normalizeAnnotationProfile(value);
  return ANNOTATION_PROFILES.find((profile) => profile.value === normalized)?.label || "None";
}
