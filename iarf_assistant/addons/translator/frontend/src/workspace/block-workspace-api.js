import { ApiError, createIdempotencyKey } from "../lib/api.js";
import { validateProjectStatusPayload } from "../lib/utils.js";
import { requireMatchingEditorStructure, validateCompactEditor } from "./compact-editor.js";

export const BLOCK_WORKSPACE_CONTRACT = "translator-block-workspace-v5";
export const ALIGNED_BLOCK_WORKSPACE_CONTRACT = "translator-block-workspace-v6";
export const BLOCK_GLOSSARY_CONTRACT = "translator-block-glossary-v3";
export const DOCUMENT_BLOCK_ENGINE = "document-block-v1";
export const LEGACY_ENGINE = "segment-v1";
export const BLOCK_EXPORT_FORMATS = Object.freeze(["epub", "docx", "pdf", "html"]);

const MAX_PAGE_LIMIT = 200;

function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function requireString(value, label) {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(`Invalid block workspace response: ${label} is missing.`);
  }
  return value;
}

function validatePagination(pagination) {
  if (!isRecord(pagination)) throw new Error("Invalid block workspace response: pagination.");
  const offset = Number(pagination.offset);
  const limit = Number(pagination.limit);
  const total = Number(pagination.total);
  if (![offset, limit, total].every(Number.isInteger) || offset < 0 || limit < 1 || limit > MAX_PAGE_LIMIT || total < 0) {
    throw new Error("Invalid block workspace response: pagination values.");
  }
  return {
    offset,
    limit,
    total,
    has_more: Boolean(pagination.has_more)
  };
}

function validateTargetSummary(summary, paginationTotal) {
  if (!isRecord(summary)) throw new Error("Invalid block workspace response: target summary.");
  const keys = Object.keys(summary).sort().join(",");
  if (keys !== "active_targets,document_complete,structurally_valid_active_targets,total_blocks") {
    throw new Error("Invalid block workspace response: target summary keys.");
  }
  const totalBlocks = summary.total_blocks;
  const activeTargets = summary.active_targets;
  const validTargets = summary.structurally_valid_active_targets;
  if (![totalBlocks, activeTargets, validTargets].every((value) => Number.isInteger(value) && value >= 0)
    || validTargets > activeTargets
    || activeTargets > totalBlocks
    || typeof summary.document_complete !== "boolean"
    || summary.document_complete !== (validTargets === totalBlocks)
    || totalBlocks !== paginationTotal) {
    throw new Error("Invalid block workspace response: target summary values.");
  }
  return {
    total_blocks: totalBlocks,
    active_targets: activeTargets,
    structurally_valid_active_targets: validTargets,
    document_complete: summary.document_complete
  };
}

function validateAlignmentSentence(sentence) {
  if (!isRecord(sentence)
    || Object.keys(sentence).sort().join(",") !== "order,text"
    || !Number.isInteger(sentence.order) || sentence.order < 1
    || typeof sentence.text !== "string" || !sentence.text.trim()) {
    throw new Error("Invalid block workspace response: alignment sentence.");
  }
  return sentence;
}

function validateAlignment(alignment) {
  if (!isRecord(alignment)
    || Object.keys(alignment).sort().join(",") !== "groups,origin,review_state"
    || !["model_reported", "automatic_fallback", "carried_forward", "user_edited"].includes(alignment.origin)
    || !["needs_review", "approved"].includes(alignment.review_state)
    || !Array.isArray(alignment.groups) || !alignment.groups.length || alignment.groups.length > 4096) {
    throw new Error("Invalid block workspace response: sentence alignment.");
  }
  const sourceOrders = [];
  const targetOrders = [];
  const groups = alignment.groups.map((group, index) => {
    if (!isRecord(group)
      || Object.keys(group).sort().join(",") !== "kind,order,source_sentences,target_sentences"
      || group.order !== index + 1
      || !["one_to_one", "split", "merge", "many_to_many"].includes(group.kind)
      || !Array.isArray(group.source_sentences) || !group.source_sentences.length
      || !Array.isArray(group.target_sentences) || !group.target_sentences.length) {
      throw new Error("Invalid block workspace response: alignment group.");
    }
    const source_sentences = group.source_sentences.map(validateAlignmentSentence);
    const target_sentences = group.target_sentences.map(validateAlignmentSentence);
    const expectedKind = source_sentences.length === 1 && target_sentences.length === 1
      ? "one_to_one"
      : source_sentences.length === 1
        ? "split"
        : target_sentences.length === 1
          ? "merge"
          : "many_to_many";
    if (group.kind !== expectedKind) {
      throw new Error("Invalid block workspace response: alignment group kind.");
    }
    sourceOrders.push(...source_sentences.map((sentence) => sentence.order));
    targetOrders.push(...target_sentences.map((sentence) => sentence.order));
    return { ...group, source_sentences, target_sentences };
  });
  const exactRange = (values) => values.every((value, index) => value === index + 1);
  if (!exactRange(sourceOrders) || !exactRange(targetOrders)) {
    throw new Error("Invalid block workspace response: alignment coverage.");
  }
  return { ...alignment, groups };
}

