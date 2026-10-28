<script>
  import { onDestroy, onMount } from "svelte";
  import { installNotePopovers } from "../lib/note-popovers.js";
  import { preparePreviewDocument } from "../lib/preview-document.js";
  import { Dialog, EpubCoverCrop, EpubEditionControls, EpubImageFormat, EpubReviewPanel, IconButton, prepareDownload, requireEpubEdition, requireEpubPreflight, resetEpubCoverReview, withEpubReviewAnchors } from "@carlina/ui";
  import { createApiClient, createIdempotencyKey, isAbortError } from "../lib/api.js";
  import { createAbortController } from "../lib/request-state.js";
  import { createPollingController, pollUntil } from "../lib/polling.js";
  import { normaliseModels } from "../lib/utils.js";
  import { annotationProfileUsesModelNotes, normalizeAnnotationProfile } from "../lib/annotation-profile.js";
  import { normalizeTranslationContextMode, normalizeTranslationUnitMode } from "../lib/translation-units.js";
  import { normaliseBlockResearchTools, selectedResearchToolSelections } from "../lib/research-providers.js";
  import {
    DEFAULT_TRANSLATION_BATCH_MAX_BLOCKS,
    DEFAULT_TRANSLATION_BATCH_INPUT_TOKENS
  } from "../lib/translation-batch-limits.js";
  import BlockList from "./BlockList.svelte";
  import BlockGlossaryPanel from "./BlockGlossaryPanel.svelte";
  import SharedGlossaryPanel from "./SharedGlossaryPanel.svelte";
  import BlockOperationState from "./BlockOperationState.svelte";
  import BlockWorkspaceSettingsDialog from "./BlockWorkspaceSettingsDialog.svelte";
  import ResearchDossierDialog from "./ResearchDossierDialog.svelte";
  import RetranslationDialog from "./RetranslationDialog.svelte";
  import RetranslationRequestsDialog from "./RetranslationRequestsDialog.svelte";
  import QADiagnosticsDialog from "./QADiagnosticsDialog.svelte";
  import ShareGrantsDialog from "./ShareGrantsDialog.svelte";
  import ConfirmationDialog from "../lib/ConfirmationDialog.svelte";
  import SemanticPreviewDialog from "./SemanticPreviewDialog.svelte";
  import NumberingSyncDialog from "./NumberingSyncDialog.svelte";
  import { BLOCK_EXPORT_FORMATS, createBlockWorkspaceClient, DOCUMENT_BLOCK_ENGINE, isConflictError, validateBlockWorkspaceBlock } from "./block-workspace-api.js";

  export let context = null;
  export let bootstrapData = null;
  export let project = null;

  const bootstrap = bootstrapData || (typeof window !== "undefined" ? window.TRANSLATOR_BOOTSTRAP || {} : {});
  const projectId = project?.id || bootstrap.projectId;
  const lifecycleController = createAbortController(context?.signal || null);
  const client = createApiClient({
    shareToken: bootstrap.shareToken || "",
    shareGrantToken: bootstrap.shareGrantToken || "",
    shareSessionCsrf: bootstrap.shareSessionCsrf || "",
    addonApi: context?.api || null,
    signal: context?.signal || null
  });
  const workspaceClient = createBlockWorkspaceClient(client, projectId);

  let workspaceProject = project || {
    id: projectId,
    name: bootstrap.projectName || "Translation Project",
    source_lang: bootstrap.sourceLang || "Source",
    target_lang: bootstrap.targetLang || "Target",
    engine_contract: bootstrap.engineContract || DOCUMENT_BLOCK_ENGINE
  };
  let blocks = [];
  let total = 0;
  let targetSummary = {
    total_blocks: 0,
    active_targets: 0,
    structurally_valid_active_targets: 0,
    document_complete: false
  };
  let sourcePlan = null;
  let accessRole = bootstrap.accessRole || (bootstrap.guestMode ? "viewer" : "owner");
  let accessLabel = bootstrap.accessLabel || "";
  let reviewMode = "block";
  let hasMore = false;
  let loading = true;
  let loadingMore = false;
  let error = "";
  let operation = null;
  let drafts = {};
  let sentenceEditingBlocks = new Set();
  const sentenceRequests = new Map();
  let conflicts = {};
  let savingBlocks = new Set();
  let loadedPages = new Map();
  let nextPageOffset = 0;
  let readSequence = 0;
  let saveSequences = new Map();
  let translationBusy = false;
  let stopBusy = false;
  let previewOpen = false;
  let qaOpen = false;
  let qaLoading = false;
  let qaLoadingMore = false;
  let qaPayload = null;
  let qaError = "";
  let qaGrouping = "token";
  let qaGroupId = null;
  let previewLoading = false;
  let previewHtml = "";
  let previewError = "";
  let exportOpen = false;
  let exportFormat = "epub";
  let startH2OnNewPage = false;
  let exportCoverMode = "existing";
  let exportCoverPage = 1;
  let exportCoverCrop = null;
  let epubEdition = null;
  let epubEditionBusy = false;
  let epubEditionBaseline = "";
  let exportCoverSource = null;
  let exportReviewRequirement = "none";
  let exportBusy = false;
  let epubOptions = {};
  let epubImageMode = "jpeg";
  let reviewedEpubImageMode = "auto";
  let epubPreflight = null;
  let epubQuality = null;
  let reviewedEpubCover = "";
  let exportStatus = "";
  let exportError = false;
  let exportDownloadUrl = "";
  let preparedExport = null;
  let exportTitle = "";
  let exportAuthors = "";
  let exportTranslators = "";
  let exportDetails = {};
  let exportIdentifiers = [];
  let exportContributors = [];
  let exportCoverFile = null;
  let exportCoverInput;
  let exportCoverPath = "";
  $: currentEpubCoverSignature = JSON.stringify([exportCoverMode, exportCoverMode === "pdf_page" ? exportCoverPage : null, exportCoverPath, exportCoverMode === "generated" ? [exportTitle, exportAuthors, exportDetails] : null]);
  $: if (epubPreflight && currentEpubCoverSignature !== reviewedEpubCover) {
    epubOptions = resetEpubCoverReview(epubOptions, epubPreflight);
    epubPreflight = null;
    epubQuality = null;
  }
  $: if (epubPreflight && epubImageMode !== reviewedEpubImageMode) {
    epubPreflight = null;
    epubQuality = null;
  }
  let exportMetadataLoaded = false;
  let exportMetadataLoading = false;
  let exportMetadataVersion = 0;
  let exportMetadataBaseline = {};
  let exportSuggestionBusy = false;
  let exportSuggestionStatus = "";
  let exportSuggestionError = false;
  let exportSuggestionModels = [];
  let exportSuggestionModel = "";
  let settingsOpen = false;
  let settingsBusy = false;
  let settingsError = "";
  let settingsModels = [];
  let researchTools = [];
  let researchToolsLoadError = "";
  let isLoadingResearchTools = false;
  let glossaryEntries = [];
  let glossaryProposals = [];
  let glossarySuggestions = [];
  let glossaryBusy = false;
  let glossaryError = "";
  let glossaryLoaded = false;
  let glossaryOpen = false;
  let glossaryTab = "book";
  let glossaryFocusConcept = "";
  let pendingCorrection = null;
  let correctionBusy = false;
  let selectionMode = false;
  let selectedBlockIds = new Set();
  let selectionAnchorId = null;
  let noteBusy = new Set();
  let noteErrors = {};
  let shareOpen = false;
  let numberingOpen = false;
  let shareBusy = false;
  let shareError = "";
  let shareGrants = [];
  let createdShareLink = "";
  let retranslationOpen = false;
  let retranslationError = "";
  let retranslationRequestsOpen = false;
  let retranslationRequests = [];
  let retranslationRequestsLoaded = false;
  let retranslationRequestsError = "";
  let retranslationRequestBusyId = "";
  let researchDossiers = [];
  let dossierOpen = false;
  let dossierBusy = false;
  let dossierError = "";
  let settingsForm = {
    translator_model: "",
    analyzer_model: "",
    researcher_model: "",
    enable_pre_analysis: false,
    enable_research: false,
    enable_translator_notes: false,
    annotation_profile: "none",
    translation_unit_mode: "block",
    translation_context_mode: "self_contained",
    research_tool_selections: [],
    translation_batch_input_tokens: DEFAULT_TRANSLATION_BATCH_INPUT_TOKENS,
    translation_batch_max_blocks: DEFAULT_TRANSLATION_BATCH_MAX_BLOCKS,
    custom_prompt: ""
  };

  const exportOptions = [
    {
      value: "epub",
      label: "EPUB",
      extension: ".epub",
      description: "Uses semantic headings and navigation; the printed source TOC is omitted by default."
    },
    {
      value: "docx",
      label: "DOCX",
      extension: ".docx",
      description: "Preserves the printed source TOC by default, alongside the semantic document structure."
    },
    {
      value: "pdf",
      label: "PDF",
      extension: ".pdf",
      description: "A paginated reading copy with selectable text, links and heading bookmarks."
    },
    {
      value: "html",
      label: "HTML",
      extension: ".html",
      description: "Preserves the printed source TOC by default and keeps the semantic markup inspectable."
    }
  ];

  const ACTIVE_OPERATION_STATES = new Set([
    "queued",
    "running",
    "executing",
    "started",
    "pending",
    "translating",
    "analyzing_terminology",
    "exporting",
    "waiting_external",
    "finalizing",
    "cancel_requested",
    "cancelling"
  ]);

  function isActiveOperation(value) {
    return Boolean(value?.id) && ACTIVE_OPERATION_STATES.has(String(value?.state || value?.status || "").toLowerCase());
  }

  function preferOperationProjection(current, incoming) {
    if (incoming?.id && (!isActiveOperation(current) || String(incoming.id) === String(current.id))) {
      return incoming;
    }
    if (isActiveOperation(current)) return current;
    return incoming?.id ? incoming : null;
  }

  function activeOperation() {
    return operationActive;
  }

  function returnToProjects(event) {
    event?.preventDefault?.();
    const path = "/addons/translator/";
    if (context?.navigate) {
      context.navigate(path);
      return;
    }
    window.location.href = path;
  }

  function setSaving(blockId, value) {
    const next = new Set(savingBlocks);
    if (value) next.add(blockId); else next.delete(blockId);
    savingBlocks = next;
  }

  function blockVersion(block) {
    return Number.isInteger(Number(block?.target?.active_version))
      ? Number(block.target.active_version)
      : 0;
  }

  function preferBlock(current, incoming) {
    if (!current) return incoming;
    const currentVersion = blockVersion(current);
    const incomingVersion = blockVersion(incoming);
    if (incomingVersion > currentVersion) return incoming;
    if (incomingVersion < currentVersion) return current;
    if (current.target && !incoming.target) return current;
    return incoming;
  }

  function rebuildBlocks() {
    const byId = new Map();
    for (const offset of [...loadedPages.keys()].sort((left, right) => left - right)) {
      for (const block of loadedPages.get(offset) || []) {
        const key = String(block.id);
        byId.set(key, preferBlock(byId.get(key), block));
      }
    }
    blocks = [...byId.values()].sort((left, right) => Number(left.order) - Number(right.order));
  }

  function updatePageCursor() {
    let cursor = 0;
    while (loadedPages.has(cursor)) {
      const page = loadedPages.get(cursor) || [];
      if (!page.length) break;
      cursor += page.length;
    }
    nextPageOffset = cursor;
    const highestLoadedEnd = Math.max(0, ...[...loadedPages.entries()].map(([offset, page]) => offset + page.length));
    hasMore = highestLoadedEnd < total;
  }

  function replacePage(offset, incoming) {
    loadedPages = new Map(loadedPages);
    loadedPages.set(Number(offset), incoming);
    rebuildBlocks();
    updatePageCursor();
  }

  function replaceLoadedBlock(incoming) {
    const nextPages = new Map(loadedPages);
    let found = false;
    let accepted = false;
    for (const [offset, page] of nextPages.entries()) {
      const nextPage = page.map((block) => {
        if (String(block.id) !== String(incoming.id)) return block;
        found = true;
        const selected = preferBlock(block, incoming);
        if (selected === incoming) accepted = true;
        return selected;
      });
      nextPages.set(offset, nextPage);
    }
    if (found) {
      loadedPages = nextPages;
      rebuildBlocks();
      if (accepted) qaPayload = null;
    }
    return found && accepted;
  }

  async function refreshOtherLoadedPages({ signal, currentOffset, requestId }) {
    const offsets = [...loadedPages.keys()]
      .filter((offset) => Number(offset) !== Number(currentOffset))
      .sort((left, right) => left - right);
    if (!offsets.length) return true;
    const payloads = await Promise.all(offsets.map((offset) => workspaceClient.load({ offset, limit: 40, signal })));
    if (requestId !== readSequence || lifecycleController.signal.aborted) return false;
    const nextPages = new Map(loadedPages);
    for (const payload of payloads) {
      nextPages.set(Number(payload.pagination.offset), payload.blocks);
    }
    loadedPages = nextPages;
    rebuildBlocks();
    updatePageCursor();
    return true;
  }

  async function loadWorkspace({ signal = lifecycleController.signal, append = false } = {}) {
    if (!projectId) throw new Error("A project identifier is required for the block workspace.");
    const offset = append ? nextPageOffset : 0;
    const requestId = ++readSequence;
    let payload;
    try {
      payload = await workspaceClient.load({ offset, limit: 40, signal });
    } catch (caught) {
      if (requestId !== readSequence || lifecycleController.signal.aborted) return false;
      throw caught;
    }
    if (requestId !== readSequence || lifecycleController.signal.aborted) return false;
    const operationWasActive = isActiveOperation(operation);
    const nextOperation = preferOperationProjection(operation, payload.latest_operation);
    workspaceProject = { ...workspaceProject, ...payload.project };
    accessRole = payload.access.role;
    accessLabel = payload.access.label || accessLabel;
    reviewMode = normalizeTranslationUnitMode(payload.review_mode);
    sourcePlan = payload.source_plan;
    total = payload.pagination.total;
    targetSummary = payload.target_summary;
    replacePage(payload.pagination.offset, payload.blocks);
    const operationIsActive = isActiveOperation(nextOperation);
    if (operationWasActive && !operationIsActive) {
      const refreshed = await refreshOtherLoadedPages({
        signal,
        currentOffset: payload.pagination.offset,
        requestId
      });
      if (!refreshed) return false;
    }
    // Publish the terminal projection only after every already-visible page
    // has caught up.  A transient page refresh failure leaves the operation
    // active so the polling controller retries instead of freezing stale rows.
    operation = nextOperation;
    error = "";
    return true;
  }

  async function refresh(signal = lifecycleController.signal) {
    try {
      return await loadWorkspace({ signal });
    } catch (caught) {
      if (isAbortError(caught)) return;
      error = caught.message || "Unable to load the semantic block workspace.";
    } finally {
      loading = false;
    }
  }

  async function refreshOperation(signal = lifecycleController.signal) {
    try {
      const payload = await workspaceClient.loadStatus({ signal });
      const operationWasActive = isActiveOperation(operation);
      const nextOperation = preferOperationProjection(operation, payload.latest_operation);
      if (operationWasActive && !isActiveOperation(nextOperation)) {
        // Keep the active projection published until every visible page has
        // passed the ordinary verified workspace boundary at terminal state.
        const glossaryWasLoaded = glossaryLoaded;
        const refreshed = await loadWorkspace({ signal });
        if (refreshed) qaPayload = null;
        if (refreshed && glossaryWasLoaded) {
          // Preliminary analysis may have added advisory terminology while the
          // operation ran. Refresh it once at the terminal boundary so an open
          // glossary never remains stale, without adding work to normal polls.
          glossaryLoaded = false;
          await loadGlossary(signal);
        }
        return refreshed;
      }
      operation = nextOperation;
      error = "";
      return true;
    } catch (caught) {
      if (isAbortError(caught)) return;
      error = caught.message || "Unable to refresh the block workflow status.";
      return false;
    }
  }

  async function loadGlossary(signal = lifecycleController.signal) {
    if (glossaryLoaded) return true;
    glossaryBusy = true;
    try {
      const payload = await workspaceClient.loadGlossary({ signal });
      glossaryEntries = payload.entries;
      glossaryProposals = payload.proposals;
      glossarySuggestions = payload.suggestions;
      glossaryError = "";
      glossaryLoaded = true;
      return true;
    } catch (caught) {
      if (isAbortError(caught)) return false;
      glossaryError = caught.message || "Unable to load the glossary and notes.";
      return false;
    } finally {
      glossaryBusy = false;
    }
  }

  async function saveGlossaryEntry(entry) {
    if (accessRole !== "owner" || glossaryBusy) return false;
    glossaryBusy = true;
    glossaryError = "";
    try {
      const payload = await workspaceClient.saveGlossaryEntry(entry, { signal: lifecycleController.signal });
      glossaryEntries = payload.entries;
      glossaryProposals = payload.proposals;
      glossarySuggestions = payload.suggestions;
      glossaryLoaded = true;
      qaPayload = null;
      return true;
    } catch (caught) {
      if (!isAbortError(caught)) {
        if (["glossary-active-conflict", "glossary-proposal-conflict"].includes(caught?.payload?.code)) {
          glossaryLoaded = false;
          try {
            const payload = await workspaceClient.loadGlossary({ signal: lifecycleController.signal });
            glossaryEntries = payload.entries;
            glossaryProposals = payload.proposals;
            glossarySuggestions = payload.suggestions;
            glossaryLoaded = true;
            glossaryError = "This term changed elsewhere. The latest version is shown; review it before editing again.";
          } catch (reloadError) {
            glossaryError = isAbortError(reloadError)
              ? ""
              : "This term changed elsewhere, and the latest glossary could not be loaded. Close and reopen this page before editing again.";
          }
        } else {
          glossaryError = caught.message || "Unable to save the glossary entry.";
        }
      }
      return false;
    } finally {
      glossaryBusy = false;
    }
  }

  async function createGlossarySuggestion(entry) {
    if (accessRole !== "reviewer" || glossaryBusy) return false;
    glossaryBusy = true;
    glossaryError = "";
    try {
      await workspaceClient.createGlossarySuggestion(entry, { signal: lifecycleController.signal });
      glossaryLoaded = false;
      return await loadGlossary();
    } catch (caught) {
      if (!isAbortError(caught)) glossaryError = caught.message || "Unable to record the glossary suggestion.";
      return false;
    } finally {
      glossaryBusy = false;
    }
  }

  async function resolveGlossarySuggestion(suggestionId, action, entry) {
    if (accessRole !== "owner" || glossaryBusy) return false;
    glossaryBusy = true;
    glossaryError = "";
    try {
      await workspaceClient.resolveGlossarySuggestion(suggestionId, action, entry, { signal: lifecycleController.signal });
      glossaryLoaded = false;
      qaPayload = null;
      return await loadGlossary();
    } catch (caught) {
      if (!isAbortError(caught)) glossaryError = caught.message || "Unable to resolve the glossary suggestion.";
      return false;
    } finally {
      glossaryBusy = false;
    }
  }

  const polling = createPollingController({
    run: (signal) => refreshOperation(signal),
    // A start response assigns operation before Svelte flushes derived state.
    // Reading operationActive here can therefore skip the very first poll.
    shouldPoll: () => isActiveOperation(operation),
    delay: 3000,
    onError: (caught) => {
      if (!isAbortError(caught)) error = caught.message || "Workspace refresh failed.";
    }
  });

  function changeCandidate(blockId, candidate) {
    if (accessRole === "viewer" || activeOperation() || translationBusy || exportBusy) return;
    drafts = { ...drafts, [blockId]: candidate };
    conflicts = { ...conflicts, [blockId]: null };
  }

  function sentenceEditingChange(blockId, value) {
    const next = new Set(sentenceEditingBlocks);
    if (value) next.add(blockId); else next.delete(blockId);
    sentenceEditingBlocks = next;
    if (!value) sentenceRequests.delete(blockId);
  }

  async function editSentence(block, sentenceEdit, expectedVersion) {
    if (accessRole === "viewer" || activeOperation() || translationBusy || exportBusy || savingBlocks.has(block.id)) {
      return { ok: false, error: "Wait for active work to finish before saving." };
    }
    const fingerprint = JSON.stringify({ sentenceEdit, expectedVersion });
    let request = sentenceRequests.get(block.id);
    if (!request || request.fingerprint !== fingerprint) {
      request = { fingerprint, key: createIdempotencyKey(`translator.sentence.${block.id}`) };
      sentenceRequests.set(block.id, request);
    }
    setSaving(block.id, true);
    try {
      const payload = await workspaceClient.editSentence(block.id, sentenceEdit, expectedVersion, {
        signal: lifecycleController.signal,
        idempotencyKey: request.key
      });
      const saved = validateBlockWorkspaceBlock(payload?.block);
      if (!replaceLoadedBlock(saved)) throw new Error("Unable to display the saved sentence.");
      sentenceRequests.delete(block.id);
      return { ok: true };
    } catch (caught) {
      if (caught?.payload?.latest_block) {
        try { replaceLoadedBlock(validateBlockWorkspaceBlock(caught.payload.latest_block)); } catch { /* Keep the draft if conflict data is invalid. */ }
      }
      const messages = {
        "sentence-edit-protected-text": "Keep the source note markers and fixed text unchanged.",
        "sentence-edit-work-limit": "This edit is too large for sentence editing. Use the formatted block editor.",
        "target-active-operation": "Translator work has started. Your edit is kept; wait for it to finish.",
        "stale_active_version": "This block changed elsewhere. Copy your draft, then cancel and reopen the sentence."
      };
      return { ok: false, error: messages[caught?.code] || caught?.message || "Unable to save the sentence. Your edit is kept." };
    } finally {
      setSaving(block.id, false);
    }
  }

  function clearBlockSelection() {
    selectionMode = false;
    selectedBlockIds = new Set();
    selectionAnchorId = null;
  }

  function toggleBlockSelectionMode() {
    if (selectionActionsDisabled) return;
    if (selectionMode) clearBlockSelection();
    else selectionMode = true;
  }

  function selectBlockForRetranslation(block, event = {}) {
    if (selectionActionsDisabled || !block?.id) return;
    const blockId = String(block.id);
    const orderedIds = blocks.map((item) => String(item.id));
    const additive = Boolean(event.ctrlKey || event.metaKey);
    const range = Boolean(event.shiftKey && selectionAnchorId && orderedIds.includes(selectionAnchorId));
    let next;
    if (range) {
      const anchorIndex = orderedIds.indexOf(selectionAnchorId);
      const blockIndex = orderedIds.indexOf(blockId);
      const [start, end] = anchorIndex <= blockIndex
        ? [anchorIndex, blockIndex]
        : [blockIndex, anchorIndex];
      const rangeIds = orderedIds.slice(start, end + 1);
      next = additive ? new Set(selectedBlockIds) : new Set();
      for (const id of rangeIds) next.add(id);
    } else if (additive) {
      next = new Set(selectedBlockIds);
      if (next.has(blockId)) next.delete(blockId); else next.add(blockId);
      selectionAnchorId = blockId;
    } else {
      next = new Set([blockId]);
      selectionAnchorId = blockId;
    }
    selectedBlockIds = next;
    selectionMode = true;
  }

  async function saveCandidate(block, candidate) {
    if (accessRole === "viewer" || activeOperation() || translationBusy || exportBusy || !candidate || !block?.id) return;
    const blockId = String(block.id);
    const saveId = (saveSequences.get(blockId) || 0) + 1;
    saveSequences = new Map(saveSequences).set(blockId, saveId);
    setSaving(block.id, true);
    try {
      const currentBlock = blocks.find((item) => String(item.id) === blockId) || block;
      const expectedVersion = Number(currentBlock.target?.active_version || 0);
      const payload = await workspaceClient.saveCandidate(block.id, candidate, expectedVersion, { signal: lifecycleController.signal });
      if (saveSequences.get(blockId) !== saveId) return;
      if (!payload?.block) throw new Error("The server returned no updated block projection.");
      const savedBlock = validateBlockWorkspaceBlock(payload.block);
      if (!replaceLoadedBlock(savedBlock)) return;
      const nextDrafts = { ...drafts };
      delete nextDrafts[block.id];
      drafts = nextDrafts;
      const nextConflicts = { ...conflicts };
      delete nextConflicts[block.id];
      conflicts = nextConflicts;
    } catch (caught) {
      if (isAbortError(caught)) return;
      if (saveSequences.get(blockId) !== saveId) return;
      if (caught?.code === "target-active-operation") {
        error = "Translator work has started. Your draft is kept; wait for the operation to finish before saving.";
        void refresh();
      } else if (isConflictError(caught) || caught?.status === 409) {
        let latest = null;
        let conflictError = "The server did not return a valid latest block. Reload the server projection before continuing.";
        try {
          latest = validateBlockWorkspaceBlock(caught?.payload?.latest_block);
          conflictError = "";
        } catch {
          // Keep the local draft blocked when the conflict payload itself is invalid.
        }
        conflicts = { ...conflicts, [block.id]: { block: latest, error: conflictError } };
      } else {
        error = caught.message || "Unable to save the block candidate.";
      }
    } finally {
      if (saveSequences.get(blockId) === saveId) setSaving(block.id, false);
    }
  }

  function reloadConflict(blockId, latest) {
    if (!latest) {
      refresh().then(() => {
        const refreshed = blocks.find((block) => String(block.id) === String(blockId));
        if (refreshed) reloadConflict(blockId, refreshed);
      }).catch(() => {});
      return;
    }
    try {
      replaceLoadedBlock(validateBlockWorkspaceBlock(latest));
    } catch {
      return;
    }
    const nextDrafts = { ...drafts };
    delete nextDrafts[blockId];
    drafts = nextDrafts;
    const nextConflicts = { ...conflicts };
    delete nextConflicts[blockId];
    conflicts = nextConflicts;
  }

  function setNoteBusy(blockId, value) {
    const next = new Set(noteBusy);
    if (value) next.add(blockId); else next.delete(blockId);
    noteBusy = next;
  }

  function setNoteError(blockId, message) {
    const next = { ...noteErrors };
    if (message) next[blockId] = message; else delete next[blockId];
    noteErrors = next;
  }

  async function applyBlockMutation(block, run, fallbackMessage) {
    if (accessRole === "viewer" || !block?.id || noteBusy.has(block.id)) return false;
    const blockId = String(block.id);
    setNoteBusy(blockId, true);
    setNoteError(blockId, "");
    try {
      const payload = await run();
      const current = blocks.find((item) => item.id === blockId);
      if (!current) return Boolean(await refresh());
      let updated;
      if (payload?.note) {
        const note = payload.note;
        const previous = current.reader_notes.find((item) => item.id === note.id);
        if (previous && previous.version > note.version) return true;
        updated = validateBlockWorkspaceBlock({
          ...current,
          reader_notes: previous
            ? current.reader_notes.map((item) => item.id === note.id ? note : item)
            : [...current.reader_notes, note]
        });
      } else if (payload?.block) {
        updated = validateBlockWorkspaceBlock(payload.block);
      } else if (payload?.review) {
        if (current.review.version > payload.review.version) return true;
        updated = validateBlockWorkspaceBlock({ ...current, review: payload.review });
      } else {
        throw new Error("The saved annotation or review response is incomplete.");
      }
      if (!replaceLoadedBlock(updated)) {
        await refresh();
      }
      return true;
    } catch (caught) {
      if (isAbortError(caught)) return false;
      if (caught?.status === 409) {
        await refresh();
        setNoteError(blockId, "This block changed elsewhere. The latest notes and review state are shown; inspect them before trying again.");
      } else {
        setNoteError(blockId, caught.message || fallbackMessage);
      }
      return false;
    } finally {
      setNoteBusy(blockId, false);
    }
  }

  function createReaderNote(block, note) {
    return applyBlockMutation(
      block,
      () => workspaceClient.createReaderNote(block.id, note, { signal: lifecycleController.signal }),
      "Unable to add the reader annotation."
    );
  }

  function reviseReaderNote(block, noteId, note, expectedVersion) {
    return applyBlockMutation(
      block,
      () => workspaceClient.reviseReaderNote(noteId, note, expectedVersion, { signal: lifecycleController.signal }),
      "Unable to revise the reader annotation."
    );
  }

  function transitionReaderNote(block, noteId, state, expectedVersion) {
    return applyBlockMutation(
      block,
      () => workspaceClient.transitionReaderNote(noteId, state, expectedVersion, { signal: lifecycleController.signal }),
      "Unable to change the reader annotation state."
    );
  }

  function saveBlockReview(block, decision) {
    return applyBlockMutation(
      block,
      () => workspaceClient.saveBlockReview(
        block.id,
        decision,
        Number(block?.review?.version || 0),
        Number(block?.target?.active_version || 0),
        null,
        { signal: lifecycleController.signal }
      ),
      "Unable to save the review decision."
    );
  }

  function recordBlockCoverage(block, kind) {
    return applyBlockMutation(
      block,
      () => workspaceClient.recordReviewCoverage(block.id, kind, Number(block?.target?.active_version || 0), { signal: lifecycleController.signal }),
      "Unable to record review coverage."
    );
  }

  function correctionCopy(block, action) {
    const blockNumber = Math.max(1, Number(block?.order) || 1);
    if (action === "merge_previous") {
      return {
        block,
        action,
        title: `Merge block ${blockNumber} with the previous block?`,
        confirmLabel: "Merge blocks",
        destructive: false
      };
    }
    if (action === "merge_next") {
      return {
        block,
        action,
        title: `Merge block ${blockNumber} with the next block?`,
        confirmLabel: "Merge blocks",
        destructive: false
      };
    }
    return {
      block,
      action,
      title: `Exclude block ${blockNumber} from translation?`,
      confirmLabel: "Exclude block",
      destructive: true
    };
  }

  function requestCorrection(block, action) {
    if (structuralActionsDisabled || correctionBusy || !sourcePlan) return;
    pendingCorrection = correctionCopy(block, action);
  }

  function closeCorrection() {
    if (!correctionBusy) pendingCorrection = null;
  }

  function resetWorkspaceProjection() {
    readSequence += 1;
    loadedPages = new Map();
    blocks = [];
    total = 0;
    nextPageOffset = 0;
    hasMore = false;
    drafts = {};
    conflicts = {};
    savingBlocks = new Set();
    glossaryEntries = [];
    glossaryProposals = [];
    glossarySuggestions = [];
    glossaryLoaded = false;
    researchDossiers = [];
    retranslationRequests = [];
    retranslationRequestsLoaded = false;
    noteBusy = new Set();
    noteErrors = {};
    clearBlockSelection();
  }

  async function confirmCorrection() {
    if (!pendingCorrection || correctionBusy || !sourcePlan) return;
    const requested = pendingCorrection;
    correctionBusy = true;
    error = "";
    try {
      const result = await workspaceClient.correctBlock(
        requested.block.id,
        requested.action,
        sourcePlan,
        { signal: lifecycleController.signal }
      );
      sourcePlan = result?.source_plan || sourcePlan;
      pendingCorrection = null;
      resetWorkspaceProjection();
      await loadWorkspace();
    } catch (caught) {
      if (isAbortError(caught)) return;
      const stale = [
        "source-plan-active-conflict",
        "source-plan-correction-conflict"
      ].includes(caught?.payload?.code);
      error = stale
        ? "The document structure changed elsewhere. The latest blocks have been loaded; review them before trying again."
        : caught.message || "Unable to change the semantic block structure.";
      pendingCorrection = null;
      if (stale) {
        resetWorkspaceProjection();
        await refresh();
      }
    } finally {
      correctionBusy = false;
    }
  }

  async function loadMore() {
    if (!hasMore || loadingMore) return;
    loadingMore = true;
    try {
      await loadWorkspace({ append: true });
    } catch (caught) {
      if (!isAbortError(caught)) error = caught.message || "Unable to load more blocks.";
    } finally {
      loadingMore = false;
    }
  }

  async function translateAll(mode = "pending") {
    if (mode === "explicit") {
      if (accessRole === "viewer" || activeOperation() || translationBusy || !localChangesSettled || !selectedBlockIds.size) return;
      retranslationOpen = true;
      retranslationError = "";
      void loadResearchDossiers();
      return;
    }
    if (accessRole !== "owner" || activeOperation() || translationBusy || !localChangesSettled) return;
    translationBusy = true;
    error = "";
    try {
      const payload = await workspaceClient.translateAll({
        signal: lifecycleController.signal,
        mode,
        blockIds: []
      });
      const startedOperation = payload?.operation || payload?.latest_operation;
      if (startedOperation?.id) operation = startedOperation;
      // Start immediately from the response, then refresh the durable
      // projection. This also covers a queue response whose first GET is
      // briefly behind the POST.
      polling.start();
      await refresh();
      polling.start();
    } catch (caught) {
      if (isAbortError(caught)) return;
      error = caught.message || "Unable to start block translation.";
    } finally {
      translationBusy = false;
    }
  }

  async function loadResearchDossiers(errorContext = "retranslation") {
    try {
      const payload = await workspaceClient.loadResearchDossiers({ signal: lifecycleController.signal });
      researchDossiers = Array.isArray(payload?.dossiers) ? payload.dossiers : [];
      if (errorContext === "dossier") dossierError = "";
    } catch (caught) {
      if (!isAbortError(caught)) {
        const message = caught.message || "Unable to load saved research dossiers.";
        if (errorContext === "dossier") dossierError = message;
        else if (errorContext === "requests") retranslationRequestsError = message;
        else retranslationError = message;
      }
    }
  }

  function upsertRetranslationRequest(request) {
    if (!request?.id) return;
    retranslationRequests = [
      request,
      ...retranslationRequests.filter((item) => String(item.id) !== String(request.id))
    ];
  }

  async function loadRetranslationRequests() {
    try {
      const payload = await workspaceClient.listRetranslationRequests({ signal: lifecycleController.signal });
      retranslationRequests = Array.isArray(payload?.requests) ? payload.requests : [];
      retranslationRequestsLoaded = true;
      retranslationRequestsError = "";
      return true;
    } catch (caught) {
      if (!isAbortError(caught)) retranslationRequestsError = caught.message || "Unable to load retranslation requests.";
      return false;
    }
  }

  async function openRetranslationRequests() {
    if (accessRole === "viewer" || retranslationRequestBusyId) return;
    retranslationRequestsOpen = true;
    retranslationRequestsError = "";
    await Promise.all([
      loadRetranslationRequests(),
      accessRole === "owner" ? loadResearchDossiers("requests") : Promise.resolve(true)
    ]);
  }

  async function attachRetranslationDossier(requestRecord, dossierRevisionId) {
    if (accessRole !== "owner" || !requestRecord?.id || retranslationRequestBusyId) return;
    retranslationRequestBusyId = String(requestRecord.id);
    retranslationRequestsError = "";
    try {
      const payload = await workspaceClient.attachRetranslationDossier(
        requestRecord.id,
        dossierRevisionId,
        Number(requestRecord.version),
        { signal: lifecycleController.signal }
      );
      upsertRetranslationRequest(payload?.request);
    } catch (caught) {
      if (isAbortError(caught)) return;
      retranslationRequestsError = caught.message || "Unable to attach the research dossier.";
      if (caught?.status === 409) await loadRetranslationRequests();
    } finally {
      retranslationRequestBusyId = "";
    }
  }

  async function executeSavedRetranslation(requestRecord) {
    if (accessRole !== "owner" || !requestRecord?.id || retranslationRequestBusyId) return;
    retranslationRequestBusyId = String(requestRecord.id);
    retranslationRequestsError = "";
    try {
      const payload = await workspaceClient.executeRetranslationRequest(
        requestRecord.id,
        Number(requestRecord.version),
        { signal: lifecycleController.signal }
      );
      upsertRetranslationRequest(payload?.request);
      const startedOperation = payload?.operation || payload?.latest_operation;
      if (startedOperation?.id) operation = startedOperation;
      polling.start();
      await refresh();
      await loadRetranslationRequests();
      polling.start();
    } catch (caught) {
      if (isAbortError(caught)) return;
      retranslationRequestsError = caught.message || "Unable to start the retranslation request.";
      if (caught?.status === 409) await loadRetranslationRequests();
    } finally {
      retranslationRequestBusyId = "";
    }
  }

  async function openResearchDossiers() {
    if (dossierBusy) return;
    dossierOpen = true;
    dossierBusy = true;
    dossierError = "";
    try {
      await loadResearchDossiers("dossier");
    } finally {
      dossierBusy = false;
    }
  }

  async function inspectResearchDossier(dossier) {
    const dossierId = dossier?.dossier_id || dossier?.id;
    if (!dossierId) throw new Error("The saved dossier has no durable identifier.");
    return workspaceClient.loadResearchDossier(dossierId, {
      revisionId: dossier?.revision_id || null,
      signal: lifecycleController.signal
    });
  }

  async function submitResearchDossier(dossier) {
    if (accessRole !== "owner" || dossierBusy) return;
    dossierBusy = true;
    dossierError = "";
    try {
      const payload = await workspaceClient.submitResearchDossier(dossier, { signal: lifecycleController.signal });
      if (!payload?.dossier?.revision_id) {
        throw new Error("The research dossier was recorded without a durable revision.");
      }
      researchDossiers = [
        payload.dossier,
        ...researchDossiers.filter((item) => item.revision_id !== payload.dossier.revision_id)
      ];
      glossaryLoaded = false;
      dossierOpen = false;
      await refresh();
      await loadGlossary();
    } catch (caught) {
      if (!isAbortError(caught)) dossierError = caught.message || "Unable to record the research dossier.";
    } finally {
      dossierBusy = false;
    }
  }

  async function submitRetranslation(requestPayload) {
    if (accessRole === "viewer" || translationBusy || !sourcePlan) return;
    const selected = blocks
      .map((block) => String(block.id))
      .filter((blockId) => selectedBlockIds.has(blockId));
    if (!selected.length) return;
    translationBusy = true;
    retranslationError = "";
    try {
      const created = await workspaceClient.createRetranslationRequest({
        scope: { kind: "blocks", block_ids: selected },
        expected_source_plan_id: sourcePlan.id,
        expected_source_plan_version: sourcePlan.version,
        custom_prompt: requestPayload.custom_prompt,
        annotation_profile: requestPayload.annotation_profile,
        research_choice: requestPayload.research_choice,
        dossier_revision_id: requestPayload.dossier_revision_id,
        rationale: requestPayload.rationale
      }, { signal: lifecycleController.signal });
      const requestId = created?.request?.id;
      if (!requestId) throw new Error("The retranslation request was saved without an identifier.");
      upsertRetranslationRequest(created.request);
      if (requestPayload.execute_now) {
        const started = await workspaceClient.executeRetranslationRequest(
          requestId,
          Number(created.request.version),
          { signal: lifecycleController.signal }
        );
        const startedOperation = started?.operation || started?.latest_operation;
        upsertRetranslationRequest(started?.request);
        if (startedOperation?.id) operation = startedOperation;
        polling.start();
      }
      retranslationOpen = false;
      clearBlockSelection();
      await refresh();
      polling.start();
    } catch (caught) {
      if (!isAbortError(caught)) retranslationError = caught.message || "Unable to record the retranslation request.";
    } finally {
      translationBusy = false;
    }
  }

  async function stopActiveOperation() {
    if (accessRole !== "owner" || !activeOperation() || stopBusy || cancellationRequested) return;
    stopBusy = true;
    error = "";
    try {
      const payload = await workspaceClient.cancelActiveOperation({ signal: lifecycleController.signal });
      if (payload?.operation?.id) operation = payload.operation;
      polling.start();
      await refresh();
      polling.start();
    } catch (caught) {
      if (!isAbortError(caught)) error = caught.message || "Unable to stop the block workflow.";
    } finally {
      stopBusy = false;
    }
  }

  async function openPreview(initialAnchor = "") {
    if (!allBlocksActive || previewLoading) return;
    previewOpen = true;
    previewLoading = true;
    previewError = "";
    previewHtml = "";
    try {
      previewHtml = await preparePreviewDocument(withEpubReviewAnchors(await workspaceClient.preview({ signal: lifecycleController.signal })), { initialAnchor: typeof initialAnchor === "string" ? initialAnchor : "" });
    } catch (caught) {
      if (isAbortError(caught)) return;
      previewError = caught.message || "Unable to generate the semantic document preview.";
    } finally {
      previewLoading = false;
    }
  }

  function closePreview() {
    if (!previewLoading) previewOpen = false;
  }

  function safeJobUrl(value, { download = false } = {}) {
    if (typeof value !== "string" || !value.trim()) return null;
    try {
      const resolved = new URL(value, window.location.origin);
      if (resolved.origin !== window.location.origin) return null;
      if (resolved.search || resolved.hash) return null;
      const prefix = `/addons/translator/api/projects/${encodeURIComponent(projectId)}/jobs/`;
      if (!resolved.pathname.startsWith(prefix)) return null;
      let jobId = resolved.pathname.slice(prefix.length);
      if (download) {
        if (!jobId.endsWith("/download")) return null;
        jobId = jobId.slice(0, -"/download".length);
      }
      if (!jobId || jobId.includes("/")) return null;
      return { url: resolved.toString(), jobId };
    } catch {
      return null;
    }
  }

  const exportMetadataFields = [
    "title", "subtitle", "description", "publisher", "publication_date", "rights",
    "journal_or_series", "volume", "authors", "editors", "translators",
    "other_contributors", "subjects", "identifiers", "cover_upload_path"
  ];

  function metadataValueEqual(left, right) {
    const empty = (value) => value == null || value === "" || (Array.isArray(value) && value.length === 0);
    if (empty(left) && empty(right)) return true;
    return JSON.stringify(left) === JSON.stringify(right);
  }

  function applyExportMetadataForm(metadata) {
    exportTitle = metadata.title || "";
    exportAuthors = (metadata.authors || []).join("\n");
    exportTranslators = (metadata.translators || []).join("\n");
    exportDetails = { ...metadata, subjects: (metadata.subjects || []).join("\n"), editors: (metadata.editors || []).join("\n") };
    exportIdentifiers = (metadata.identifiers || []).map((item) => ({ ...item }));
    exportContributors = (metadata.other_contributors || []).map((item) => ({ ...item }));
    exportCoverPath = metadata.cover_upload_path || "";
  }

  function mergeSavedExportMetadata(metadata) {
    if (!exportMetadataLoaded) {
      applyExportMetadataForm(metadata);
      exportMetadataBaseline = structuredClone(metadata);
      return false;
    }
    const local = currentExportMetadata();
    const merged = {};
    let preservedLocalEdits = false;
    for (const key of exportMetadataFields) {
      const localValue = local[key];
      const baselineValue = exportMetadataBaseline[key];
      const savedValue = metadata[key];
      if (!metadataValueEqual(localValue, baselineValue)) {
        preservedLocalEdits = true;
        if (localValue !== undefined) merged[key] = structuredClone(localValue);
      } else if (savedValue !== undefined) {
        merged[key] = structuredClone(savedValue);
      }
    }
    applyExportMetadataForm(merged);
    exportMetadataBaseline = structuredClone(metadata);
    return preservedLocalEdits;
  }

  async function openExport() {
    if (accessRole !== "owner") return;
    if (!exportStatus && (activeOperation() || !allBlocksVerified)) return;
    exportOpen = true;
    if (exportMetadataLoading || exportBusy) return;
    exportMetadataLoading = true;
    try {
      const [savedResult, modelResult, settingsResult, editionResult] = await Promise.allSettled([
        client.fetchJson(`/addons/translator/api/projects/${encodeURIComponent(projectId)}/export-metadata`, { signal: lifecycleController.signal }),
        client.fetchJson("/api/llm/models?model_type=chat&ready_only=true", { signal: lifecycleController.signal }),
        client.fetchJson(`/addons/translator/api/projects/${encodeURIComponent(projectId)}/settings`, { signal: lifecycleController.signal }),
        workspaceClient.getEpubEdition({ signal: lifecycleController.signal })
      ]);
      if (savedResult.status !== "fulfilled") throw savedResult.reason;
      const saved = savedResult.value;
      exportCoverSource = saved.cover_source || null;
      if (exportCoverSource?.candidate && exportCoverMode === "existing") exportCoverPage = exportCoverSource.candidate.page;
      const metadata = saved.metadata || {};
      const savedVersion = Number.isInteger(saved.version) ? saved.version : 0;
      if (!exportMetadataLoaded || savedVersion !== exportMetadataVersion) {
        const preservedLocalEdits = mergeSavedExportMetadata(metadata);
        if (preservedLocalEdits) {
          exportSuggestionError = false;
          exportSuggestionStatus = "Saved book details changed elsewhere. Refreshed unchanged fields and kept your local edits.";
        }
      }
      exportMetadataVersion = savedVersion;
      exportSuggestionModels = modelResult.status === "fulfilled" ? normaliseModels(modelResult.value) : [];
      const preferredModel = settingsResult.status === "fulfilled" ? settingsResult.value?.translator_model : "";
      exportSuggestionModel = exportSuggestionModels.some((model) => model.id === preferredModel) ? preferredModel : "";
      exportMetadataLoaded = true;
      if (editionResult.status === "fulfilled") {
        const edition = requireEpubEdition(editionResult.value);
        applyEpubEdition(edition, !epubEdition || edition.stale || JSON.stringify(currentEpubRecipe()) === epubEditionBaseline);
      }
    } catch (caught) {
      if (!isAbortError(caught)) {
        exportError = true;
        exportStatus = "Unable to load saved book details. Close this dialog and retry.";
      }
    } finally {
      exportMetadataLoading = false;
    }
  }

  function closeExport() {
    exportOpen = false;
  }

  function currentExportMetadata() {
    const metadata = {};
    if (exportTitle.trim()) metadata.title = exportTitle.trim();
    if (exportAuthors.trim()) metadata.authors = exportAuthors.split("\n").map((name) => name.trim()).filter(Boolean);
    if (exportTranslators.trim()) metadata.translators = exportTranslators.split("\n").map((name) => name.trim()).filter(Boolean);
    else if ("translators" in exportDetails) metadata.translators = [];
    for (const key of ["subtitle", "description", "publisher", "publication_date", "rights", "journal_or_series", "volume"]) {
      if (typeof exportDetails[key] === "string") metadata[key] = exportDetails[key].trim();
    }
    for (const key of ["subjects", "editors"]) {
      if (typeof exportDetails[key] === "string") metadata[key] = exportDetails[key].split("\n").map((value) => value.trim()).filter(Boolean);
    }
    metadata.identifiers = exportIdentifiers.filter((item) => item.value.trim()).map((item) => ({ scheme: item.scheme, value: item.value.trim() }));
    metadata.other_contributors = exportContributors.filter((item) => item.name.trim()).map((item) => ({ role: item.role, name: item.name.trim() }));
    if (exportCoverPath || "cover_upload_path" in exportDetails) metadata.cover_upload_path = exportCoverPath;
    return metadata;
  }

  async function suggestExportMetadata() {
    if (accessRole !== "owner" || activeOperation() || exportBusy || exportSuggestionBusy || !exportMetadataLoaded || !exportSuggestionModel) return;
    exportSuggestionBusy = true;
    exportSuggestionError = false;
    exportSuggestionStatus = "Preparing suggestions from the book…";
    try {
      const result = await client.fetchJson(`/addons/translator/api/projects/${encodeURIComponent(projectId)}/export-metadata/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ metadata: currentExportMetadata(), expected_version: exportMetadataVersion, model_id: exportSuggestionModel }),
        signal: lifecycleController.signal
      });
      const metadata = result.metadata || {};
      if (!exportTitle.trim()) exportTitle = metadata.title || "";
      if (!exportAuthors.trim()) exportAuthors = (metadata.authors || []).join("\n");
      for (const key of ["subtitle", "description"]) {
        if (!exportDetails[key]?.trim() && metadata[key]) exportDetails = { ...exportDetails, [key]: metadata[key] };
      }
      if (!exportDetails.subjects?.trim() && metadata.subjects?.length) exportDetails = { ...exportDetails, subjects: metadata.subjects.join("\n") };
      exportSuggestionStatus = "Suggestions ready. Review the book details before exporting.";
    } catch (caught) {
      if (!isAbortError(caught)) {
        exportSuggestionError = true;
        exportSuggestionStatus = "Suggestions could not be prepared. Your book details are unchanged; you can continue editing or retry.";
      }
    } finally {
      exportSuggestionBusy = false;
    }
  }

  function openGlossary() {
    glossaryOpen = true;
    if (!glossaryBusy) {
      glossaryLoaded = false;
      void loadGlossary();
    }
  }

  function selectedEpubCoverSignature() {
    return JSON.stringify([exportCoverMode, exportCoverMode === "pdf_page" ? [exportCoverPage, exportCoverCrop] : null, exportCoverPath, exportCoverMode === "generated" ? [exportTitle, exportAuthors, exportDetails] : null]);
  }

  function currentEpubCover() {
    return exportCoverMode === "existing" ? {} : { mode: exportCoverMode, ...(exportCoverMode === "pdf_page" ? { page: exportCoverPage, ...(exportCoverCrop ? { crop: exportCoverCrop } : {}) } : {}) };
  }

  function currentEpubRecipe() {
    return { epub_options: epubOptions, epub_cover: currentEpubCover(), epub_image_mode: epubImageMode };
  }

  function applyEpubEdition(edition, applyChoices = true) {
    epubEdition = edition;
    if (applyChoices && edition.effective_recipe) {
      epubOptions = edition.effective_recipe.epub_options;
      epubImageMode = edition.effective_recipe.epub_image_mode;
      exportCoverMode = edition.effective_recipe.epub_cover.mode || "existing";
      exportCoverPage = edition.effective_recipe.epub_cover.page || exportCoverSource?.candidate?.page || 1;
      exportCoverCrop = edition.effective_recipe.epub_cover.crop || null;
      epubPreflight = null; epubQuality = null;
    }
    if (applyChoices) epubEditionBaseline = JSON.stringify(currentEpubRecipe());
  }

  async function loadEpubEdition() {
    epubEditionBusy = true;
    try { applyEpubEdition(requireEpubEdition(await workspaceClient.getEpubEdition({ signal: lifecycleController.signal }))); }
    finally { epubEditionBusy = false; }
  }

  async function saveEpubEdition() {
    if (!epubEdition) return;
    epubEditionBusy = true;
    try {
      const review = await reviewEpub(epubOptions);
      if (!review.edition_revision) throw new Error("Review the current source before saving edition choices.");
      const edition = requireEpubEdition(await workspaceClient.saveEpubEdition({ signal: lifecycleController.signal, recipe: currentEpubRecipe(), expectedVersion: epubEdition.version, expectedSourceRevision: review.edition_revision }));
      applyEpubEdition(edition, false);
      epubEditionBaseline = JSON.stringify(currentEpubRecipe());
    } finally { epubEditionBusy = false; }
  }

  async function clearEpubEdition() {
    if (!epubEdition) return;
    epubEditionBusy = true;
    try { applyEpubEdition(requireEpubEdition(await workspaceClient.clearEpubEdition({ signal: lifecycleController.signal, expectedVersion: epubEdition.version })), false); }
    finally { epubEditionBusy = false; }
  }

  async function reviewEpub(options) {
    const metadata = currentExportMetadata();
    if (exportCoverPath) metadata.cover_upload_path = exportCoverPath;
    const epubCover = currentEpubCover();
    const coverSignature = selectedEpubCoverSignature();
    const requestedImageMode = epubImageMode;
    if (epubPreflight && coverSignature !== reviewedEpubCover) options = resetEpubCoverReview(options, epubPreflight);
    const result = requireEpubPreflight(await workspaceClient.epubPreflight({ signal: lifecycleController.signal, options, metadata, epubCover, epubImageMode: requestedImageMode }));
    if (coverSignature !== selectedEpubCoverSignature()) throw new Error("The cover changed during review. Review the selected cover again.");
    if (requestedImageMode !== epubImageMode) throw new Error("The image format changed during review. Review again before exporting.");
    epubOptions = result.options;
    reviewedEpubCover = coverSignature;
    reviewedEpubImageMode = requestedImageMode;
    epubPreflight = result;
    epubQuality = null;
    return result;
  }

  async function submitExport(event) {
    event.preventDefault();
    if (accessRole !== "owner" || activeOperation() || !allBlocksVerified || exportBusy || epubEditionBusy || exportSuggestionBusy || exportMetadataLoading || !exportMetadataLoaded) return;
    exportBusy = true;
    exportError = false;
    preparedExport?.dispose();
    preparedExport = null;
    exportDownloadUrl = "";
    exportStatus = "Queueing verified semantic export...";
    const startedAt = Date.now();
    try {
      const metadata = currentExportMetadata();
      const coverChoice = exportFormat === "epub" && exportCoverMode !== "existing" ? currentEpubCover() : null;
      if (coverChoice?.mode === "pdf_page" && (!exportCoverSource?.pdf || !Number.isInteger(exportCoverPage) || exportCoverPage < 1 || exportCoverPage > 1000000)) throw new Error("Choose a positive physical page number from the source PDF.");
      if (exportCoverFile && !coverChoice) {
        if (exportCoverFile.size > 10 * 1024 * 1024) throw new Error("Choose a cover smaller than 10 MB.");
        exportStatus = "Uploading cover…";
        const form = new FormData();
        form.append("file", exportCoverFile);
        const uploaded = await client.fetchJson("/api/upload", { method: "POST", body: form, signal: lifecycleController.signal });
        if (!uploaded.file_path) throw new Error("The cover upload returned no file reference.");
        exportCoverPath = uploaded.file_path;
        exportCoverFile = null;
      }
      if (exportCoverPath || "cover_upload_path" in exportDetails) metadata.cover_upload_path = exportCoverPath;
      const epubReview = exportFormat === "epub" ? await reviewEpub(epubOptions) : null;
      const payload = await workspaceClient.exportDocument(exportFormat, { signal: lifecycleController.signal, metadata, startH2OnNewPage, reviewRequirement: exportReviewRequirement, epubCover: coverChoice, epubOptions, epubImageMode: exportFormat === "epub" ? epubImageMode : null, epubReviewHash: epubReview?.review_hash });
      const startedOperation = payload?.operation || payload?.latest_operation;
      if (startedOperation?.id) operation = startedOperation;
      if (!payload?.status_url) {
        throw new Error("The export was recorded, but no job status URL was returned. Refresh the workspace before retrying.");
      }
      const statusTarget = safeJobUrl(payload.status_url);
      if (!statusTarget) {
        throw new Error("The export service returned an invalid job status URL. Refresh the workspace and retry.");
      }
      polling.start();
      const status = await pollUntil({
        signal: lifecycleController.signal,
        maxAttempts: 1200,
        intervalMs: 1000,
        successStates: ["completed"],
        errorStates: ["cancelled", "failed", "error", "partial", "completed_with_errors"],
        load: async (signal) => {
          const current = await workspaceClient.jobStatus(statusTarget.url, { signal });
          const seconds = Math.floor((Date.now() - startedAt) / 1000);
          const elapsed = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
          const state = String(current.status || current.state || "queued").toLowerCase();
          exportStatus = ["queued", "pending"].includes(state)
            ? `Waiting for the local export worker · ${elapsed}`
            : `Building ${exportFormat.toUpperCase()} document · ${elapsed}`;
          return current;
        }
      });
      const downloadTarget = safeJobUrl(status?.result?.download_url || payload.download_url, { download: true });
      epubQuality = status?.result?.quality || status?.result?.artifact?.quality || null;
      if (!downloadTarget || downloadTarget.jobId !== statusTarget.jobId) {
        throw new Error("Export completed without a safe download URL. Refresh the workspace and retry.");
      }
      exportStatus = "Retrieving the prepared document…";
      preparedExport = await prepareDownload(downloadTarget.url, `${workspaceProject.name || "Translation"}.${exportFormat}`, {
        signal: lifecycleController.signal, fetcher: client.authFetch
      });
      exportDownloadUrl = preparedExport.url;
      exportStatus = "Export ready. Select Download prepared document to save it.";
    } catch (caught) {
      if (!isAbortError(caught)) {
        exportError = true;
        exportStatus = caught.message || "Unable to build the semantic export. Check the project status and retry.";
      }
    } finally {
      exportBusy = false;
      if (!lifecycleController.signal.aborted) void refresh();
    }
  }

  function selectExportFormat(value) {
    if (BLOCK_EXPORT_FORMATS.includes(value)) exportFormat = value;
  }

  async function openSettings() {
    if (accessRole !== "owner" || activeOperation() || exportBusy || settingsBusy) return;
    settingsBusy = true;
    isLoadingResearchTools = true;
    settingsError = "";
    researchToolsLoadError = "";
    try {
      const [settingsResult, modelResult, toolsResult] = await Promise.allSettled([
        client.fetchJson(`/addons/translator/api/projects/${encodeURIComponent(projectId)}/settings`, { signal: lifecycleController.signal }),
        client.fetchJson("/api/llm/models?model_type=chat&ready_only=true", { signal: lifecycleController.signal }),
        client.fetchJson("/addons/translator/api/block-research-tools", { signal: lifecycleController.signal })
      ]);
      if (settingsResult.status === "rejected") throw settingsResult.reason;
      if (modelResult.status === "rejected") throw modelResult.reason;
      settingsModels = normaliseModels(modelResult.value);
      if (toolsResult.status === "fulfilled") {
        researchTools = normaliseBlockResearchTools(toolsResult.value);
      } else if (!isAbortError(toolsResult.reason)) {
        researchTools = [];
        researchToolsLoadError = toolsResult.reason?.message || "Unable to load exact research tools.";
      } else {
        throw toolsResult.reason;
      }
      const settings = settingsResult.value || {};
      settingsForm = {
        translator_model: String(settings.translator_model || ""),
        analyzer_model: String(settings.analyzer_model || ""),
        researcher_model: String(settings.researcher_model || ""),
        enable_pre_analysis: Boolean(settings.enable_pre_analysis),
        enable_research: Boolean(settings.enable_research),
        enable_translator_notes: Boolean(settings.enable_translator_notes),
        annotation_profile: normalizeAnnotationProfile(settings.annotation_profile, settings.enable_translator_notes),
        translation_unit_mode: normalizeTranslationUnitMode(settings.translation_unit_mode),
        translation_context_mode: normalizeTranslationContextMode(settings.translation_context_mode),
        research_tool_selections: selectedResearchToolSelections(settings.research_tool_selections),
        translation_batch_input_tokens: Number(settings.translation_batch_input_tokens || DEFAULT_TRANSLATION_BATCH_INPUT_TOKENS),
        translation_batch_max_blocks: Number(settings.translation_batch_max_blocks || DEFAULT_TRANSLATION_BATCH_MAX_BLOCKS),
        custom_prompt: String(settings.custom_prompt || "")
      };
      settingsOpen = true;
    } catch (caught) {
      if (!isAbortError(caught)) {
        settingsError = caught.message || "Unable to load semantic block settings. Please retry.";
        error = settingsError;
      }
    } finally {
      isLoadingResearchTools = false;
      settingsBusy = false;
    }
  }

  function updateSettingsField(field, value) {
    if (!Object.prototype.hasOwnProperty.call(settingsForm, field)) return;
    settingsForm = { ...settingsForm, [field]: value };
  }

  async function saveSettings(event) {
    event.preventDefault();
    if (accessRole !== "owner" || activeOperation() || exportBusy || settingsBusy) return;
    settingsBusy = true;
    settingsError = "";
    try {
      await client.fetchJson(`/addons/translator/api/projects/${encodeURIComponent(projectId)}/settings`, {
        method: "PUT",
        signal: lifecycleController.signal,
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          translator_model: settingsForm.translator_model,
          analyzer_model: settingsForm.analyzer_model,
          researcher_model: settingsForm.researcher_model,
          enable_pre_analysis: Boolean(settingsForm.enable_pre_analysis),
          enable_research: Boolean(settingsForm.enable_research),
          annotation_profile: normalizeAnnotationProfile(settingsForm.annotation_profile, settingsForm.enable_translator_notes),
          enable_translator_notes: annotationProfileUsesModelNotes(settingsForm.annotation_profile),
          translation_unit_mode: normalizeTranslationUnitMode(settingsForm.translation_unit_mode),
          translation_context_mode: normalizeTranslationContextMode(settingsForm.translation_context_mode),
          research_tool_selections: selectedResearchToolSelections(settingsForm.research_tool_selections),
          translation_batch_input_tokens: Number(settingsForm.translation_batch_input_tokens),
          translation_batch_max_blocks: Number(settingsForm.translation_batch_max_blocks),
          custom_prompt: settingsForm.custom_prompt || ""
        })
      });
      settingsOpen = false;
      error = "";
      await refresh();
    } catch (caught) {
      if (!isAbortError(caught)) {
        settingsError = caught.message || "Unable to save semantic block settings. Please retry.";
      }
    } finally {
      settingsBusy = false;
    }
  }

  async function openShareGrants() {
    if (accessRole !== "owner" || shareBusy) return;
    shareOpen = true;
    shareBusy = true;
    shareError = "";
    createdShareLink = "";
    try {
      const payload = await workspaceClient.listShareGrants({ signal: lifecycleController.signal });
      shareGrants = Array.isArray(payload?.grants) ? payload.grants : [];
    } catch (caught) {
      if (!isAbortError(caught)) shareError = caught.message || "Unable to load review links.";
    } finally {
      shareBusy = false;
    }
  }

  async function createShareGrant(grant) {
    if (accessRole !== "owner" || shareBusy) return;
    shareBusy = true;
    shareError = "";
    createdShareLink = "";
    try {
      const payload = await workspaceClient.createShareGrant(grant, { signal: lifecycleController.signal });
      if (!payload?.grant?.id || !payload?.secret || !payload?.review_path) {
        throw new Error("The review link was issued without its one-time secret.");
      }
      shareGrants = [payload.grant, ...shareGrants.filter((item) => item.id !== payload.grant.id)];
      const base = new URL(payload.review_path, window.location.origin);
      base.hash = `access=${encodeURIComponent(payload.secret)}`;
      createdShareLink = base.toString();
    } catch (caught) {
      if (!isAbortError(caught)) shareError = caught.message || "Unable to issue the review link.";
    } finally {
      shareBusy = false;
    }
  }

  async function revokeShareGrant(grant) {
    if (accessRole !== "owner" || shareBusy || !grant?.id) return;
    shareBusy = true;
    shareError = "";
    try {
      const payload = await workspaceClient.revokeShareGrant(grant.id, { signal: lifecycleController.signal });
      shareGrants = Array.isArray(payload?.grants)
        ? payload.grants
        : shareGrants.map((item) => item.id === grant.id ? { ...item, status: "revoked" } : item);
      createdShareLink = "";
    } catch (caught) {
      if (!isAbortError(caught)) shareError = caught.message || "Unable to revoke the review link.";
    } finally {
      shareBusy = false;
    }
  }

  async function loadQaDiagnostics() {
    if (qaLoading || qaLoadingMore) return;
    qaLoading = true;
    qaError = "";
    try {
      qaPayload = await workspaceClient.loadQaDiagnostics({
        limit: 200,
        groupBy: qaGrouping,
        groupId: qaGroupId,
        signal: lifecycleController.signal
      });
    } catch (caught) {
      if (!isAbortError(caught)) {
        qaError = caught.message || "Unable to run translation QA.";
      }
    } finally {
      qaLoading = false;
    }
  }

  async function loadMoreQaDiagnostics() {
    const cursor = qaPayload?.pagination?.next_cursor;
    if (!cursor || qaLoading || qaLoadingMore) return;
    qaLoadingMore = true;
    qaError = "";
    try {
      const next = await workspaceClient.loadQaDiagnostics({
        cursor,
        limit: 200,
        groupBy: qaGrouping,
        groupId: qaGroupId,
        signal: lifecycleController.signal
      });
      qaPayload = {
        ...next,
        items: [...(qaPayload?.items || []), ...(next?.items || [])],
        groups: qaGroupId ? next.groups : [...(qaPayload?.groups || []), ...(next?.groups || [])],
        occurrences: [...(qaPayload?.occurrences || []), ...(next?.occurrences || [])],
        source_page_urls: {
          ...(qaPayload?.source_page_urls || {}),
          ...(next?.source_page_urls || {})
        }
      };
    } catch (caught) {
      if (!isAbortError(caught)) {
        qaError = caught.message || "Unable to load more QA findings.";
      }
    } finally {
      qaLoadingMore = false;
    }
  }

  async function chooseQaGrouping(mode) {
    qaGrouping = mode;
    qaGroupId = null;
    qaPayload = null;
    await loadQaDiagnostics();
  }

  async function inspectQaGroup(id) {
    qaGroupId = id;
    qaPayload = null;
    await loadQaDiagnostics();
  }

  async function openQaDiagnostics() {
    qaOpen = true;
    if (!qaPayload) await loadQaDiagnostics();
  }

  function closeQaDiagnostics() {
    qaOpen = false;
  }

  async function navigateQaFinding(finding) {
    const blockId = String(finding?.block_id || "");
    const order = Number(finding?.order);
    if (!blockId) return;
    qaOpen = false;
    if (!blocks.some((block) => String(block.id) === blockId) && Number.isInteger(order) && order > 0) {
      try {
        const payload = await workspaceClient.load({
          offset: order - 1,
          limit: 1,
          signal: lifecycleController.signal
        });
        replacePage(payload.pagination.offset, payload.blocks);
      } catch (caught) {
        if (!isAbortError(caught)) error = caught.message || "Unable to load the QA block.";
      }
    }
    setTimeout(() => {
      const element = document.querySelector(`[data-block-id="${CSS.escape(blockId)}"]`);
      element?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 0);
  }

  $: operationState = String(operation?.state || workspaceProject?.status || "").toLowerCase();
  $: operationActive = ACTIVE_OPERATION_STATES.has(operationState);
  $: cancellationRequested = ["cancel_requested", "cancelling"].includes(operationState);
  $: ownerAccess = accessRole === "owner";
  $: reviewerAccess = accessRole === "reviewer";
  $: viewerAccess = accessRole === "viewer";
  $: settingsAllowed = ownerAccess && !operationActive && !exportBusy && !settingsBusy;
  $: localChangesSettled = Object.keys(drafts).length === 0 && sentenceEditingBlocks.size === 0 && savingBlocks.size === 0;
  $: structuralActionsDisabled = !ownerAccess
    || operationActive
    || exportBusy
    || settingsBusy
    || !localChangesSettled;
  $: selectionActionsDisabled = viewerAccess
    || operationActive
    || exportBusy
    || settingsBusy
    || !localChangesSettled
    || translationBusy;
  $: allBlocksActive = Boolean(targetSummary.document_complete) && localChangesSettled;
  $: allBlocksVerified = Boolean(targetSummary.document_complete) && localChangesSettled;
  onMount(async () => {
    if (workspaceProject.engine_contract !== DOCUMENT_BLOCK_ENGINE) {
      error = "This workspace is not a document-block project.";
      loading = false;
      return;
    }
    await refresh();
    polling.start();
  });

  onDestroy(() => {
    preparedExport?.dispose();
    polling.stop();
    lifecycleController.abort();
  });
</script>

<main class="block-workspace" data-testid="block-workspace" use:installNotePopovers>
  <div class="workspace-toolbar" role="group" aria-label="Document tools">
    <div class="toolbar-context">
      <span class="engine-badge">{reviewMode === "aligned_sentences" ? "Aligned sentence groups" : "Semantic blocks"}</span>
      {#if !ownerAccess}
        <div class="access-badge" title={accessLabel || "Accountless capability access"}>
          <span>{reviewerAccess ? "Reviewer" : "Viewer"}</span>
          <small>{reviewerAccess ? "Can edit & request" : "Read only"}</small>
        </div>
      {/if}
    </div>
    <div class="toolbar-actions">
      <IconButton
        icon="eye"
        label={previewLoading ? "Loading preview…" : "Preview document"}
        disabled={!allBlocksActive || previewLoading}
        title={!allBlocksActive ? "Finish and save every block before previewing." : "Preview document"}
        aria-haspopup="dialog"
        onclick={openPreview}
      />
      {#if !viewerAccess}
        <IconButton
          icon="refresh"
          label={selectionMode ? "Cancel block retranslation selection" : "Select blocks to retranslate"}
          selected={selectionMode}
          aria-pressed={selectionMode}
          disabled={loading || selectionActionsDisabled}
          onclick={toggleBlockSelectionMode}
        />
      {/if}
      {#if ownerAccess && !selectionMode && targetSummary.total_blocks > targetSummary.active_targets}
        <IconButton
          icon="play"
          label={operationActive ? "Translation in progress" : translationBusy ? "Starting translation…" : "Translate remaining blocks"}
          title="Translate remaining blocks using the project's saved model and reasoning preference."
          disabled={loading || selectionActionsDisabled}
          onclick={() => translateAll("pending")}
        />
      {/if}
      <IconButton icon="book" label={glossaryLoaded ? `Glossary (${glossaryEntries.length + glossaryProposals.length + glossarySuggestions.length})` : "Glossary"} disabled={loading} aria-haspopup="dialog" onclick={openGlossary} />
      <IconButton icon="refresh" label={qaLoading ? "Running translation QA…" : qaPayload ? `Translation QA (${qaPayload.summary?.total || 0})` : "Translation QA"} disabled={loading || qaLoading} aria-haspopup="dialog" onclick={openQaDiagnostics} />
      <IconButton icon="folder" label={dossierBusy ? "Loading research…" : "Research dossiers"} disabled={dossierBusy} aria-haspopup="dialog" onclick={openResearchDossiers} />
      {#if ownerAccess}
        <IconButton icon="refresh" label="Synchronize list numbering" disabled={loading || structuralActionsDisabled} aria-haspopup="dialog" onclick={() => (numberingOpen = true)} />
        <IconButton icon="send" label={shareBusy ? "Loading links…" : "Share for review"} disabled={shareBusy} aria-haspopup="dialog" onclick={openShareGrants} />
        <IconButton
          icon="settings"
          label={settingsBusy ? "Loading settings..." : "Settings"}
          disabled={!settingsAllowed}
          title={operationActive || exportBusy ? "Settings cannot change while an operation is active." : "Settings"}
          aria-haspopup="dialog"
          onclick={openSettings}
        />
      {/if}
      {#if !viewerAccess}
        <IconButton
          icon="chat"
          label={retranslationRequestsLoaded ? `Requests (${retranslationRequests.filter((item) => ["requested", "started"].includes(item.state)).length})` : "Requests"}
          disabled={Boolean(retranslationRequestBusyId)}
          aria-haspopup="dialog"
          onclick={openRetranslationRequests}
        />
      {/if}
      <div class="toolbar-export" data-testid="block-export-actions" class:export-running={exportBusy}>
        <IconButton
          icon="download"
          label={exportBusy ? "View export progress" : "Export document"}
          disabled={!ownerAccess || (!exportStatus && (operationActive || !allBlocksVerified || settingsBusy))}
          title={exportStatus ? "View export details" : !ownerAccess ? "Only the project owner can download exports." : operationActive ? "An operation is already active." : !allBlocksVerified ? "Finish and save every block before downloading." : "Download EPUB, DOCX, PDF, or HTML"}
          aria-haspopup="dialog"
          onclick={openExport}
        />
      </div>
    </div>
    {#if selectionMode && !viewerAccess}
      <div class="toolbar-selection">
        <span role="status">{selectedBlockIds.size ? `${selectedBlockIds.size} selected.` : "Choose blocks below."} Ctrl/Cmd-click toggles; Shift-click selects a range.</span>
        <button type="button" class="btn primary" disabled={selectionActionsDisabled || !selectedBlockIds.size} on:click={() => translateAll("explicit")}>
          {ownerAccess ? "Retranslate" : "Request retranslation"}{selectedBlockIds.size ? ` (${selectedBlockIds.size})` : " selected"}
        </button>
      </div>
    {/if}
    {#if exportStatus && !exportOpen}
      <button type="button" class="toolbar-export-status" class:export-error={exportError} on:click={openExport}>
        <span role="status">{exportBusy ? exportStatus : exportError ? "Export failed — view details" : "Download ready"}</span>
      </button>
    {/if}
  </div>

  <header class="workspace-header">
    <div class="workspace-heading">
      {#if ownerAccess}
        <a class="back-link" href="/addons/translator/" on:click={returnToProjects}><span aria-hidden="true">←</span> Translation projects</a>
      {/if}
      <p class="eyebrow">Translation workspace</p>
      <h1>{workspaceProject.name || "Translation Project"}</h1>
      <p class="workspace-description">Review the source beside its active translation, maintain reader annotations and terminology, and keep every correction auditable.</p>
      <p class="direction"><strong>{workspaceProject.source_lang || "Source"}</strong> <span aria-hidden="true">→</span> <strong>{workspaceProject.target_lang || "Target"}</strong></p>
    </div>
  </header>

  <section class="workspace-summary" aria-label="Translation workspace summary">
    <div><span>Source blocks</span><strong>{targetSummary.total_blocks}</strong><small>ordered in this document</small></div>
    <div><span>Translated</span><strong>{targetSummary.active_targets}</strong><small>{operationActive ? "saved; editing resumes when work finishes" : "ready to read or edit"}</small></div>
    <div><span>Remaining</span><strong>{Math.max(0, targetSummary.total_blocks - targetSummary.active_targets)}</strong><small>{operationActive ? "translation in progress" : "awaiting translation"}</small></div>
  </section>

  <div class="workspace-layout">
    <div class="workspace-main">
      {#if error}
        <div class="alert error" role="alert">
          <span>{error}</span>
          <button type="button" class="btn btn-ghost" on:click={() => refresh()}>Retry</button>
        </div>
      {/if}

      <BlockOperationState
        {operation}
        canStop={ownerAccess && operationActive}
        {stopBusy}
        onStop={stopActiveOperation}
      />

      <QADiagnosticsDialog open={qaOpen} loading={qaLoading} loadingMore={qaLoadingMore} error={qaError} payload={qaPayload} grouping={qaGrouping} groupId={qaGroupId} ongrouping={chooseQaGrouping} ongroup={inspectQaGroup} onclose={closeQaDiagnostics} onrefresh={loadQaDiagnostics} onloadmore={loadMoreQaDiagnostics} onnavigate={navigateQaFinding} />

      {#if numberingOpen}
        <NumberingSyncDialog client={workspaceClient} onClose={() => (numberingOpen = false)} onApplied={() => refresh()} />
      {/if}

      <Dialog open={exportOpen} title="Export semantic document" description="Choose a format for the verified translation. Reader annotations are included in every format." size="lg" onclose={closeExport}>
            <form class="export-form" on:submit={submitExport}>
              <fieldset class="export-options" disabled={exportBusy || epubEditionBusy}>
                <legend>Choose export format</legend>
                {#each exportOptions as option}
                  <label class="format-option">
                    <input
                      type="radio"
                      name="block-export-format"
                      value={option.value}
                      checked={exportFormat === option.value}
                      disabled={exportBusy}
                      on:change={(event) => selectExportFormat(event.currentTarget.value)}
                    />
                    <span class="format-copy">
                      <span class="format-heading"><span>{option.label}</span><span class="format-extension">{option.extension}</span></span>
                      <span class="format-description">{option.description}</span>
                    </span>
                  </label>
                {/each}
              </fieldset>
              <fieldset class="export-options" disabled={exportBusy || epubEditionBusy}>
                <legend>Review coverage</legend>
                <label class="export-metadata-field">Required before export
                  <select bind:value={exportReviewRequirement}>
                    <option value="none">Draft — no review requirement</option>
                    <option value="structural">Structure checked in every block</option>
                    <option value="target_reading">Target text read in every block</option>
                    <option value="bilingual">Every block compared with the source</option>
                  </select>
                </label>
                <p class="export-hint">Coverage applies to the current translation revisions. It records what was checked and remains separate from approval.</p>
              </fieldset>
              {#if exportFormat === "docx" || exportFormat === "pdf"}
                <fieldset class="export-options" disabled={exportBusy || epubEditionBusy}>
                  <legend>Page layout</legend>
                  <label class="format-option">
                    <input type="checkbox" bind:checked={startH2OnNewPage} />
                    <span class="format-copy">
                      <span>Start H2 headings on a new page</span>
                      <span class="format-description">Useful when H2 marks chapters or letters.</span>
                    </span>
                  </label>
                </fieldset>
              {/if}
              {#if exportFormat === "epub"}<EpubEditionControls edition={epubEdition} busy={exportBusy || exportMetadataLoading || epubEditionBusy} onload={loadEpubEdition} onsave={saveEpubEdition} onclear={clearEpubEdition} /><EpubImageFormat bind:mode={epubImageMode} busy={exportBusy || exportMetadataLoading || epubEditionBusy} /><EpubReviewPanel bind:options={epubOptions} preflight={epubPreflight} quality={epubQuality} imageMode={epubImageMode} busy={exportBusy || exportMetadataLoading || epubEditionBusy} onreview={reviewEpub} oninspect={(anchor) => { exportOpen = false; void openPreview(anchor); }} />{/if}
              <fieldset class="export-options" disabled={exportBusy || epubEditionBusy || exportSuggestionBusy || exportMetadataLoading}>
                <legend>Book details for this export</legend>
                {#if exportSuggestionModels.length}
                  <label class="export-metadata-field">Model for book details
                    <select bind:value={exportSuggestionModel}>
                      <option value="">Choose a model</option>
                      {#each exportSuggestionModels as model}<option value={model.id}>{model.name} · {model.provider}</option>{/each}
                    </select>
                  </label>
                  <button type="button" class="btn secondary" disabled={!exportSuggestionModel || !exportMetadataLoaded} on:click={suggestExportMetadata}>Suggest missing details</button>
                  <p class="export-hint">Suggests a title, author names, description and subjects. Existing details are kept.</p>
                {:else if exportMetadataLoaded}
                  <p class="export-hint">Configure a chat model to suggest missing book details.</p>
                {/if}
                <label class="export-metadata-field">Title<input bind:value={exportTitle} maxlength="500" placeholder="Use the source title" /></label>
                <label class="export-metadata-field">Subtitle<input bind:value={exportDetails.subtitle} maxlength="500" /></label>
                <label class="export-metadata-field">Authors<textarea bind:value={exportAuthors} rows="2" placeholder="Use the source authors; one name per line to replace them"></textarea></label>
                <label class="export-metadata-field">Translators<textarea bind:value={exportTranslators} rows="2" placeholder="Optional; one name per line"></textarea></label>
                {#if exportFormat === "epub"}
                  <fieldset class="export-cover-options">
                    <legend>Cover</legend>
                    <label><input type="radio" bind:group={exportCoverMode} value="existing" /> Use existing or uploaded cover</label>
                    <label><input type="radio" bind:group={exportCoverMode} value="pdf_page" disabled={!exportCoverSource?.pdf} /> Use a source PDF page</label>
                    <label><input type="radio" bind:group={exportCoverMode} value="generated" /> Generate from this edition’s title and authors</label>
                    {#if exportCoverMode === "pdf_page"}
                      <label class="export-metadata-field">Physical PDF page<input type="number" min="1" max="1000000" step="1" bind:value={exportCoverPage} /></label>
                      <p class="export-hint">Count from the first PDF page, including unnumbered pages. This preserves the source page’s original language.</p>
                      {#if exportCoverSource?.candidate}<p class="export-hint">Suggested page {exportCoverSource.candidate.page}: {exportCoverSource.candidate.evidence}</p>{/if}
                      {#if exportCoverSource?.document_id && Number.isInteger(exportCoverPage) && exportCoverPage > 0}
                        <EpubCoverCrop bind:crop={exportCoverCrop} busy={exportBusy || epubEditionBusy} src={`/addons/doc_processor/api/documents/${encodeURIComponent(exportCoverSource.document_id)}/pdf-pages/${exportCoverPage - 1}/thumbnail?width=400`} />
                      {/if}
                    {:else if exportCoverMode === "generated"}
                      <p class="export-hint">Creates a static cover from the book details above, including translators when supplied.</p>
                    {/if}
                  </fieldset>
                {/if}
                {#if exportFormat !== "epub" || exportCoverMode === "existing"}
                <label class="export-metadata-field">Cover image<input bind:this={exportCoverInput} type="file" accept="image/jpeg,image/png" on:change={(event) => { exportCoverFile = event.currentTarget.files?.[0] || null; }} /></label>
                <p class="export-hint">JPEG or PNG, up to 10 MB. Covers are resized to fit the book. {exportCoverPath || exportCoverFile ? "A replacement cover is selected." : "The original cover is kept unless you choose a replacement."}</p>
                {#if exportCoverPath || exportCoverFile}<button type="button" class="btn secondary" on:click={() => { exportCoverFile = null; if (exportCoverInput) exportCoverInput.value = ""; exportCoverPath = ""; exportDetails = { ...exportDetails, cover_upload_path: "" }; }}>Use original cover</button>{/if}
                {/if}
                <p class="export-hint">Language: {workspaceProject.target_lang}. These details describe the translated edition. The original edition remains recorded separately.</p>
                <details class="more-book-details">
                  <summary>More book details</summary>
                  <label class="export-metadata-field">Description<textarea bind:value={exportDetails.description} rows="3" maxlength="4000"></textarea></label>
                  <label class="export-metadata-field">Subjects / tags<textarea bind:value={exportDetails.subjects} rows="2" placeholder="One subject per line"></textarea></label>
                  <label class="export-metadata-field">Publisher<input bind:value={exportDetails.publisher} maxlength="500" /></label>
                  <label class="export-metadata-field">Publication date<input bind:value={exportDetails.publication_date} maxlength="10" placeholder="YYYY, YYYY-MM, or YYYY-MM-DD" /></label>
                  <label class="export-metadata-field">Rights / license<textarea bind:value={exportDetails.rights} rows="2" maxlength="4000"></textarea></label>
                  <label class="export-metadata-field">Series<input bind:value={exportDetails.journal_or_series} maxlength="500" /></label>
                  <label class="export-metadata-field">Volume<input bind:value={exportDetails.volume} maxlength="100" /></label>
                  <label class="export-metadata-field">Editors<textarea bind:value={exportDetails.editors} rows="2" placeholder="One name per line"></textarea></label>
                  <p class="export-hint">Only enter identifiers assigned to this translated edition.</p>
                  {#each exportIdentifiers as identifier, index}
                    <div class="metadata-row">
                      <label>Identifier type<select bind:value={identifier.scheme}><option>ISBN</option><option>DOI</option><option>URI</option><option>OTHER</option></select></label>
                      <label>Identifier<input bind:value={identifier.value} maxlength="500" /></label>
                      <button type="button" aria-label={`Remove identifier ${index + 1}`} on:click={() => exportIdentifiers = exportIdentifiers.filter((_, itemIndex) => itemIndex !== index)}>Remove</button>
                    </div>
                  {/each}
                  <button type="button" class="btn secondary" disabled={exportIdentifiers.length >= 20} on:click={() => exportIdentifiers = [...exportIdentifiers, { scheme: "ISBN", value: "" }]}>Add identifier</button>
                  {#each exportContributors as contributor, index}
                    <div class="metadata-row">
                      <label>Contributor role<select bind:value={contributor.role}><option value="ill">Illustrator</option><option value="aui">Introduction / foreword</option><option value="aft">Afterword</option><option value="cmm">Commentator</option><option value="ctb">Contributor</option>{#if !["ill", "aui", "aft", "cmm", "ctb"].includes(contributor.role)}<option value={contributor.role}>{contributor.role}</option>{/if}</select></label>
                      <label>Contributor name<input bind:value={contributor.name} maxlength="300" /></label>
                      <button type="button" aria-label={`Remove contributor ${index + 1}`} on:click={() => exportContributors = exportContributors.filter((_, itemIndex) => itemIndex !== index)}>Remove</button>
                    </div>
                  {/each}
                  <button type="button" class="btn secondary" disabled={exportContributors.length >= 50} on:click={() => exportContributors = [...exportContributors, { role: "ill", name: "" }]}>Add contributor</button>
                </details>
              </fieldset>
              {#if exportMetadataLoading}<p role="status">Loading saved book details…</p>{/if}
              {#if exportSuggestionStatus}<p class="export-status" class:export-error={exportSuggestionError} role={exportSuggestionError ? "alert" : "status"}>{exportSuggestionStatus}</p>{/if}
              {#if exportStatus}<p class="export-status" class:export-error={exportError} role={exportError ? "alert" : "status"}>{exportStatus}</p>{/if}
              {#if exportBusy}<p class="export-hint">You can hide this dialog and keep reading while the document is prepared.</p>{/if}
              {#if exportDownloadUrl}<a class="download-link" href={exportDownloadUrl} download={preparedExport?.filename}>Download prepared document</a>{/if}
              <div class="export-form-actions">
                <button type="button" class="btn secondary" on:click={closeExport}>{exportBusy ? "Hide" : "Close"}</button>
                <button type="submit" class="btn primary" disabled={exportBusy || epubEditionBusy || exportSuggestionBusy || exportMetadataLoading || !exportMetadataLoaded}>{exportBusy ? "Preparing export..." : "Export and download"}</button>
              </div>
            </form>
      </Dialog>

      <Dialog open={glossaryOpen} title="Glossary" description="Preferred translations and usage notes for this document." size="lg" onclose={() => (glossaryOpen = false)}>
        {#if ownerAccess}
          <div class="glossary-navigation" role="group" aria-label="Glossary views">
            <button type="button" aria-pressed={glossaryTab === "book"} on:click={() => glossaryTab = "book"}>This book</button>
            <button type="button" aria-pressed={glossaryTab === "shared"} on:click={() => glossaryTab = "shared"}>Shared glossaries</button>
          </div>
        {/if}
        {#if glossaryOpen && ownerAccess && glossaryTab === "shared"}
          <SharedGlossaryPanel {client} {projectId} {operationActive}
            onApplied={async () => { glossaryLoaded = false; qaPayload = null; await loadGlossary(); }}
            onCheckTranslation={async () => { glossaryOpen = false; qaPayload = null; await openQaDiagnostics(); }}
            onReviewLocked={(conceptId) => { glossaryFocusConcept = conceptId; glossaryTab = "book"; }}
          />
        {:else}
        <BlockGlossaryPanel
          focusConceptId={glossaryFocusConcept}
          entries={glossaryEntries}
          proposals={glossaryProposals}
          suggestions={glossarySuggestions}
          busy={glossaryBusy}
          loaded={glossaryLoaded}
          error={glossaryError}
          readOnly={viewerAccess}
          {accessRole}
          onSave={saveGlossaryEntry}
          onSuggest={createGlossarySuggestion}
          onResolveSuggestion={resolveGlossarySuggestion}
        />
        {/if}
      </Dialog>

      {#if ownerAccess && settingsOpen}
        <BlockWorkspaceSettingsDialog
          open={settingsOpen}
          models={settingsModels}
          {settingsForm}
          executionMode={workspaceProject.settings?.execution_mode || "managed"}
          {settingsBusy}
          {operationActive}
          {exportBusy}
          {settingsError}
          {researchTools}
          {researchToolsLoadError}
          {isLoadingResearchTools}
          onClose={() => (settingsOpen = false)}
          onSubmit={saveSettings}
          onFieldChange={updateSettingsField}
          onReasoningError={(message) => (settingsError = message)}
        />
      {/if}

      <ShareGrantsDialog
        open={shareOpen}
        grants={shareGrants}
        busy={shareBusy}
        error={shareError}
        createdLink={createdShareLink}
        onClose={() => (shareOpen = false)}
        onCreate={createShareGrant}
        onRevoke={revokeShareGrant}
      />

      <ResearchDossierDialog
        open={dossierOpen}
        dossiers={researchDossiers}
        selectedBlockIds={blocks.map((block) => String(block.id)).filter((blockId) => selectedBlockIds.has(blockId))}
        {accessRole}
        busy={dossierBusy}
        error={dossierError}
        onClose={() => !dossierBusy && (dossierOpen = false)}
        onInspect={inspectResearchDossier}
        onSubmit={submitResearchDossier}
      />

      <RetranslationDialog
        open={retranslationOpen}
        selectionCount={selectedBlockIds.size}
        selectedBlockIds={blocks.map((block) => String(block.id)).filter((blockId) => selectedBlockIds.has(blockId))}
        {accessRole}
        dossiers={researchDossiers}
        defaultAnnotationProfile={settingsForm.annotation_profile}
        busy={translationBusy}
        error={retranslationError}
        onClose={() => !translationBusy && (retranslationOpen = false)}
        onSubmit={submitRetranslation}
      />

      <RetranslationRequestsDialog
        open={retranslationRequestsOpen}
        requests={retranslationRequests}
        dossiers={researchDossiers}
        {accessRole}
        busyRequestId={retranslationRequestBusyId}
        error={retranslationRequestsError}
        onClose={() => !retranslationRequestBusyId && (retranslationRequestsOpen = false)}
        onAttach={attachRetranslationDossier}
        onExecute={executeSavedRetranslation}
      />

      <ConfirmationDialog
        open={Boolean(pendingCorrection)}
        title={pendingCorrection?.title || "Confirm block correction"}
        description={pendingCorrection?.action === "exclude"
          ? "The original source remains in the retained plan, but this material will be omitted from the active translation and export. Unaffected translations are carried forward."
          : "The original source remains unchanged. The two adjacent blocks become one translation unit; their current targets must be translated again, while unaffected translations are carried forward."}
        confirmLabel={pendingCorrection?.confirmLabel || "Continue"}
        destructive={Boolean(pendingCorrection?.destructive)}
        busy={correctionBusy}
        onconfirm={confirmCorrection}
        onclose={closeCorrection}
      />

      {#if loading}
        <div class="state-card" role="status">Loading ordered semantic blocks...</div>
      {:else}
        <SemanticPreviewDialog
          {previewOpen}
          {previewLoading}
          {previewHtml}
          {previewError}
          onClosePreview={closePreview}
        />
        <BlockList
          {blocks}
          readOnly={viewerAccess || operationActive || translationBusy || exportBusy}
          {accessRole}
          reviewMode={reviewMode}
          {drafts}
          {savingBlocks}
          {conflicts}
          totalBlocks={targetSummary.total_blocks}
          structuralDisabled={structuralActionsDisabled}
          structuralBusy={correctionBusy}
          {selectionMode}
          {selectedBlockIds}
          selectionDisabled={selectionActionsDisabled}
          onCandidateChange={changeCandidate}
          onSave={saveCandidate}
          onSentenceEdit={editSentence}
          onSentenceEditingChange={sentenceEditingChange}
          onReloadConflict={reloadConflict}
          onCorrect={requestCorrection}
          onSelect={selectBlockForRetranslation}
          {noteBusy}
          {noteErrors}
          onCreateNote={createReaderNote}
          onReviseNote={reviseReaderNote}
          onTransitionNote={transitionReaderNote}
          onReview={saveBlockReview}
          onCoverage={recordBlockCoverage}
        />
        {#if hasMore}
          <button type="button" class="btn btn-ghost load-more" disabled={loadingMore} on:click={loadMore}>{loadingMore ? "Loading..." : `Load more blocks (${blocks.length} of ${total})`}</button>
        {:else if total > 0}
          <p class="pagination-summary">Showing all {total} ordered blocks.</p>
        {/if}
      {/if}
    </div>

  </div>
</main>

<style>
  .glossary-navigation { display:flex; gap:.4rem; margin-bottom:1rem; border-bottom:1px solid var(--color-rule); padding-bottom:.7rem; }
  .glossary-navigation button { padding:.5rem .8rem; font:inherit; font-size:.85rem; color:var(--color-ink); background:var(--color-surface); border:1px solid var(--color-rule); cursor:pointer; }
  .glossary-navigation button[aria-pressed="true"] { background:var(--color-surface-muted); border-color:var(--color-clay); font-weight:650; }
  .glossary-navigation button:focus-visible { outline:2px solid var(--color-clay); outline-offset:2px; }
  .block-workspace { box-sizing:border-box; width:100%; min-width:0; min-height:100%; overflow:visible; padding:clamp(1rem,3vw,2rem); display:grid; gap:1rem; align-content:start; background:var(--color-paper,#f1eee7); }
  .block-workspace > :global(*) { width:min(1500px,100%); margin-inline:auto; }
  .workspace-layout { display:grid; align-items:start; gap:1rem; min-width:0; }
  .workspace-main { display:grid; align-content:start; gap:1rem; min-width:0; }
  .workspace-header { display:flex; justify-content:space-between; align-items:end; gap:2rem; padding:0 0 clamp(1.25rem,3vw,2rem); border-bottom:1px solid var(--color-rule-strong,#968b7d); }
  .workspace-heading { max-width:48rem; }
  .back-link { display:inline-flex; align-items:center; gap:.4rem; margin-bottom:1.25rem; color:var(--color-ink-muted,#675f56); font-size:.76rem; font-weight:650; letter-spacing:.025em; text-decoration:none; }
  .back-link:hover { color:var(--color-ink,#302d29); }
  .eyebrow { margin:0 0 .65rem; color:var(--color-clay,#7c6053); font-size:.66rem; font-weight:800; letter-spacing:.18em; text-transform:uppercase; }
  h1 { margin:0; font-family:var(--font-editorial,Georgia,serif); font-size:clamp(2.05rem,5vw,3.45rem); font-weight:450; letter-spacing:-.035em; line-height:.98; overflow-wrap:anywhere; }
  .workspace-description { max-width:42rem; margin:1rem 0 0; color:var(--color-ink-muted,#675f56); font-size:clamp(.88rem,1.4vw,1rem); line-height:1.65; }
  .direction { display:flex; align-items:center; gap:.5rem; margin:.8rem 0 0; color:var(--color-ink-muted,#675f56); font-size:.78rem; }
  .direction strong { color:var(--color-ink-soft,#4d4842); font-weight:700; }
  .direction span { padding:0 .25rem; }
  .workspace-toolbar { position:sticky; top:0; z-index:40; box-sizing:border-box; display:flex; align-items:center; flex-wrap:wrap; gap:.35rem .8rem; padding:.35rem .5rem; border-block:1px solid var(--color-rule,#d0c8bc); background:var(--color-paper,#f1eee7); box-shadow:0 4px 8px -6px rgb(0 0 0 / .22); }
  .toolbar-context { display:flex; align-items:center; gap:.65rem; min-width:0; }
  .toolbar-actions { display:flex; flex-wrap:wrap; align-items:center; gap:.2rem; margin-left:auto; }
  .toolbar-selection { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:.5rem; flex-basis:100%; padding:.2rem .35rem; }
  .toolbar-selection span { flex:1 1 15rem; color:var(--color-ink-muted,#675f56); font-size:.73rem; }
  .toolbar-actions :global(.ui-icon-button:focus-visible) { outline:2px solid var(--color-clay,#7c6053); outline-offset:2px; }
  .toolbar-export { position:relative; border-left:1px solid var(--color-rule,#d0c8bc); margin-left:.2rem; padding-left:.3rem; }
  .export-running::after { content:""; position:absolute; top:.3rem; right:.3rem; width:.35rem; height:.35rem; border-radius:50%; background:var(--color-clay,#7c6053); pointer-events:none; }
  .toolbar-export-status { flex-basis:100%; margin:0; padding:.2rem .35rem; border:0; background:transparent; color:var(--color-clay,#7c6053); cursor:pointer; text-align:right; font:inherit; font-size:.73rem; }
  .toolbar-export-status:hover { text-decoration:underline; text-underline-offset:.2em; }
  .access-badge { min-height:2.1rem; display:grid; align-content:center; gap:.04rem; padding:.34rem .58rem; border:1px solid var(--color-rule,#d0c8bc); background:var(--color-surface,#faf8f3); }
  .access-badge span { color:var(--color-clay,#7c6053); font-size:.67rem; font-weight:800; letter-spacing:.08em; text-transform:uppercase; }
  .access-badge small { color:var(--color-ink-muted,#675f56); font-size:.59rem; }
  .engine-badge { color:var(--color-ink-muted,#675f56); font-size:.63rem; font-weight:650; letter-spacing:.07em; text-transform:uppercase; }
  .workspace-summary { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); border-block:1px solid var(--color-rule,#d0c8bc); background:color-mix(in srgb,var(--color-surface,#faf8f3) 58%,transparent); }
  .workspace-summary > div { min-width:0; display:grid; grid-template-columns:auto 1fr; gap:.15rem .65rem; padding:.8rem 1rem; border-right:1px solid var(--color-rule,#d0c8bc); }
  .workspace-summary > div:last-child { border-right:0; }
  .workspace-summary span { grid-column:1/-1; color:var(--color-clay,#7c6053); font-size:.62rem; font-weight:800; letter-spacing:.12em; text-transform:uppercase; }
  .workspace-summary strong { font-family:var(--font-editorial,Georgia,serif); font-size:1.65rem; font-weight:450; line-height:1; }
  .workspace-summary small { align-self:end; color:var(--color-ink-muted,#675f56); font-size:.7rem; line-height:1.25; }
  .alert { display:flex; justify-content:space-between; align-items:center; gap:.75rem; padding:.7rem .8rem; border:1px solid transparent; font-size:.85rem; }
  .alert.error { color:var(--color-danger,#b33); background:var(--color-danger-muted,#fff0f0); border-color:color-mix(in srgb,var(--color-danger,#b33) 35%,var(--color-rule,#ddd)); }
  .btn { min-height:2.1rem; border:1px solid var(--color-rule,#d0c8bc); padding:.48rem .68rem; background:transparent; color:var(--color-ink-soft,#4d4842); cursor:pointer; font:inherit; font-size:.73rem; font-weight:650; letter-spacing:.025em; }
  .btn:disabled { opacity:.55; cursor:not-allowed; }
  .state-card { padding:2rem 1rem; border:1px dashed var(--color-rule,#ddd); border-radius:.55rem; color:var(--color-ink-muted,#667); text-align:center; }
  .export-form { display:grid; gap:.85rem; }
  .export-metadata-field { display:grid; gap:.3rem; font-size:.82rem; color:var(--color-ink-soft,#555); }
  .more-book-details { display:grid; gap:.7rem; }
  .more-book-details > * { margin-top:.7rem; }
  .more-book-details summary { cursor:pointer; font-weight:600; }
  .metadata-row { display:flex; flex-wrap:wrap; align-items:end; gap:.5rem; }
  .metadata-row label { display:grid; gap:.3rem; flex:1; min-width:9rem; font-size:.82rem; }
  .metadata-row input, .metadata-row select { min-width:0; width:100%; padding:.4rem; color:inherit; background:var(--color-surface,#faf8f3); border:1px solid var(--color-rule,#ddd); border-radius:.3rem; }
  .export-metadata-field input, .export-metadata-field textarea, .export-metadata-field select { box-sizing:border-box; width:100%; padding:.55rem; border:1px solid var(--color-rule,#ddd); border-radius:.35rem; color:var(--color-ink,#302d29); background:var(--color-surface,#faf8f3); font:inherit; }
  .export-options { display:grid; gap:.55rem; min-width:0; padding:0; border:0; }
  .export-options legend { margin-bottom:.1rem; color:var(--color-ink-soft,#555); font-size:.82rem; font-weight:600; }
  .format-option { display:grid; grid-template-columns:auto minmax(0,1fr); align-items:start; gap:.65rem; padding:.65rem .7rem; border:1px solid var(--color-rule,#ddd); border-radius:.5rem; background:var(--color-surface,#fff); cursor:pointer; }
  .format-option:hover { border-color:color-mix(in srgb,var(--color-active,#356ae6) 28%,var(--color-rule,#ddd)); background:color-mix(in srgb,var(--color-active-muted,#eef3ff) 55%,var(--color-surface,#fff)); }
  .format-option:focus-within { outline:2px solid color-mix(in srgb,var(--color-active,#356ae6) 25%,transparent); outline-offset:1px; }
  .format-option input { width:1rem; height:1rem; margin:.15rem 0 0; accent-color:var(--color-active,#356ae6); }
  .format-copy { display:grid; gap:.18rem; min-width:0; }
  .format-heading { display:flex; align-items:baseline; gap:.45rem; color:var(--color-ink-soft,#555); font-size:.88rem; font-weight:600; }
  .format-extension { color:var(--color-ink-muted,#667); font-size:.75rem; font-weight:400; }
  .format-description { color:var(--color-ink-muted,#667); font-size:.78rem; line-height:1.4; }
  .export-status { margin:0; padding:.6rem .7rem; border:1px solid var(--color-rule,#ddd); border-radius:.45rem; color:var(--color-ink-muted,#667); font-size:.78rem; line-height:1.4; }
  .export-form-actions { display:flex; justify-content:flex-end; gap:.5rem; }
  .export-error { color:var(--color-danger,#a33); }
  .export-hint { margin:0; color:var(--color-ink-muted,#667); font-size:.78rem; line-height:1.5; }
  .export-cover-options { display:grid; gap:.55rem; margin:0; padding:.75rem; border:1px solid var(--color-rule,#ddd); border-radius:.45rem; }
  .export-cover-options > label { display:flex; align-items:center; gap:.4rem; font-size:.85rem; }
  .download-link { color:var(--color-clay,#7c6053); font-size:.88rem; font-weight:650; text-underline-offset:.2em; }
  .btn.secondary { background:var(--color-surface,#fff); }
  .btn.primary { border-color:var(--color-umber,#5c5149); background:var(--color-umber,#5c5149); color:var(--color-surface,#faf8f3); }
  .load-more { width:fit-content; justify-self:center; background:var(--color-surface,#fff); }
  .pagination-summary { margin:0; color:var(--color-ink-muted,#667); font-size:.8rem; text-align:center; }
  @media (max-width: 820px) { .workspace-summary { grid-template-columns:repeat(3,minmax(0,1fr)); } }
  @media (max-width: 650px) { .workspace-header { align-items:start; flex-direction:column; } .block-workspace { padding:.85rem; } .toolbar-context { display:contents; } .toolbar-context .engine-badge { display:none; } .toolbar-actions { width:100%; justify-content:space-between; gap:0; } .alert { align-items:start; flex-direction:column; } .workspace-summary { grid-template-columns:1fr; } .workspace-summary > div { border-right:0; border-bottom:1px solid var(--color-rule,#d0c8bc); } .workspace-summary > div:last-child { border-bottom:0; } }
</style>
