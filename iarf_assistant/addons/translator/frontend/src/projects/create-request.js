import {
  selectedResearchProviderIds,
  selectedResearchToolSelections
} from "../lib/research-providers.js";
import {
  annotationProfileUsesModelNotes,
  normalizeAnnotationProfile
} from "../lib/annotation-profile.js";
import { normalizeTranslationContextMode, normalizeTranslationUnitMode } from "../lib/translation-units.js";

export function retainCreateRequestKey(previous, payload, createKey) {
  const fingerprint = JSON.stringify(payload);
  if (previous?.fingerprint === fingerprint && previous.key) {
    return { state: previous, key: previous.key };
  }
  const key = createKey();
  return { state: { fingerprint, key }, key };
}

/** Keep timed subtitles and semantic documents on their sole supported engines. */
export function buildProjectCreatePayload(form, researchEnabled) {
  if (form.enable_research && !researchEnabled) {
    throw new Error("Requested research is not executable with the current analysis, tool, provider, and connection selections.");
  }
  const payload = {
    ...form,
    engine_contract: form.source_type === "subtitle_studio"
      ? "segment-v1"
      : "document-block-v1",
    defer_source_planning: form.source_type !== "subtitle_studio",
    enable_research: Boolean(researchEnabled)
  };
  if (payload.engine_contract === "document-block-v1") {
    payload.translation_unit_mode = normalizeTranslationUnitMode(form.translation_unit_mode);
    payload.translation_context_mode = normalizeTranslationContextMode(form.translation_context_mode);
    payload.annotation_profile = normalizeAnnotationProfile(
      form.annotation_profile,
      form.enable_translator_notes
    );
    // Keep the legacy flag during the compatibility window. The backend
    // derives the same value and freezes the profile itself for new block
    // operations.
    payload.enable_translator_notes = annotationProfileUsesModelNotes(payload.annotation_profile);
    delete payload.research_plugin_ids;
    delete payload.chunk_size;
    payload.research_tool_selections = selectedResearchToolSelections(form.research_tool_selections);
  } else {
    delete payload.translation_unit_mode;
    delete payload.translation_context_mode;
    delete payload.annotation_profile;
    delete payload.research_tool_selections;
    delete payload.translation_batch_input_tokens;
    delete payload.translation_batch_max_blocks;
    payload.research_plugin_ids = selectedResearchProviderIds(form.research_plugin_ids);
  }
  return payload;
}
