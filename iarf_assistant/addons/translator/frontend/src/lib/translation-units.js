export const BLOCK_TRANSLATION_UNIT = "block";
export const ALIGNED_SENTENCE_TRANSLATION_UNIT = "aligned_sentences";

export const TRANSLATION_UNIT_MODES = Object.freeze([
  {
    value: BLOCK_TRANSLATION_UNIT,
    label: "Semantic blocks",
    description: "Current workflow: translate and edit each complete structural block as one continuous unit."
  },
  {
    value: ALIGNED_SENTENCE_TRANSLATION_UNIT,
    label: "Aligned sentence groups",
    description: "Segment the source sentences first, then translate complete blocks with explicit split/merge groups for side-by-side review."
  }
]);

export function normalizeTranslationUnitMode(value) {
  return value === ALIGNED_SENTENCE_TRANSLATION_UNIT
    ? ALIGNED_SENTENCE_TRANSLATION_UNIT
    : BLOCK_TRANSLATION_UNIT;
}

export const TRANSLATION_CONTEXT_MODES = Object.freeze([
  {
    value: "self_contained",
    label: "Fresh context for each batch",
    description: "Include a short source and translation excerpt from the preceding passage."
  },
  {
    value: "retained",
    label: "Continue in the same conversation",
    description: "Omit the preceding passage when the translator already has it in context."
  }
]);

export function normalizeTranslationContextMode(value) {
  return value === "retained" ? "retained" : "self_contained";
}