function validateTarget(target, sourceEditor) {
  const keys = Object.keys(target).sort().join(",");
  if (!isRecord(target)
    || !["active_version,editor,translator_notes,validation", "active_version,alignment,editor,translator_notes,validation"].includes(keys)
    || !Number.isInteger(target.active_version)
    || target.active_version < 1
    || !isRecord(target.validation)
    || Object.keys(target.validation).sort().join(",") !== "findings,valid"
    || typeof target.validation.valid !== "boolean"
    || !Array.isArray(target.validation.findings)) {
    throw new Error("Invalid block workspace response: target projection.");
  }
  if (!Array.isArray(target.translator_notes) || target.translator_notes.length > 6) {
    throw new Error("Invalid block workspace response: translator notes.");
  }
  if (Object.prototype.hasOwnProperty.call(target, "alignment")) {
    target.alignment = validateAlignment(target.alignment);
  }
  for (const note of target.translator_notes) {
    if (!isRecord(note)
      || !["anchor_text,author_type,body,id", "anchor_text,author_type,body,body_html,id"].includes(Object.keys(note).sort().join(","))
      || typeof note.id !== "string" || !note.id
      || typeof note.anchor_text !== "string" || !note.anchor_text || note.anchor_text.length > 160
      || typeof note.body !== "string" || !note.body || note.body.length > 800
      || (note.body_html !== undefined && (typeof note.body_html !== "string" || note.body_html.length > 64000))
      || note.author_type !== "model") {
      throw new Error("Invalid block workspace response: translator note.");
    }
  }
  for (const finding of target.validation.findings) {
    if (!isRecord(finding)
      || Object.keys(finding).sort().join(",") !== "message,severity"
      || typeof finding.message !== "string"
      || typeof finding.severity !== "string") {
      throw new Error("Invalid block workspace response: validation finding.");
    }
  }
  requireMatchingEditorStructure(sourceEditor, target.editor);
  return target;
}

function validateAccess(access) {
  if (!isRecord(access) || !["owner", "reviewer", "viewer"].includes(access.role)) {
    throw new Error("Invalid block workspace response: access role.");
  }
  if (access.label !== null && access.label !== undefined && typeof access.label !== "string") {
    throw new Error("Invalid block workspace response: access label.");
  }
  return { role: access.role, label: access.label || null };
}

function validateReaderNote(note) {
  if (!isRecord(note)
    || !["anchor,author,body,citation_ids,id,state,version", "anchor,author,body,body_html,citation_ids,id,state,version"].includes(Object.keys(note).sort().join(","))
    || typeof note.id !== "string" || !note.id
    || !Number.isInteger(note.version) || note.version < 1
    || !["proposed", "active", "retired", "needs_review"].includes(note.state)
    || typeof note.body !== "string" || !note.body || note.body.length > 4000
    || (note.body_html !== undefined && (typeof note.body_html !== "string" || note.body_html.length > 64000))
    || !Array.isArray(note.citation_ids)
    || note.citation_ids.some((value) => typeof value !== "string" || !value || value.length > 255)
    || !isRecord(note.anchor)
    || !["whole_block", "source", "target"].includes(note.anchor.kind)
    || (note.anchor.text !== null && typeof note.anchor.text !== "string")
    || !isRecord(note.author)
    || typeof note.author.type !== "string"
    || typeof note.author.label !== "string") {
    throw new Error("Invalid block workspace response: reader note.");
  }
  if (note.anchor.kind === "whole_block"
    && (note.anchor.text !== null || note.anchor.occurrence_index !== null)) {
    throw new Error("Invalid block workspace response: whole-block note anchor.");
  }
  if (note.anchor.kind !== "whole_block"
    && (!note.anchor.text
      || note.anchor.text.length > 240
      || !Number.isInteger(note.anchor.occurrence_index)
      || note.anchor.occurrence_index < 0)) {
    throw new Error("Invalid block workspace response: reader note anchor.");
  }
  return note;
}

function validateBlockReview(review) {
  const required = ["actor", "decision", "rationale", "updated_at", "version"];
  const optional = ["effective_decision", "stale", "target_revision_id", "current_target_revision_id", "current_target_version", "coverage"];
  if (!isRecord(review)
    || required.some((key) => !Object.prototype.hasOwnProperty.call(review, key))
    || Object.keys(review).some((key) => !required.includes(key) && !optional.includes(key))
    || !["pending", "approved", "needs_changes"].includes(review.decision)
    || !Number.isInteger(review.version) || review.version < 0
    || (review.rationale !== null && typeof review.rationale !== "string")
    || (review.updated_at !== null && typeof review.updated_at !== "string")
    || (review.actor !== null && !isRecord(review.actor))) {
    throw new Error("Invalid block workspace response: block review.");
  }
  if ((review.effective_decision !== undefined && !["pending", "approved", "needs_changes"].includes(review.effective_decision))
    || (review.stale !== undefined && typeof review.stale !== "boolean")
    || ["target_revision_id", "current_target_revision_id"].some((key) => review[key] !== undefined && review[key] !== null && (typeof review[key] !== "string" || !review[key]))
    || (review.current_target_version !== undefined && review.current_target_version !== null && (!Number.isInteger(review.current_target_version) || review.current_target_version < 1))
    || (review.coverage !== undefined && !isRecord(review.coverage))) {
    throw new Error("Invalid block workspace response: review evidence.");
  }
  if (review.coverage !== undefined && Object.entries(review.coverage).some(([kind, evidence]) =>
    !["structural", "target_reading", "bilingual"].includes(kind)
    || !isRecord(evidence) || typeof evidence.complete !== "boolean"
    || !Number.isInteger(evidence.reviewed_source_anchor_count) || evidence.reviewed_source_anchor_count < 0
    || !Array.isArray(evidence.coverage_ids) || evidence.coverage_ids.some((id) => typeof id !== "string" || !id)
    || !Array.isArray(evidence.reviewed_source_anchor_ids) || evidence.reviewed_source_anchor_ids.some((id) => typeof id !== "string" || !id))) {
    throw new Error("Invalid block workspace response: coverage evidence.");
  }
  return review;
}

export function validateBlockWorkspaceBlock(block) {
  if (!isRecord(block)
    || Object.keys(block).sort().join(",") !== "id,order,reader_notes,review,role,source,target") {
    throw new Error("Invalid block workspace response: block item.");
  }
  requireString(block.id, "block id");
  requireString(block.role, "block role");
  if (!Number.isInteger(Number(block.order)) || Number(block.order) < 1) throw new Error("Invalid block workspace response: block order.");
  if (!isRecord(block.source) || Object.keys(block.source).join(",") !== "editor") {
    throw new Error("Invalid block workspace response: source projection.");
  }
  const sourceEditor = validateCompactEditor(block.source.editor, "source editor projection");
  if (block.target != null && !isRecord(block.target)) throw new Error("Invalid block workspace response: target.");
  if (block.target != null) validateTarget(block.target, sourceEditor);
  if (!Array.isArray(block.reader_notes) || block.reader_notes.length > 100) {
    throw new Error("Invalid block workspace response: reader notes.");
  }
  block.reader_notes.forEach(validateReaderNote);
  validateBlockReview(block.review);
  return block;
}

export function validateBlockWorkspacePayload(payload) {
  if (!isRecord(payload) || ![BLOCK_WORKSPACE_CONTRACT, ALIGNED_BLOCK_WORKSPACE_CONTRACT].includes(payload.contract)) {
    throw new Error("This project returned an incompatible block workspace contract.");
  }
  const aligned = payload.contract === ALIGNED_BLOCK_WORKSPACE_CONTRACT;
  if ((aligned && payload.review_mode !== "aligned_sentences")
    || (!aligned && Object.prototype.hasOwnProperty.call(payload, "review_mode"))) {
    throw new Error("Invalid block workspace response: review mode.");
  }
  if (!isRecord(payload.project) || payload.project.engine_contract !== DOCUMENT_BLOCK_ENGINE) {
    throw new Error("This project is not using the document-block engine.");
  }
  if (!isRecord(payload.source_plan)
    || Object.keys(payload.source_plan).sort().join(",") !== "id,version"
    || !Number.isInteger(payload.source_plan.version)
    || payload.source_plan.version < 1) {
    throw new Error("Invalid block workspace response: active source plan.");
  }
  requireString(payload.source_plan.id, "source plan id");
  const access = validateAccess(payload.access);
  const pagination = validatePagination(payload.pagination);
  const target_summary = validateTargetSummary(payload.target_summary, pagination.total);
  if (!Array.isArray(payload.blocks)) throw new Error("Invalid block workspace response: blocks.");
  const blocks = payload.blocks.map(validateBlockWorkspaceBlock)
    .sort((left, right) => Number(left.order) - Number(right.order));
  if (blocks.some((block) => block.target && Boolean(block.target.alignment) !== aligned)) {
    throw new Error("Invalid block workspace response: alignment mode.");
  }
  return { ...payload, access, blocks, pagination, target_summary, review_mode: aligned ? "aligned_sentences" : "block" };
}

export function validateBlockGlossaryPayload(payload) {
  if (!isRecord(payload) || payload.contract !== BLOCK_GLOSSARY_CONTRACT) {
    throw new Error("This project returned an incompatible block glossary contract.");
  }
  requireString(payload.project_id, "glossary project id");
  requireString(payload.source_plan_id, "glossary source plan id");
  if (!Array.isArray(payload.entries)) throw new Error("Invalid block glossary response: entries.");
  if (!Array.isArray(payload.proposals)) throw new Error("Invalid block glossary response: proposals.");
  if (!Array.isArray(payload.suggestions)) throw new Error("Invalid block glossary response: suggestions.");
  const entries = payload.entries.map((entry) => {
    if (!isRecord(entry)) throw new Error("Invalid block glossary response: entry.");
    const keys = Object.keys(entry).sort().join(",");
    if (keys !== "active_version,concept_id,notes,scope,source_term,target_term,updated_at" && keys !== "active_version,concept_id,notes,scope,source_term,target_term,updated_at,usage") {
      throw new Error("Invalid block glossary response: entry keys.");
    }
    requireString(entry.concept_id, "glossary concept id");
    requireString(entry.source_term, "glossary source term");
    requireString(entry.target_term, "glossary target term");
    if (!Number.isInteger(entry.active_version) || entry.active_version < 1) {
      throw new Error("Invalid block glossary response: active version.");
    }
    if (entry.notes !== null && typeof entry.notes !== "string") {
      throw new Error("Invalid block glossary response: notes.");
    }
    if (entry.scope !== "document") throw new Error("Invalid block glossary response: scope.");
    if (entry.usage !== undefined) validateGlossaryUsage(entry.usage);
    return entry;
  });
  const proposals = payload.proposals.map((proposal) => {
    if (!isRecord(proposal)) throw new Error("Invalid block glossary response: proposal.");
    const keys = Object.keys(proposal).sort().join(",");
    if (keys !== "concept_id,notes,proposal_id,proposed_at,scope,source_term,target_term" && keys !== "concept_id,notes,proposal_id,proposed_at,scope,source_term,target_term,usage") {
      throw new Error("Invalid block glossary response: proposal keys.");
    }
    requireString(proposal.proposal_id, "glossary proposal id");
    requireString(proposal.concept_id, "glossary proposal concept id");
    requireString(proposal.source_term, "glossary proposal source term");
    requireString(proposal.target_term, "glossary proposal target term");
    requireString(proposal.proposed_at, "glossary proposal timestamp");
    if (proposal.notes !== null && typeof proposal.notes !== "string") {
      throw new Error("Invalid block glossary response: proposal notes.");
    }
    if (proposal.scope !== "document") throw new Error("Invalid block glossary response: proposal scope.");
    if (proposal.usage !== undefined) validateGlossaryUsage(proposal.usage);
    return proposal;
  });
  const suggestions = payload.suggestions.map((suggestion) => {
    if (!isRecord(suggestion)
      || Object.keys(suggestion).sort().join(",") !== "author,created_at,id,notes,source_term,state,target_term,version"
      || typeof suggestion.id !== "string" || !suggestion.id
      || !Number.isInteger(suggestion.version) || suggestion.version < 1
      || typeof suggestion.source_term !== "string" || !suggestion.source_term
      || typeof suggestion.target_term !== "string" || !suggestion.target_term
      || (suggestion.notes !== null && typeof suggestion.notes !== "string")
      || !["pending", "approved", "rejected", "withdrawn"].includes(suggestion.state)
      || typeof suggestion.created_at !== "string"
      || !isRecord(suggestion.author)) {
      throw new Error("Invalid block glossary response: glossary suggestion.");
    }
    return suggestion;
  });
  return { ...payload, entries, proposals, suggestions };
}

function validateGlossaryUsage(usage) {
  const keys = isRecord(usage) ? Object.keys(usage).sort().join(",") : "";
  const baseKeys = "match_policy,observations,provenance,sense,source_forms";
  const variantKeys = "accepted_target_forms,match_policy,observations,provenance,sense,source_forms";
  if (!isRecord(usage)
    || (keys !== baseKeys && keys !== variantKeys)
    || typeof usage.sense !== "string"
    || !["preferred", "exact"].includes(usage.match_policy)
    || typeof usage.provenance !== "string"
    || !Array.isArray(usage.source_forms)
    || usage.source_forms.some((form) => typeof form !== "string" || !form.trim())
    || (usage.accepted_target_forms !== undefined
      && (!Array.isArray(usage.accepted_target_forms)
        || usage.accepted_target_forms.some((form) => typeof form !== "string" || !form.trim())))
    || !Array.isArray(usage.observations)) {
    throw new Error("Invalid block glossary response: usage.");
  }
  for (const observation of usage.observations) {
    if (!isRecord(observation)
      || Object.keys(observation).sort().join(",") !== "anchor_id,block_id,end_offset,start_offset,surface_form"
      || typeof observation.block_id !== "string"
      || (observation.anchor_id !== null && typeof observation.anchor_id !== "string")
      || !Number.isInteger(observation.start_offset) || observation.start_offset < 0
      || !Number.isInteger(observation.end_offset) || observation.end_offset <= observation.start_offset
      || typeof observation.surface_form !== "string" || !observation.surface_form) {
      throw new Error("Invalid block glossary response: occurrence.");
    }
  }
}

export function createBlockWorkspaceClient(client, projectId) {
  const encodedProjectId = encodeURIComponent(projectId);
  return {
    async load({ offset = 0, limit = 40, signal } = {}) {
      const query = new URLSearchParams({ offset: String(offset), limit: String(limit) });
      const payload = await client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/block-workspace?${query}`, { signal });
      return validateBlockWorkspacePayload(payload);
    },
    async loadStatus({ signal } = {}) {
      const payload = await client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/status`, { signal });
      return validateProjectStatusPayload(payload, projectId);
    },
    async loadGlossary({ signal } = {}) {
      const payload = await client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/block-glossary`, { signal });
      return validateBlockGlossaryPayload(payload);
    },
    async saveGlossaryEntry(entry, { signal } = {}) {
      const conceptId = typeof entry?.concept_id === "string" && entry.concept_id
        ? `/${encodeURIComponent(entry.concept_id)}`
        : "";
      const method = conceptId ? "PUT" : "POST";
      const payload = await client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/block-glossary${conceptId}`, {
        method,
        signal,
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": createIdempotencyKey(`translator.block.glossary.${entry?.concept_id || "new"}`)
        },
        body: JSON.stringify({
          source_term: String(entry?.source_term || ""),
          target_term: String(entry?.target_term || ""),
          notes: entry?.notes ? String(entry.notes) : null,
          accepted_target_forms: Array.isArray(entry?.accepted_target_forms)
            ? entry.accepted_target_forms.map((item) => String(item)).filter(Boolean)
            : Array.isArray(entry?.usage?.accepted_target_forms)
              ? entry.usage.accepted_target_forms.map((item) => String(item)).filter(Boolean)
              : [],
          expected_active_version: Number(entry?.active_version || 0),
          expected_proposal_id: entry?.proposal_id ? String(entry.proposal_id) : null
        })
      });
      return validateBlockGlossaryPayload(payload);
    },
    async createGlossarySuggestion(entry, { signal } = {}) {
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/glossary-suggestions`, {
        method: "POST",
        signal,
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": createIdempotencyKey(`translator.glossary.suggestion.${projectId}`)
        },
        body: JSON.stringify({
          source_term: String(entry?.source_term || ""),
          target_term: String(entry?.target_term || ""),
          notes: entry?.notes ? String(entry.notes) : null
        })
      });
    },
    async resolveGlossarySuggestion(suggestionId, action, entry, { signal } = {}) {
      if (!suggestionId || !["approve", "reject"].includes(action)) {
        throw new Error("Choose a valid glossary suggestion action.");
      }
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/glossary-suggestions/${encodeURIComponent(suggestionId)}/resolve`, {
        method: "POST",
        signal,
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": createIdempotencyKey(`translator.glossary.suggestion.${suggestionId}.${action}`)
        },
        body: JSON.stringify({
          action,
          expected_version: Number(entry?.version || 0),
          source_term: entry?.source_term == null ? null : String(entry.source_term),
          target_term: entry?.target_term == null ? null : String(entry.target_term),
          notes: entry?.notes ? String(entry.notes) : null
        })
      });
    },
    async createReaderNote(blockId, note, { signal } = {}) {
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/blocks/${encodeURIComponent(blockId)}/reader-notes`, {
        method: "POST",
        signal,
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": createIdempotencyKey(`translator.reader-note.${blockId}`)
        },
        body: JSON.stringify(note)
      });
    },
    async reviseReaderNote(noteId, note, expectedVersion, { signal } = {}) {
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/reader-notes/${encodeURIComponent(noteId)}`, {
        method: "PUT",
        signal,
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": createIdempotencyKey(`translator.reader-note.${noteId}.revision`)
        },
        body: JSON.stringify({ ...note, expected_version: expectedVersion })
      });
    },
    async transitionReaderNote(noteId, state, expectedVersion, { signal } = {}) {
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/reader-notes/${encodeURIComponent(noteId)}/state`, {
        method: "POST",
        signal,
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": createIdempotencyKey(`translator.reader-note.${noteId}.${state}`)
        },
        body: JSON.stringify({ state, expected_version: expectedVersion })
      });
    },
    async saveBlockReview(blockId, decision, expectedVersion, expectedTargetVersion, rationale = null, { signal } = {}) {
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/blocks/${encodeURIComponent(blockId)}/review`, {
        method: "PUT",
        signal,
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": createIdempotencyKey(`translator.block-review.${blockId}.${decision}`)
        },
        body: JSON.stringify({
          decision,
          rationale: rationale ?? "",
          expected_version: expectedVersion,
          expected_target_version: expectedTargetVersion
        })
      });
    },
    async recordReviewCoverage(blockId, kind, expectedTargetVersion, { signal } = {}) {
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/blocks/${encodeURIComponent(blockId)}/review-coverage`, {
        method: "POST", signal,
        headers: { "Content-Type": "application/json", "Idempotency-Key": createIdempotencyKey(`translator.coverage.${blockId}.${kind}`) },
        body: JSON.stringify({ kind, expected_target_version: expectedTargetVersion, scope: "block", source_anchor_ids: [] })
      });
    },
    async loadQaDiagnostics({ categories = [], severities = [], blockIds = [], includeSpelling = true, groupBy = "finding", includeExcerpts = true, groupId = null, cursor = null, limit = 100, signal } = {}) {
      const params = new URLSearchParams();
      if (categories.length) params.set("categories", categories.join(","));
      if (severities.length) params.set("severities", severities.join(","));
      for (const blockId of blockIds) params.append("block_id", String(blockId));
      if (!includeSpelling) params.set("include_spelling", "0");
      params.set("group_by", groupBy);
      if (!includeExcerpts) params.set("include_excerpts", "0");
      if (groupId) params.set("group_id", String(groupId));
      if (cursor) params.set("cursor", String(cursor));
      params.set("limit", String(limit));
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/qa-diagnostics?${params}`, { signal });
    },
    async loadResearchDossiers({ signal } = {}) {
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/research-dossiers`, { signal });
    },
    async loadResearchDossier(dossierId, { revisionId = null, signal } = {}) {
      const params = new URLSearchParams({ include_payload: "1" });
      if (revisionId) params.set("revision_id", String(revisionId));
      return client.fetchJson(
        `/addons/translator/api/projects/${encodedProjectId}/research-dossiers/${encodeURIComponent(String(dossierId))}?${params}`,
        { signal }
      );
    },
    async submitResearchDossier(dossier, { signal } = {}) {
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/research-dossiers`, {
        method: "POST",
        signal,
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": String(dossier?.client_submission_id || createIdempotencyKey(`translator.research-dossier.${projectId}`))
        },
        body: JSON.stringify(dossier)
      });
    },
    async listShareGrants({ signal } = {}) {
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/share-grants`, { signal });
    },
    async createShareGrant(grant, { signal } = {}) {
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/share-grants`, {
        method: "POST",
        signal,
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": createIdempotencyKey(`translator.share-grant.${projectId}`)
        },
        body: JSON.stringify(grant)
      });
    },
    async revokeShareGrant(grantId, { signal } = {}) {
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/share-grants/${encodeURIComponent(grantId)}`, {
        method: "DELETE",
        signal,
        headers: { "Idempotency-Key": createIdempotencyKey(`translator.share-grant.${grantId}.revoke`) }
      });
    },
    async createRetranslationRequest(requestPayload, { signal } = {}) {
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/retranslation-requests`, {
        method: "POST",
        signal,
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": createIdempotencyKey(`translator.retranslation-request.${projectId}`)
        },
        body: JSON.stringify(requestPayload)
      });
    },
    async listRetranslationRequests({ signal } = {}) {
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/retranslation-requests`, { signal });
    },
    async attachRetranslationDossier(requestId, dossierRevisionId, expectedVersion, { signal } = {}) {
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/retranslation-requests/${encodeURIComponent(requestId)}/dossier`, {
        method: "POST",
        signal,
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": createIdempotencyKey(`translator.retranslation-request.${requestId}.dossier`)
        },
        body: JSON.stringify({
          dossier_revision_id: dossierRevisionId,
          expected_version: expectedVersion
        })
      });
    },
    async executeRetranslationRequest(requestId, expectedVersion, { signal } = {}) {
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/retranslation-requests/${encodeURIComponent(requestId)}/execute`, {
        method: "POST",
        signal,
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": createIdempotencyKey(`translator.retranslation-request.${requestId}.execute`)
        },
        body: JSON.stringify({ expected_version: expectedVersion })
      });
    },
    async saveCandidate(blockId, candidate, expectedActiveVersion, { signal } = {}) {
      if (!blockId) throw new Error("A block identifier is required.");
      const encodedBlockId = encodeURIComponent(blockId);
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/blocks/${encodedBlockId}/candidate`, {
        method: "PUT",
        signal,
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": createIdempotencyKey(`translator.block.${blockId}`)
        },
        body: JSON.stringify({
          candidate,
          expected_active_version: expectedActiveVersion
        })
      });
    },
    async editSentence(blockId, sentenceEdit, expectedActiveVersion, { signal, idempotencyKey } = {}) {
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/blocks/${encodeURIComponent(blockId)}/candidate`, {
        method: "PUT",
        signal,
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKey || createIdempotencyKey(`translator.sentence.${blockId}`)
        },
        body: JSON.stringify({ sentence_edit: sentenceEdit, expected_active_version: expectedActiveVersion })
      });
    },
    async correctBlock(blockId, action, sourcePlan, { signal } = {}) {
      if (!blockId) throw new Error("A block identifier is required.");
      if (!["merge_previous", "merge_next", "exclude"].includes(action)) {
        throw new Error("A supported block correction is required.");
      }
      if (!isRecord(sourcePlan) || !sourcePlan.id || !Number.isInteger(sourcePlan.version)) {
        throw new Error("The active source plan is unavailable. Refresh the workspace.");
      }
      const encodedBlockId = encodeURIComponent(blockId);
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/blocks/${encodedBlockId}/correction`, {
        method: "POST",
        signal,
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": createIdempotencyKey(`translator.block.correction.${blockId}.${action}`)
        },
        body: JSON.stringify({
          action,
          expected_source_plan_id: sourcePlan.id,
          expected_active_version: sourcePlan.version
        })
      });
    },
    async translateAll({
      signal,
      mode = "pending",
      blockIds = [],
      translatorModel = null,
      translationReasoning = null
    } = {}) {
      if (!["pending", "explicit"].includes(mode)) {
        throw new Error("Choose a supported translation scope.");
      }
      const selectedBlockIds = Array.isArray(blockIds)
        ? blockIds.map((value) => String(value || "").trim()).filter(Boolean)
        : [];
      if ((mode === "pending" && selectedBlockIds.length)
        || (mode === "explicit" && !selectedBlockIds.length)
        || new Set(selectedBlockIds).size !== selectedBlockIds.length) {
        throw new Error("Choose a valid block selection.");
      }
      const body = {
        scope: { mode, block_ids: selectedBlockIds }
      };
      if (translatorModel) body.translator_model = String(translatorModel);
      if (translationReasoning) body.translation_reasoning = String(translationReasoning);
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/translate_all`, {
        method: "POST",
        signal,
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": createIdempotencyKey(`translator.block.translate.${projectId}`)
        },
        body: JSON.stringify(body)
      });
    },
    async cancelActiveOperation({ signal } = {}) {
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/cancel`, {
        method: "POST",
        signal,
        headers: { Accept: "application/json" }
      });
    },
    async preview({ signal } = {}) {
      return client.fetchText(`/addons/translator/api/projects/${encodedProjectId}/preview`, { signal });
    },
    async numberingPreview({ signal } = {}) {
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/numbering`, { signal });
    },
    async numberingApply(payload, { signal } = {}) {
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/numbering`, {
        method: "POST", signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
      });
    },
    async numberingUndo(payload, { signal } = {}) {
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/numbering/undo`, {
        method: "POST", signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
      });
    },
    async numberingSourcePreview({ signal } = {}) {
      return client.fetchText(`/addons/translator/api/projects/${encodedProjectId}/numbering/source-preview`, { signal });
    },
    async epubPreflight({ signal, options = {}, metadata = {}, epubCover = null, epubImageMode = null } = {}) {
      if (epubImageMode !== null && !["auto", "jpeg", "webp"].includes(epubImageMode)) throw new Error("Invalid EPUB image format.");
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/epub/preflight`, {
        method: "POST", signal, headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ epub_options: options, export_metadata: metadata, ...(epubCover ? { epub_cover: epubCover } : {}), ...(epubImageMode !== null ? { epub_image_mode: epubImageMode } : {}) })
      });
    },
    async getEpubEdition({ signal } = {}) {
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/epub/edition`, { signal });
    },
    async saveEpubEdition({ signal, recipe, expectedVersion, expectedSourceRevision } = {}) {
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/epub/edition`, { method: "PUT", signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ recipe, expected_version: expectedVersion, expected_source_revision: expectedSourceRevision }) });
    },
    async clearEpubEdition({ signal, expectedVersion } = {}) {
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/epub/edition`, { method: "DELETE", signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ expected_version: expectedVersion }) });
    },
    async exportDocument(format, { signal, metadata = {}, startH2OnNewPage = false, reviewRequirement = "none", epubCover = null, epubOptions = null, epubImageMode = null, epubReviewHash = null } = {}) {
      if (!BLOCK_EXPORT_FORMATS.includes(format)) {
        throw new Error("Block exports support only EPUB, DOCX, PDF, and HTML.");
      }
      if (!["none", "structural", "target_reading", "bilingual"].includes(reviewRequirement)) {
        throw new Error("Invalid export review requirement.");
      }
      if (epubImageMode !== null && (format !== "epub" || !["auto", "jpeg", "webp"].includes(epubImageMode))) throw new Error("Invalid EPUB image format.");
      const query = new URLSearchParams({ format });
      return client.fetchJson(`/addons/translator/api/projects/${encodedProjectId}/export?${query}`, {
        method: "POST",
        signal,
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "Idempotency-Key": createIdempotencyKey(`translator.block.export.${format}`)
        },
        body: JSON.stringify({ export_metadata: metadata, ...(format === "epub" ? { epub_options: epubOptions || {}, ...(epubImageMode !== null ? { epub_image_mode: epubImageMode } : {}), ...(epubReviewHash ? { epub_review_hash: epubReviewHash } : {}) } : {}), ...(format === "epub" && epubCover ? { epub_cover: epubCover } : {}), ...(reviewRequirement !== "none" ? { review_requirement: reviewRequirement } : {}), ...(["docx", "pdf"].includes(format) && startH2OnNewPage ? { start_h2_on_new_page: true } : {}) })
      });
    },
    async jobStatus(statusUrl, { signal } = {}) {
      if (typeof statusUrl !== "string" || !statusUrl.trim()) {
        throw new Error("The export service did not return a job status URL.");
      }
      return client.fetchJson(statusUrl, { signal });
    }
  };
}

export function isConflictError(error) {
  return error instanceof ApiError && error.status === 409;
}
