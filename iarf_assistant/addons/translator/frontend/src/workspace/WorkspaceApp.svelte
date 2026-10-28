<script>
  import { onDestroy, onMount, tick } from "svelte";
  import { preparePreviewDocument } from "../lib/preview-document.js";
  import { Dialog } from "@carlina/ui";
  import { createApiClient, createIdempotencyKey, isAbortError } from "../lib/api.js";
  import { createAbortController, LatestRequestGeneration } from "../lib/request-state.js";
  import { pollUntil } from "../lib/polling.js";
  import { createDraftState, beginDraftSave, failDraftSave, finishDraftSave, mergeDraftState, rebaseDraft, updateDraft } from "../lib/drafts.js";
  import { DEFAULT_SEGMENT_PAGE_SIZE, mergeSegmentPages, pageHasMore, readSegmentPage, resetSegmentPageState } from "../lib/workspace-page.js";
  import { buildTranslationRequest } from "../lib/translator-actions.js";
  import { normaliseResearchProviders, researchProviderOptions, selectedResearchProviderIds } from "../lib/research-providers.js";
  import WorkspaceSegmentEditor from "./WorkspaceSegmentEditor.svelte";
  import WorkspaceHeader from "./WorkspaceHeader.svelte";
  import WorkspaceContextSidebar from "./WorkspaceContextSidebar.svelte";
  import GlossaryQuickEditor from "./GlossaryQuickEditor.svelte";
  import GlossaryTermDialog from "./GlossaryTermDialog.svelte";
  import WorkspaceExportDialog from "./WorkspaceExportDialog.svelte";
  import WorkspaceSettingsDialog from "./WorkspaceSettingsDialog.svelte";
  import WorkspacePreviewDialog from "./WorkspacePreviewDialog.svelte";
  import ConfirmationDialog from "../lib/ConfirmationDialog.svelte";
  import TextInputDialog from "../lib/TextInputDialog.svelte";
  import {
    ACTIVE_PROJECT_STATUSES,
    SETUP_STATUSES,
    buildSegmentBlocks,
    buildSplitParts,
    containsHtmlMarkup,
    escapeHtml,
    formatInteger,
    allowedExportFormats,
    modelSettingsHref,
    modelSetupMessage,
    normaliseModels,
    selectReadyModel
  } from "../lib/utils.js";

  export let context = null;
  export let bootstrapData = null;

  const bootstrap = bootstrapData || window.TRANSLATOR_BOOTSTRAP || {};
  const projectId = bootstrap.projectId;
  const guestMode = Boolean(bootstrap.guestMode);
  const client = createApiClient({
    shareToken: bootstrap.shareToken || "",
    addonApi: context?.api || null,
    signal: context?.signal || null
  });
  const lifecycleController = createAbortController(context?.signal || null);

  let project = {
    id: projectId,
    name: bootstrap.projectName || "Translation Project",
    source_lang: bootstrap.sourceLang || "Source",
    target_lang: bootstrap.targetLang || "Target",
    source_type: bootstrap.sourceType || "document",
    status: ""
  };

  let workspaceError = "";
  let segments = [];
  let segmentBlocks = [];
  let translationDrafts = {};
  let draftState = createDraftState();
  let draftMeta = {};
  let focusedSegmentId = "";
  let segmentSaveTimers = new Map();
  let translatingSegmentIds = new Set();
  let visibleSegmentLimit = DEFAULT_SEGMENT_PAGE_SIZE;
  let totalSegmentCount = 0;
  let hasMoreSegments = false;
  let visibleSegmentBlocks = [];
  let segmentOffset = 0;
  let segmentHasMore = false;
  let loadingMoreSegments = false;
  let serverSegmentTotal = 0;

  let currentTab = "memory";
  let workingMemory = "";
  let memoryFocused = false;
  let glossary = [];
  let ragCollectionIds = [];
  let ragCollections = [];

  let syncStatus = "All changes saved";
  let syncStatusTone = "ok";
  let metrics = { tokens: 0, cost: 0 };
  let sourceExtractionMetrics = "";

  let pollTimer = null;
  let pollController = null;
  const workspaceGeneration = new LatestRequestGeneration();
  const saveControllers = new Map();
  let sidebarWidth = 360;
  let resizingSidebar = false;

  let showPreviewModal = false;
  let previewHtml = "Generating preview...";
  let previewLoading = false;

  let showExportModal = false;
  let exportFormat = "docx";
  let exportBusy = false;
  let exportStatus = "";

  let showSettingsModal = false;
  let settingsBusy = false;
  let models = [];
  let modelLoadError = "";
  let modelSetupError = "";
  let actionBusy = false;
  let pendingConfirmation = null;
  let confirmationBusy = false;
  let splitDialogSegmentId = "";
  let shareUrl = "";

  $: modelsByProvider = models.reduce((acc, model) => {
    const provider = model.provider || "Other";
    if (!acc[provider]) acc[provider] = [];
    acc[provider].push(model);
    return acc;
  }, {});
  let settingsForm = {
    translator_model: "",
    researcher_model: "",
    enable_research: false,
    enable_translator_notes: false,
    research_plugin_ids: [],
    chunk_size_chars: 2500,
    custom_prompt: ""
  };
  let researchProviders = [];
  let researchProvidersLoadError = "";
  let isLoadingResearchProviders = false;

  let inlineGlossary = { source: "", target: "", notes: "" };
  let showGlossaryModal = false;
  let glossaryModalTerm = { id: "", source_term: "", target_term: "", context_notes: "", needs_review: false };
  let glossaryBubble = null;
  let glossaryBubbleBusy = false;
  let glossaryBubbleStatus = "";
  let glossaryBubbleEl;

  const GLOSSARY_BUBBLE_MARGIN = 12;
  const GLOSSARY_BUBBLE_GAP = 10;
  const GLOSSARY_BUBBLE_MAX_WIDTH = 360;

  $: segmentBlocks = buildSegmentBlocks(segments, glossary);
  $: totalSegmentCount = serverSegmentTotal || segmentBlocks.reduce((count, block) => count + block.segments.length, 0);
  $: hasMoreSegments = segmentHasMore || totalSegmentCount > visibleSegmentLimit;
  $: visibleSegmentBlocks = limitSegmentBlocks(segmentBlocks, visibleSegmentLimit);
  $: humanGlossary = glossary.filter((item) => item.is_user_defined);
  $: discoveredGlossary = glossary.filter((item) => !item.is_user_defined);
  $: setupMode = SETUP_STATUSES.has(project.status);
  $: translatingMode = project.status === "translating";
  $: isSubtitleProject = project.source_type === "subtitle_studio";
  $: exportOptions = allowedExportFormats({ guestMode, isSubtitleProject });
  $: if (!exportOptions.some((option) => option.value === exportFormat)) exportFormat = exportOptions[0]?.value || "html";
  $: researchProviderRows = researchProviderOptions(researchProviders, settingsForm.research_plugin_ids);
  $: researchEnabled = Boolean(settingsForm.enable_research && selectedResearchProviderIds(settingsForm.research_plugin_ids).length);

  function setWorkspaceError(message) {
    if (lifecycleController.signal.aborted) return;
    workspaceError = message || "Something went wrong.";
  }

  function autoExpand(node) {
    let frame = null;
    let observedWidth = 0;

    const resize = () => {
      frame = null;
      node.style.height = "auto";
      node.style.height = `${node.scrollHeight}px`;
    };

    const scheduleResize = () => {
      if (frame !== null) cancelAnimationFrame(frame);
      frame = requestAnimationFrame(resize);
    };

    const observer = new ResizeObserver(([entry]) => {
      const nextWidth = Math.round(entry?.contentRect?.width || 0);
      if (nextWidth !== observedWidth) {
        observedWidth = nextWidth;
        scheduleResize();
      }
    });

    node.addEventListener("input", scheduleResize);
    observer.observe(node);
    scheduleResize();

    return {
      update: scheduleResize,
      destroy() {
        if (frame !== null) cancelAnimationFrame(frame);
        observer.disconnect();
        node.removeEventListener("input", scheduleResize);
      }
    };
  }

  function updateSyncStatus(text, tone = "ok") {
    if (lifecycleController.signal.aborted) return;
    syncStatus = text;
    syncStatusTone = tone;
  }

  function clearPollTimer() {
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
    pollController?.abort();
    pollController = null;
  }

  function schedulePolling() {
    clearPollTimer();
    const active = ACTIVE_PROJECT_STATUSES.has(project.status) || segments.some((segment) => segment.status === "ai_translating");
    if (active) {
      pollTimer = setTimeout(() => {
        pollTimer = null;
        pollController = new AbortController();
        refreshWorkspace(pollController.signal).catch(() => {});
      }, 3000);
    }
  }

  function setSourceExtractionMetrics(projectPayload) {
    const metricsPayload = projectPayload?.document?.settings?.metrics || {};
    const parts = [];
    if (metricsPayload.duration_seconds) parts.push(`Clock: ${metricsPayload.duration_seconds}s`);
    if (metricsPayload.cost) parts.push(`Cost: $${Number(metricsPayload.cost).toFixed(3)}`);
    sourceExtractionMetrics = parts.join(" | ");
  }

  function mergeDrafts(nextSegments) {
    draftState = mergeDraftState(draftState, nextSegments);
    const merged = {};
    const meta = {};
    for (const [id, entry] of Object.entries(draftState.entries)) {
      merged[id] = entry.value || "";
      meta[id] = entry;
    }
    translationDrafts = merged;
    draftMeta = meta;
  }

  function limitSegmentBlocks(blocks, limit) {
    if (!Array.isArray(blocks) || limit <= 0) return [];
    let remaining = limit;
    const limited = [];
    for (const block of blocks) {
      if (remaining <= 0) break;
      const blockSegments = Array.isArray(block?.segments) ? block.segments : [];
      if (!blockSegments.length) continue;
      const nextSegments = blockSegments.slice(0, remaining);
      limited.push({ ...block, segments: nextSegments });
      remaining -= nextSegments.length;
    }
    return limited;
  }

  async function loadMoreSegments() {
    if (loadingMoreSegments || !segmentHasMore) {
      visibleSegmentLimit = Math.min(totalSegmentCount, visibleSegmentLimit + DEFAULT_SEGMENT_PAGE_SIZE);
      return;
    }
    loadingMoreSegments = true;
    try {
      await loadWorkspaceState(lifecycleController.signal, { offset: segmentOffset, append: true });
      visibleSegmentLimit = Math.min(totalSegmentCount, visibleSegmentLimit + DEFAULT_SEGMENT_PAGE_SIZE);
    } catch (error) {
      if (!isAbortError(error)) setWorkspaceError(error.message || "Unable to load more segments. Please retry.");
    } finally {
      loadingMoreSegments = false;
    }
  }

  function applyContext(contextPayload) {
    const context = contextPayload || {};
    if (!memoryFocused) {
      workingMemory = context.working_memory || "";
    }
    glossary = Array.isArray(context.glossary) ? context.glossary : [];
    if (glossaryBubble?.id) {
      const refreshed = glossary.find((term) => Number(term.id) === Number(glossaryBubble.id));
      if (refreshed) {
        glossaryBubble = {
          ...glossaryBubble,
          source_term: refreshed.source_term || glossaryBubble.source_term,
          target_term: refreshed.target_term || "",
          context_notes: refreshed.context_notes || "",
          needs_review: Boolean(refreshed.needs_review)
        };
      } else {
        glossaryBubble = null;
      }
    }
    ragCollectionIds = Array.isArray(context.rag_collection_ids) ? context.rag_collection_ids.map(String) : [];
  }

  function resetStructuralPageState() {
    const reset = resetSegmentPageState();
    segmentOffset = reset.offset;
    segmentHasMore = reset.hasMore;
    serverSegmentTotal = reset.total;
    visibleSegmentLimit = reset.visibleLimit;
    segments = reset.segments;
    draftState = createDraftState();
    draftMeta = {};
    translationDrafts = {};
  }

  async function reloadAfterStructuralEdit() {
    resetStructuralPageState();
    await loadWorkspaceState(lifecycleController.signal, { offset: 0, append: false });
  }

  async function loadWorkspaceState(signal = lifecycleController.signal, { offset = 0, append = false } = {}) {
    const generation = workspaceGeneration.next();
    const query = new URLSearchParams({ limit: String(DEFAULT_SEGMENT_PAGE_SIZE), offset: String(offset) });
    const state = await client.fetchJson(`/addons/translator/api/projects/${projectId}/workspace_state?${query}`, { signal });
    if (!workspaceGeneration.isCurrent(generation)) return;
    project = state.project || project;
    setSourceExtractionMetrics(project);
    const page = readSegmentPage(state);
    const preserveLoadedTail = !append && offset === 0 && page.paginated && segments.length > page.segments.length;
    const mergeOffset = append ? offset : (preserveLoadedTail ? page.segments.length : 0);
    const nextSegments = append || preserveLoadedTail
      ? mergeSegmentPages(segments, page.segments, mergeOffset)
      : page.segments;
    segmentOffset = Math.max(segmentOffset, page.offset + page.segments.length);
    serverSegmentTotal = page.paginated ? page.total : nextSegments.length;
    segmentHasMore = pageHasMore(page, nextSegments.length);
    mergeDrafts(nextSegments);
    segments = nextSegments;
    applyContext(state.context);
  }

  async function loadProjectMetrics(signal = lifecycleController.signal) {
    try {
      const payload = await client.fetchJson(`/addons/translator/api/projects/${projectId}/metrics`, { signal });
      metrics = {
        tokens: Number(payload.tokens || 0),
        cost: Number(payload.cost || 0)
      };
    } catch (error) {
      if (isAbortError(error)) return;
      console.warn("Unable to load project metrics", error);
    }
  }

  async function loadKnowledgeCollections() {
    if (guestMode) return;
    try {
      const payload = await client.fetchJson("/knowledge/api/collections", { signal: lifecycleController.signal });
      ragCollections = Array.isArray(payload) ? payload.filter((item) => item.collection_mode === "vector") : [];
    } catch (error) {
      if (isAbortError(error)) return;
      console.warn("Unable to load knowledge collections", error);
      ragCollections = [];
    }
  }

  async function refreshWorkspace(signal = lifecycleController.signal) {
    try {
      await loadWorkspaceState(signal);
      await loadProjectMetrics(signal);
      schedulePolling();
    } catch (error) {
      if (isAbortError(error)) return;
      console.error("Workspace refresh failed", error);
      setWorkspaceError(error.message || "Unable to load workspace state.");
      clearPollTimer();
      pollTimer = setTimeout(() => refreshWorkspace(lifecycleController.signal), 5000);
    }
  }

  async function saveSegment(segmentId, translatedText = undefined, status = "human_approved", snapshot = null) {
    if (guestMode) return null;
    const payload = { status };
    if (translatedText !== undefined) {
      payload.translated_text = translatedText;
    }
    const currentEntry = draftMeta[segmentId] || {};
    if (snapshot?.expectedVersion != null) payload.expected_version = snapshot.expectedVersion;
    else if (currentEntry.version != null) payload.expected_version = currentEntry.version;

    const controller = createAbortController(context?.signal || null);
    const key = String(segmentId);
    // A newer save supersedes the older request. This both reduces wasted
    // work and keeps the controller map a complete unmount-cancellation set.
    saveControllers.get(key)?.abort();
    saveControllers.set(key, controller);

    try {
      const response = await client.fetchJson(`/addons/translator/api/projects/${projectId}/segments/${segmentId}`, {
        method: "PUT",
        signal: controller.signal,
        headers: { "Content-Type": "application/json", "Idempotency-Key": createIdempotencyKey(`translator.segment.${segmentId}`) },
        body: JSON.stringify(payload)
      });

      if (response?.segment?.id) {
        const incoming = response.segment;
        const finished = finishDraftSave(draftState, snapshot || {
          id: String(segmentId),
          value: translatedText ?? "",
          expectedVersion: payload.expected_version,
          generation: draftMeta[segmentId]?.generation || 0,
          saveGeneration: draftMeta[segmentId]?.saveGeneration || 0
        }, incoming);
        // A response from an older save must not even update the server copy
        // held by the UI after a newer save has begun.
        if (finished.state === draftState) return response;
        segments = segments.map((segment) => (segment.id === incoming.id ? incoming : segment));
        draftState = finished.state;
        mergeDrafts([incoming, ...segments.filter((item) => item.id !== incoming.id)]);
        if (finished.applied) updateSyncStatus("All changes saved", "ok");
      }
      return response;
    } finally {
      if (saveControllers.get(key) === controller) saveControllers.delete(key);
    }
  }

  function queueSegmentSave(segmentId) {
    if (guestMode) return;
    updateSyncStatus("Saving changes...", "busy");
    if (segmentSaveTimers.has(segmentId)) {
      clearTimeout(segmentSaveTimers.get(segmentId));
    }

    const timer = setTimeout(async () => {
      const begun = beginDraftSave(draftState, segmentId);
      draftState = begun.state;
      draftMeta = Object.fromEntries(Object.entries(draftState.entries));
      const snapshot = begun.snapshot;
      if (!snapshot) return;
      try {
        await saveSegment(segmentId, snapshot.value, "human_approved", snapshot);
      } catch (error) {
        draftState = failDraftSave(draftState, snapshot, error);
        draftMeta = Object.fromEntries(Object.entries(draftState.entries));
        if (error?.status === 409) updateSyncStatus("Conflict: load the server version or keep the local draft", "error");
        else if (!isAbortError(error)) updateSyncStatus(error.message || "Save failed", "error");
      } finally {
        if (segmentSaveTimers.get(segmentId) === timer) segmentSaveTimers.delete(segmentId);
      }
    }, 900);

    segmentSaveTimers.set(segmentId, timer);
  }

  function handleDraftInput(segmentId, value) {
    if (guestMode) return;
    draftState = updateDraft(draftState, segmentId, value);
    draftMeta = Object.fromEntries(Object.entries(draftState.entries));
    translationDrafts = Object.fromEntries(Object.entries(draftState.entries).map(([id, entry]) => [id, entry.value || ""]));
    queueSegmentSave(segmentId);
  }

  function handleDraftFocus(segmentId) {
    if (guestMode) return;
    focusedSegmentId = String(segmentId);
  }

  function handleDraftBlur(segmentId) {
    if (guestMode) return;
    if (focusedSegmentId === String(segmentId)) focusedSegmentId = "";
    queueSegmentSave(segmentId);
  }

  function reloadSegmentFromServer(segmentId) {
    if (guestMode) return;
    const entry = draftState.entries[String(segmentId)];
    if (!entry?.server) return;
    draftState = {
      ...draftState,
      entries: {
        ...draftState.entries,
        [String(segmentId)]: {
          ...entry,
          value: entry.server.translated_text || "",
          version: entry.server.version ?? entry.version,
          dirty: false,
          inFlight: false,
          conflict: null,
          server: entry.server
        }
      }
    };
    segments = segments.map((segment) => String(segment.id) === String(segmentId) ? entry.server : segment);
    draftMeta = Object.fromEntries(Object.entries(draftState.entries));
    translationDrafts = { ...translationDrafts, [segmentId]: entry.server.translated_text || "" };
    updateSyncStatus("Server version loaded; your local draft was discarded", "ok");
  }

  function rebaseSegmentDraft(segmentId) {
    if (guestMode) return;
    const entry = draftState.entries[String(segmentId)];
    if (!entry?.server) return;
    draftState = rebaseDraft(draftState, segmentId);
    draftMeta = Object.fromEntries(Object.entries(draftState.entries));
    translationDrafts = Object.fromEntries(Object.entries(draftState.entries).map(([id, item]) => [id, item.value || ""]));
    updateSyncStatus("Local draft rebased; saving against the latest version", "busy");
    queueSegmentSave(segmentId);
  }

  async function onSegmentStatus(segmentId, status) {
    if (guestMode) return;
    const begun = beginDraftSave(draftState, segmentId);
    draftState = begun.state;
    draftMeta = Object.fromEntries(Object.entries(draftState.entries));
    try {
      await saveSegment(segmentId, begun.snapshot?.value || translationDrafts[segmentId] || "", status, begun.snapshot);
      updateSyncStatus("Segment updated", "ok");
    } catch (error) {
      draftState = failDraftSave(draftState, begun.snapshot, error);
      draftMeta = Object.fromEntries(Object.entries(draftState.entries));
      updateSyncStatus(error?.status === 409 ? "Conflict: load the server version or keep the local draft" : (error.message || "Update failed"), "error");
    }
  }

  async function pollJobStatus(statusUrl, maxAttempts = 900, intervalMs = 1000, signal = lifecycleController.signal) {
    return pollUntil({
      signal,
      maxAttempts,
      intervalMs,
      load: (nextSignal) => client.fetchJson(statusUrl, { signal: nextSignal })
    });
  }

  function startSegmentTranslationMarker(segmentId) {
    translatingSegmentIds = new Set([...translatingSegmentIds, segmentId]);
  }

  function stopSegmentTranslationMarker(segmentId) {
    const next = new Set(translatingSegmentIds);
    next.delete(segmentId);
    translatingSegmentIds = next;
  }

  async function translateSingleSegment(segmentId) {
    if (guestMode) return;

    const segment = segments.find((item) => String(item.id) === String(segmentId));
    if (!segment) {
      setWorkspaceError("This segment is no longer loaded. Refresh the workspace before translating it.");
      return;
    }
    startSegmentTranslationMarker(segmentId);
    try {
      const translationRequest = buildTranslationRequest(
        segment,
        createIdempotencyKey(`translator.translate.${segmentId}`)
      );
      const payload = await client.fetchJson(`/addons/translator/api/projects/${projectId}/segments/${segmentId}/translate`, {
        method: "POST",
        signal: lifecycleController.signal,
        ...translationRequest
      });
      if (!payload.job_id) throw new Error("No translation job id returned.");
      await pollJobStatus(`/addons/translator/api/jobs/${payload.job_id}`, 900, 1500, lifecycleController.signal);
      await refreshWorkspace(lifecycleController.signal);
    } catch (error) {
      if (!isAbortError(error)) setWorkspaceError(error.message || "Unable to translate segment. Please retry.");
    } finally {
      stopSegmentTranslationMarker(segmentId);
    }
  }

  function mergeSegmentDown(segmentId) {
    if (guestMode) return;
    const segment = segments.find((item) => String(item.id) === String(segmentId));
    if (!segment) return;
    pendingConfirmation = {
      title: "Merge segments?",
      description: "This invalidates the current review state for the affected segments. You can translate them again afterward.",
      confirmLabel: "Merge",
      action: async () => {
        await client.fetchJson(`/addons/translator/api/projects/${projectId}/segments/${segmentId}/merge_down`, {
          method: "POST",
          headers: { "Content-Type": "application/json", "Idempotency-Key": createIdempotencyKey(`translator.merge.${segmentId}`) },
          signal: lifecycleController.signal,
          body: JSON.stringify({ expected_version: segment.version })
        });
        await reloadAfterStructuralEdit();
      }
    };
  }

  async function confirmPendingAction() {
    if (guestMode || !pendingConfirmation || confirmationBusy) return;
    confirmationBusy = true;
    try {
      await pendingConfirmation.action();
      pendingConfirmation = null;
    } catch (error) {
      setWorkspaceError(error.message || "Unable to update segments. Please retry.");
    } finally {
      confirmationBusy = false;
    }
  }

  async function splitSegment(segmentId) {
    if (guestMode) return;
    const segment = segments.find((item) => item.id === segmentId);
    if (!segment) return;
    if (containsHtmlMarkup(segment.original_text)) {
      setWorkspaceError("This segment contains inline formatting and cannot be split safely. Adjust the source structure and re-segment it instead.");
      return;
    }
    splitDialogSegmentId = String(segmentId);
  }

  async function confirmSplit(splitTail) {
    if (guestMode) return;
    const segment = segments.find((item) => String(item.id) === splitDialogSegmentId);
    splitDialogSegmentId = "";
    if (!segment) return;
    const parts = buildSplitParts(segment.original_text, splitTail);
    if (!parts) {
      setWorkspaceError("Could not split segment with the provided text. The first part must end with that exact text.");
      return;
    }
    try {
      await client.fetchJson(`/addons/translator/api/projects/${projectId}/segments/${segment.id}/split`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Idempotency-Key": createIdempotencyKey(`translator.split.${segment.id}`) },
        signal: lifecycleController.signal,
        body: JSON.stringify({ ...parts, expected_version: segment.version })
      });
      await reloadAfterStructuralEdit();
    } catch (error) {
      setWorkspaceError(error.message || "Unable to split segment. Please retry.");
    }
  }

  async function saveMemory() {
    if (guestMode) return;
    try {
      await client.fetchJson(`/addons/translator/api/projects/${projectId}/context`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        signal: lifecycleController.signal,
        body: JSON.stringify({ working_memory: workingMemory })
      });
      updateSyncStatus("Memory saved", "ok");
    } catch (error) {
      updateSyncStatus(error.message || "Memory save failed", "error");
    }
  }

  async function saveKnowledgeSelection() {
    if (guestMode) return;
    try {
      await client.fetchJson(`/addons/translator/api/projects/${projectId}/context`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        signal: lifecycleController.signal,
        body: JSON.stringify({ rag_collection_ids: ragCollectionIds })
      });
      updateSyncStatus("Knowledge selection saved", "ok");
    } catch (error) {
      updateSyncStatus(error.message || "Knowledge save failed", "error");
    }
  }

  async function addInlineGlossaryTerm() {
    if (guestMode) return;
    const sourceTerm = inlineGlossary.source.trim();
    const targetTerm = inlineGlossary.target.trim();
    if (!sourceTerm || !targetTerm) {
      setWorkspaceError("Source and target terms are required.");
      return;
    }

    try {
      await client.fetchJson(`/addons/translator/api/projects/${projectId}/glossary`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: lifecycleController.signal,
        body: JSON.stringify({
          source_term: sourceTerm,
          target_term: targetTerm,
          context_notes: inlineGlossary.notes.trim()
        })
      });
      inlineGlossary = { source: "", target: "", notes: "" };
      await refreshWorkspace(lifecycleController.signal);
    } catch (error) {
      setWorkspaceError(error.message || "Unable to add glossary term. Please retry.");
    }
  }

  function openGlossaryModal(term = null) {
    glossaryModalTerm = {
      id: term?.id || "",
      source_term: term?.source_term || "",
      target_term: term?.target_term || "",
      context_notes: term?.context_notes || "",
      needs_review: Boolean(term?.needs_review)
    };
    showGlossaryModal = true;
  }

  function updateInlineGlossaryField(field, value) {
    if (guestMode) return;
    inlineGlossary = { ...inlineGlossary, [field]: value };
  }

  function updateGlossaryModalField(field, value) {
    if (guestMode) return;
    glossaryModalTerm = { ...glossaryModalTerm, [field]: value };
  }

  function updateKnowledgeSelection(collectionId, checked) {
    if (guestMode) return;
    if (checked) {
      ragCollectionIds = Array.from(new Set([...ragCollectionIds, String(collectionId)]));
    } else {
      ragCollectionIds = ragCollectionIds.filter((id) => id !== String(collectionId));
    }
  }

  async function saveGlossaryTerm(event) {
    if (guestMode) return;
    event?.preventDefault?.();
    const source = glossaryModalTerm.source_term.trim();
    const target = glossaryModalTerm.target_term.trim();
    if (!source || !target) {
      setWorkspaceError("Source and target terms are required.");
      return;
    }

    const id = glossaryModalTerm.id;
    const method = id ? "PUT" : "POST";
    const url = id
      ? `/addons/translator/api/projects/${projectId}/glossary/${id}`
      : `/addons/translator/api/projects/${projectId}/glossary`;

    try {
      await client.fetchJson(url, {
        method,
        headers: { "Content-Type": "application/json" },
        signal: lifecycleController.signal,
        body: JSON.stringify({
          source_term: source,
          target_term: target,
          context_notes: glossaryModalTerm.context_notes.trim(),
          needs_review: glossaryModalTerm.needs_review
        })
      });
      showGlossaryModal = false;
      await refreshWorkspace(lifecycleController.signal);
    } catch (error) {
      setWorkspaceError(error.message || "Unable to save glossary term. Please retry.");
    }
  }

  async function deleteGlossaryTerm(termId) {
    if (guestMode) return;
    const term = glossary.find((item) => String(item.id) === String(termId));
    pendingConfirmation = {
      title: "Delete glossary term?",
      description: `Remove “${term?.source_term || "this term"}” from this project glossary?`,
      confirmLabel: "Delete term",
      action: async () => {
        await client.fetchJson(`/addons/translator/api/projects/${projectId}/glossary/${termId}`, { method: "DELETE", signal: lifecycleController.signal, headers: { "Idempotency-Key": createIdempotencyKey(`translator.glossary.delete.${termId}`) } });
        await refreshWorkspace(lifecycleController.signal);
      }
    };
  }

  function closeGlossaryBubble() {
    glossaryBubble = null;
    glossaryBubbleBusy = false;
    glossaryBubbleStatus = "";
  }

  function computeGlossaryBubblePosition(anchorRect, bubbleWidth, bubbleHeight) {
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;

    const width = Math.max(120, Math.min(bubbleWidth, viewportWidth - GLOSSARY_BUBBLE_MARGIN * 2));
    const height = Math.max(120, Math.min(bubbleHeight, viewportHeight - GLOSSARY_BUBBLE_MARGIN * 2));

    const desiredLeft = anchorRect.left + anchorRect.width / 2 - width / 2;
    const left = Math.max(
      GLOSSARY_BUBBLE_MARGIN,
      Math.min(viewportWidth - width - GLOSSARY_BUBBLE_MARGIN, desiredLeft)
    );

    const spaceAbove = anchorRect.top - GLOSSARY_BUBBLE_MARGIN;
    const spaceBelow = viewportHeight - anchorRect.bottom - GLOSSARY_BUBBLE_MARGIN;
    const shouldOpenAbove = spaceAbove > spaceBelow && spaceAbove >= height + GLOSSARY_BUBBLE_GAP;

    const desiredTop = shouldOpenAbove
      ? anchorRect.top - height - GLOSSARY_BUBBLE_GAP
      : anchorRect.bottom + GLOSSARY_BUBBLE_GAP;
    const top = Math.max(
      GLOSSARY_BUBBLE_MARGIN,
      Math.min(viewportHeight - height - GLOSSARY_BUBBLE_MARGIN, desiredTop)
    );

    return { left, top };
  }

  function clampOpenGlossaryBubbleToViewport() {
    if (!glossaryBubble || !glossaryBubbleEl) return;
    const width = glossaryBubbleEl.offsetWidth || GLOSSARY_BUBBLE_MAX_WIDTH;
    const height = glossaryBubbleEl.offsetHeight || 280;
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;
    const left = Math.max(
      GLOSSARY_BUBBLE_MARGIN,
      Math.min(viewportWidth - width - GLOSSARY_BUBBLE_MARGIN, glossaryBubble.left)
    );
    const top = Math.max(
      GLOSSARY_BUBBLE_MARGIN,
      Math.min(viewportHeight - height - GLOSSARY_BUBBLE_MARGIN, glossaryBubble.top)
    );
    if (left !== glossaryBubble.left || top !== glossaryBubble.top) {
      glossaryBubble = { ...glossaryBubble, left, top };
    }
  }

  async function handleSegmentsPaneClick(event) {
    const target = event.target;
    if (!(target instanceof Element)) return;

    const mark = target.closest(".glossary-highlight");
    if (!mark) return;

    const markId = Number(mark.getAttribute("data-glossary-id"));
    const markSource = mark.getAttribute("data-glossary-source") || mark.textContent || "";
    const fallbackTarget = mark.getAttribute("data-glossary-target") || "";
    const fallbackNotes = mark.getAttribute("data-glossary-notes") || "";

    let matchedTerm = null;
    if (Number.isFinite(markId) && markId > 0) {
      matchedTerm = glossary.find((term) => Number(term.id) === markId) || null;
    }
    if (!matchedTerm) {
      matchedTerm =
        glossary.find((term) => String(term.source_term || "").toLowerCase() === String(markSource).toLowerCase()) || null;
    }

    const anchorRect = mark.getBoundingClientRect();
    const fallbackWidth = Math.min(GLOSSARY_BUBBLE_MAX_WIDTH, window.innerWidth - GLOSSARY_BUBBLE_MARGIN * 2);
    const initialPosition = computeGlossaryBubblePosition(anchorRect, fallbackWidth, 280);

    glossaryBubble = {
      id: matchedTerm?.id || "",
      source_term: matchedTerm?.source_term || markSource,
      target_term: matchedTerm?.target_term || fallbackTarget,
      context_notes: matchedTerm?.context_notes || fallbackNotes,
      needs_review: Boolean(matchedTerm?.needs_review),
      left: initialPosition.left,
      top: initialPosition.top
    };
    glossaryBubbleBusy = false;
    glossaryBubbleStatus = "";

    await tick();
    if (!glossaryBubble) return;
    const measuredWidth = glossaryBubbleEl?.offsetWidth || fallbackWidth;
    const measuredHeight = glossaryBubbleEl?.offsetHeight || 280;
    const measuredPosition = computeGlossaryBubblePosition(anchorRect, measuredWidth, measuredHeight);
    glossaryBubble = { ...glossaryBubble, left: measuredPosition.left, top: measuredPosition.top };
  }

  async function saveGlossaryBubbleTerm() {
    if (guestMode || !glossaryBubble) return;

    const source = glossaryBubble.source_term.trim();
    const target = glossaryBubble.target_term.trim();
    if (!source || !target) {
      glossaryBubbleStatus = "Source and target terms are required.";
      return;
    }
    if (!glossaryBubble.id) {
      glossaryBubbleStatus = "Term id missing. Use full editor to add this term.";
      return;
    }

    glossaryBubbleBusy = true;
    glossaryBubbleStatus = "";
    try {
      await client.fetchJson(`/addons/translator/api/projects/${projectId}/glossary/${glossaryBubble.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        signal: lifecycleController.signal,
        body: JSON.stringify({
          source_term: source,
          target_term: target,
          context_notes: glossaryBubble.context_notes.trim(),
          needs_review: glossaryBubble.needs_review
        })
      });
      await refreshWorkspace(lifecycleController.signal);
      glossaryBubbleStatus = "Saved";
    } catch (error) {
      glossaryBubbleStatus = error.message || "Unable to save term.";
    } finally {
      glossaryBubbleBusy = false;
    }
  }

  function editGlossaryBubbleTerm() {
    if (!glossaryBubble) return;
    openGlossaryModal(glossaryBubble);
    closeGlossaryBubble();
  }

  async function deleteGlossaryBubbleTerm() {
    if (guestMode || !glossaryBubble?.id) return;
    const id = glossaryBubble.id;
    const source = glossaryBubble.source_term;
    pendingConfirmation = {
      title: "Delete glossary term?",
      description: `Remove “${source || "this term"}” from this project glossary?`,
      confirmLabel: "Delete term",
      action: async () => {
        await client.fetchJson(`/addons/translator/api/projects/${projectId}/glossary/${id}`, { method: "DELETE", signal: lifecycleController.signal, headers: { "Idempotency-Key": createIdempotencyKey(`translator.glossary.delete.${id}`) } });
        closeGlossaryBubble();
        await refreshWorkspace(lifecycleController.signal);
      }
    };
  }

  function handleDocumentPointerDown(event) {
    if (!glossaryBubble) return;
    const target = event.target;
    if (!(target instanceof Element)) return;
    if (target.closest(".glossary-bubble")) return;
    if (target.closest(".glossary-highlight")) return;
    closeGlossaryBubble();
  }

  function handleDocumentKeydown(event) {
    const target = event.target;
    if (
      (event.key === "Enter" || event.key === " ") &&
      target instanceof Element &&
      target.classList.contains("glossary-highlight")
    ) {
      event.preventDefault();
      handleSegmentsPaneClick(event);
      return;
    }

    if (event.key === "Escape" && glossaryBubble) {
      closeGlossaryBubble();
    }
  }

  function handleViewportResize() {
    clampOpenGlossaryBubbleToViewport();
  }

  async function uploadGlossaryCsv(event) {
    if (guestMode) return;
    const file = event?.target?.files?.[0];
    if (!file) return;

    try {
      updateSyncStatus("Uploading glossary...", "busy");
      const formData = new FormData();
      formData.append("file", file);
      await client.fetchJson(`/addons/translator/api/projects/${projectId}/glossary/upload`, {
        method: "POST",
        signal: lifecycleController.signal,
        body: formData
      });
      await refreshWorkspace(lifecycleController.signal);
      updateSyncStatus("Glossary uploaded", "ok");
    } catch (error) {
      updateSyncStatus(error.message || "Glossary upload failed", "error");
    } finally {
      if (event?.target) event.target.value = "";
    }
  }

  async function runAnalyzer() {
    if (guestMode || actionBusy || project.status === "analyzing_terminology") return;
    actionBusy = true;
    try {
      await client.fetchJson(`/addons/translator/api/projects/${projectId}/analyze`, { method: "POST", signal: lifecycleController.signal, headers: { "Idempotency-Key": createIdempotencyKey("translator.analyze") } });
      await refreshWorkspace(lifecycleController.signal);
    } catch (error) {
      setWorkspaceError(error.message || "Unable to start terminology analysis. Please retry.");
    } finally {
      actionBusy = false;
    }
  }

  async function toggleTranslateAll() {
    if (guestMode || actionBusy) return;
    actionBusy = true;
    try {
      if (translatingMode) {
        await client.fetchJson(`/addons/translator/api/projects/${projectId}/cancel`, { method: "POST", signal: lifecycleController.signal, headers: { "Idempotency-Key": createIdempotencyKey("translator.cancel") } });
      } else {
        await client.fetchJson(`/addons/translator/api/projects/${projectId}/translate_all`, { method: "POST", signal: lifecycleController.signal, headers: { "Idempotency-Key": createIdempotencyKey("translator.translate") } });
      }
      await refreshWorkspace(lifecycleController.signal);
    } catch (error) {
      setWorkspaceError(error.message || "Unable to update translation worker state. Please retry.");
    } finally {
      actionBusy = false;
    }
  }

  async function shareWorkspace() {
    if (guestMode) return;
    try {
      const payload = await client.fetchJson(`/addons/translator/api/projects/${projectId}/share`, { method: "POST", signal: lifecycleController.signal });
      if (payload.url) {
        shareUrl = payload.url;
      }
    } catch (error) {
      setWorkspaceError(error.message || "Unable to create share link. Please retry.");
    }
  }

  async function openPreview() {
    showPreviewModal = true;
    previewLoading = true;
    previewHtml = "Generating preview...";
    try {
      previewHtml = await preparePreviewDocument(await client.fetchText(`/addons/translator/api/projects/${projectId}/preview`, { signal: lifecycleController.signal }));
    } catch (error) {
      if (isAbortError(error)) return;
      previewHtml = `<p style="padding:1rem;color:#7d1f1f;">${escapeHtml(error.message || "Unable to generate preview.")}</p>`;
    } finally {
      previewLoading = false;
    }
  }

  function openExportDialog() {
    exportFormat = exportOptions[0]?.value || "html";
    exportStatus = "";
    showExportModal = true;
  }

  function updateExportFormat(value) {
    exportFormat = value;
  }

  async function downloadGuestHtml(exportUrl) {
    // Open a tab synchronously while the click gesture is still active, then
    // fill it with an authenticated blob. This keeps the share token out of
    // browser history, referrers, and server access logs.
    const previewWindow = window.open("about:blank", "_blank");
    try {
      const html = await client.fetchText(exportUrl, { signal: lifecycleController.signal });
      const blobUrl = URL.createObjectURL(new Blob([html], { type: "text/html" }));
      if (previewWindow) {
        previewWindow.location.href = blobUrl;
        window.setTimeout(() => URL.revokeObjectURL(blobUrl), 60_000);
      } else {
        const link = document.createElement("a");
        link.href = blobUrl;
        link.download = `${project.name || "translated_document"}.html`;
        link.click();
        window.setTimeout(() => URL.revokeObjectURL(blobUrl), 5_000);
      }
    } catch (error) {
      previewWindow?.close();
      throw error;
    }
  }

  async function submitExport(event) {
    event.preventDefault();
    exportStatus = "";

    const exportUrl = `/addons/translator/api/projects/${projectId}/export?format=${encodeURIComponent(exportFormat)}`;
    if (guestMode || exportFormat === "html") {
      if (guestMode) {
        exportBusy = true;
        exportStatus = "Preparing HTML preview...";
        try {
          await downloadGuestHtml(exportUrl);
          showExportModal = false;
        } catch (error) {
          if (!isAbortError(error)) exportStatus = error.message || "Unable to export HTML preview.";
        } finally {
          exportBusy = false;
        }
      } else {
        window.open(client.tokenizedUrl(exportUrl), "_blank");
        showExportModal = false;
      }
      return;
    }
    if (isSubtitleProject && ["srt", "vtt", "subtitle_txt", "subtitle_json"].includes(exportFormat)) {
      window.location.href = client.tokenizedUrl(exportUrl);
      showExportModal = false;
      return;
    }

    exportBusy = true;
    exportStatus = "Queued export job...";
    try {
      const payload = await client.fetchJson(exportUrl, {
        method: "POST",
        signal: lifecycleController.signal,
        headers: {
          Accept: "application/json",
          "Idempotency-Key": createIdempotencyKey(`translator.export.${exportFormat}`)
        }
      });
      if (!payload.job_id || !payload.status_url) {
        throw new Error("Export service did not return a job id.");
      }
      exportStatus = "Processing export...";
      const status = await pollJobStatus(payload.status_url, 1200, 1000, lifecycleController.signal);
      const downloadUrl = status?.result?.download_url || payload.download_url;
      if (!downloadUrl) throw new Error("Export completed without download URL.");
      exportStatus = "Download ready. Starting download...";
      window.location.href = client.tokenizedUrl(downloadUrl);
      showExportModal = false;
    } catch (error) {
      if (!isAbortError(error)) exportStatus = error.message || "Unable to export.";
    } finally {
      exportBusy = false;
    }
  }

  async function openSettings() {
    if (guestMode) return;
    settingsBusy = true;
    researchProvidersLoadError = "";
    try {
      modelSetupError = "";
      const modelPayload = await client.fetchJson("/api/llm/models?model_type=chat&ready_only=true", { signal: lifecycleController.signal });
      models = normaliseModels(modelPayload);
      modelLoadError = modelSetupMessage(models);
      if (!models.length) {
        modelSetupError = modelLoadError;
        return;
      }

      isLoadingResearchProviders = true;
      const [settingsResult, providerResult] = await Promise.allSettled([
        client.fetchJson(`/addons/translator/api/projects/${projectId}/settings`, { signal: lifecycleController.signal }),
        client.fetchJson("/addons/translator/api/research-providers", { signal: lifecycleController.signal })
      ]);
      if (settingsResult.status === "rejected") throw settingsResult.reason;
      if (providerResult.status === "fulfilled") {
        researchProviders = normaliseResearchProviders(providerResult.value);
      } else if (!isAbortError(providerResult.reason)) {
        researchProviders = [];
        researchProvidersLoadError = providerResult.reason?.message || "Unable to load research provider options.";
      } else {
        throw providerResult.reason;
      }
      const settings = settingsResult.value;
      const savedResearchIds = selectedResearchProviderIds(settings.research_plugin_ids);
      settingsForm = {
        translator_model: selectReadyModel(models, settings.translator_model),
        researcher_model: selectReadyModel(models, settings.researcher_model),
        enable_research: Boolean(settings.enable_research && savedResearchIds.length),
        enable_translator_notes: Boolean(settings.enable_translator_notes),
        research_plugin_ids: savedResearchIds,
        chunk_size_chars: Number(settings.chunk_size_chars || 2500),
        custom_prompt: settings.custom_prompt || ""
      };
      showSettingsModal = true;
    } catch (error) {
      if (!isAbortError(error)) {
        researchProvidersLoadError = error.message || "Unable to load project settings. Please retry.";
        setWorkspaceError(researchProvidersLoadError);
      }
    } finally {
      isLoadingResearchProviders = false;
      settingsBusy = false;
    }
  }

  function toggleResearchProvider(providerId, event) {
    const selected = new Set(selectedResearchProviderIds(settingsForm.research_plugin_ids));
    if (event.currentTarget.checked) selected.add(providerId);
    else selected.delete(providerId);
    settingsForm = { ...settingsForm, research_plugin_ids: [...selected] };
  }

  function updateSettingsFormField(field, value) {
    settingsForm = { ...settingsForm, [field]: value };
  }

  async function saveSettings(event) {
    if (guestMode) return;
    event.preventDefault();
    settingsBusy = true;
    try {
      await client.fetchJson(`/addons/translator/api/projects/${projectId}/settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        signal: lifecycleController.signal,
        body: JSON.stringify({
          ...settingsForm,
          enable_research: researchEnabled,
          research_plugin_ids: selectedResearchProviderIds(settingsForm.research_plugin_ids)
        })
      });
      showSettingsModal = false;
      updateSyncStatus("Project settings saved", "ok");
    } catch (error) {
      setWorkspaceError(error.message || "Unable to save settings. Please retry.");
    } finally {
      settingsBusy = false;
    }
  }

  function renderContextNote(note) {
    const escaped = escapeHtml(note || "");
    return escaped.replace(
      /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
    );
  }

  function onSidebarResizeStart(event) {
    event.preventDefault();
    resizingSidebar = true;
    document.body.style.cursor = "col-resize";

    const onMove = (moveEvent) => {
      const viewport = document.documentElement.clientWidth;
      const desired = viewport - moveEvent.clientX;
      const clamped = Math.max(260, Math.min(viewport - 360, desired));
      sidebarWidth = clamped;
    };

    const onUp = () => {
      resizingSidebar = false;
      document.body.style.cursor = "";
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };

    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }

  onMount(async () => {
    document.addEventListener("pointerdown", handleDocumentPointerDown);
    document.addEventListener("click", handleSegmentsPaneClick);
    document.addEventListener("keydown", handleDocumentKeydown);
    window.addEventListener("resize", handleViewportResize);
    try {
      await loadWorkspaceState(lifecycleController.signal);
      await Promise.all([loadProjectMetrics(lifecycleController.signal), loadKnowledgeCollections()]);
      schedulePolling();
    } catch (error) {
      if (!isAbortError(error)) workspaceError = error.message || "Unable to initialise translator workspace.";
    }
  });

  onDestroy(() => {
    document.removeEventListener("pointerdown", handleDocumentPointerDown);
    document.removeEventListener("click", handleSegmentsPaneClick);
    document.removeEventListener("keydown", handleDocumentKeydown);
    window.removeEventListener("resize", handleViewportResize);
    clearPollTimer();
    lifecycleController.abort();
    workspaceGeneration.invalidate();
    for (const controller of saveControllers.values()) controller.abort();
    saveControllers.clear();
    for (const timer of segmentSaveTimers.values()) {
      clearTimeout(timer);
    }
    segmentSaveTimers = new Map();
  });
</script>

<main class="workspace-page">
  <WorkspaceHeader
    {project}
    {guestMode}
    {sourceExtractionMetrics}
    {metrics}
    {setupMode}
    {actionBusy}
    {translatingMode}
    {settingsBusy}
    {isSubtitleProject}
    {syncStatus}
    {syncStatusTone}
    onRunAnalyzer={runAnalyzer}
    onToggleTranslateAll={toggleTranslateAll}
    onShare={shareWorkspace}
    onSettings={openSettings}
    onPreview={openPreview}
    onExport={openExportDialog}
  />

  {#if workspaceError}
    <div class="workspace-error" role="alert">
      <span>{workspaceError}</span>
      <button class="btn btn-ghost" type="button" on:click={() => (workspaceError = "")}>Dismiss</button>
      <button class="btn btn-ghost" type="button" on:click={() => refreshWorkspace()}>Retry</button>
    </div>
  {/if}

  {#if modelSetupError}
    <div class="workspace-error" role="status">
      <span>{modelSetupError}</span>
      <a class="btn btn-ghost" href={modelSettingsHref()}>Open Model Settings</a>
      <button class="btn btn-ghost" type="button" on:click={() => (modelSetupError = "")}>Dismiss</button>
    </div>
  {/if}

  <div class="workspace-body">
    <section class="segments-pane">
      {#if !visibleSegmentBlocks.length}
        <div class="empty-state">No segments yet. Processing may still be running.</div>
      {:else}
        {#if hasMoreSegments}
          <div class="segment-window-hint">
            Showing {formatInteger(visibleSegmentLimit)} of {formatInteger(totalSegmentCount)} segments.
          </div>
        {/if}
        <WorkspaceSegmentEditor
          blocks={visibleSegmentBlocks}
          drafts={draftState.entries}
          {guestMode}
          translatingIds={translatingSegmentIds}
          {autoExpand}
          {renderContextNote}
          onDraftInput={handleDraftInput}
          onDraftFocus={handleDraftFocus}
          onDraftBlur={handleDraftBlur}
          onMerge={mergeSegmentDown}
          onSplit={splitSegment}
          onTranslate={translateSingleSegment}
          onStatus={onSegmentStatus}
          onResolveConflict={reloadSegmentFromServer}
          onRebaseConflict={rebaseSegmentDraft}
        />
        {#if hasMoreSegments}
          <div class="segment-load-actions">
            <button class="btn" disabled={loadingMoreSegments} on:click={loadMoreSegments}>
              {loadingMoreSegments ? "Loading..." : `Load next ${formatInteger(Math.min(DEFAULT_SEGMENT_PAGE_SIZE, totalSegmentCount - visibleSegmentLimit))}`}
            </button>
          </div>
        {/if}
      {/if}
    </section>

    <button
      class={`resizer ${resizingSidebar ? "active" : ""}`}
      type="button"
      aria-label="Resize sidebar"
      on:mousedown={onSidebarResizeStart}
    ></button>

    <WorkspaceContextSidebar
      {guestMode}
      {currentTab}
      {workingMemory}
      {humanGlossary}
      {discoveredGlossary}
      {ragCollectionIds}
      {ragCollections}
      {inlineGlossary}
      {sidebarWidth}
      onTabChange={(tab) => (currentTab = tab)}
      onMemoryChange={(value) => (workingMemory = value)}
      onMemoryFocus={() => (memoryFocused = true)}
      onMemoryBlur={() => (memoryFocused = false)}
      onSaveMemory={saveMemory}
      onEditGlossary={openGlossaryModal}
      onDeleteGlossary={deleteGlossaryTerm}
      onInlineFieldChange={updateInlineGlossaryField}
      onAddInlineGlossary={addInlineGlossaryTerm}
      onUploadGlossary={uploadGlossaryCsv}
      onKnowledgeSelectionChange={updateKnowledgeSelection}
      onSaveKnowledgeSelection={saveKnowledgeSelection}
    />
  </div>
</main>

<GlossaryQuickEditor
  bubble={glossaryBubble}
  {guestMode}
  busy={glossaryBubbleBusy}
  status={glossaryBubbleStatus}
  bind:bubbleElement={glossaryBubbleEl}
  onClose={closeGlossaryBubble}
  onFieldChange={(field, value) => {
    if (guestMode) return;
    glossaryBubble = { ...glossaryBubble, [field]: value };
    glossaryBubbleStatus = "";
  }}
  onSave={saveGlossaryBubbleTerm}
  onEdit={editGlossaryBubbleTerm}
  onDelete={deleteGlossaryBubbleTerm}
/>

<WorkspaceExportDialog
  open={showExportModal}
  {exportOptions}
  {exportFormat}
  {exportBusy}
  {exportStatus}
  onClose={() => (showExportModal = false)}
  onSubmit={submitExport}
  onFormatChange={updateExportFormat}
/>

<WorkspaceSettingsDialog
  open={showSettingsModal}
  {models}
  {modelsByProvider}
  {settingsForm}
  {settingsBusy}
  {researchProviderRows}
  {researchProvidersLoadError}
  {isLoadingResearchProviders}
  onClose={() => (showSettingsModal = false)}
  onSubmit={saveSettings}
  onFieldChange={updateSettingsFormField}
  onToggleResearchProvider={toggleResearchProvider}
  onReasoningError={setWorkspaceError}
/>

<GlossaryTermDialog
  open={showGlossaryModal}
  term={glossaryModalTerm}
  onClose={() => (showGlossaryModal = false)}
  onSave={saveGlossaryTerm}
  onFieldChange={updateGlossaryModalField}
/>

<WorkspacePreviewDialog
  open={showPreviewModal}
  {previewLoading}
  {previewHtml}
  onClose={() => (showPreviewModal = false)}
/>

<ConfirmationDialog
  open={Boolean(pendingConfirmation)}
  title={pendingConfirmation?.title || "Confirm action"}
  description={pendingConfirmation?.description || ""}
  confirmLabel={pendingConfirmation?.confirmLabel || "Confirm"}
  busy={confirmationBusy}
  onclose={() => !confirmationBusy && (pendingConfirmation = null)}
  onconfirm={confirmPendingAction}
/>

<TextInputDialog
  open={Boolean(splitDialogSegmentId)}
  title="Split segment"
  description="Enter the exact text that should end the first part. The remaining text becomes the second segment."
  label="End of first part"
  placeholder="Paste the exact sentence ending"
  confirmLabel="Split segment"
  onclose={() => (splitDialogSegmentId = "")}
  onconfirm={confirmSplit}
/>

<Dialog open={Boolean(shareUrl)} title="Workspace share link" description="Anyone with this link can view the shared workspace." onclose={() => (shareUrl = "")} size="sm">
  <div class="share-dialog">
    <label for="share-link">Share URL</label>
    <input id="share-link" readonly value={shareUrl} on:focus={(event) => event.currentTarget.select()} />
    <button class="btn primary" type="button" on:click={async () => { await navigator.clipboard?.writeText(shareUrl); updateSyncStatus("Share link copied", "ok"); }}>Copy link</button>
  </div>
</Dialog>

<style>
  .workspace-page {
    height: 100vh;
    display: flex;
    flex-direction: column;
    color: var(--color-ink);
    background:
      radial-gradient(circle at 6% -18%, color-mix(in srgb, var(--color-active) 7%, transparent), transparent 42%),
      var(--color-paper);
  }

  .workspace-body {
    min-height: 0;
    flex: 1;
    display: flex;
    overflow: hidden;
  }

  .segments-pane {
    flex: 1;
    overflow: auto;
    padding: 1rem 1rem 1.1rem;
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .empty-state {
    border: 1px dashed var(--color-rule);
    background: var(--color-surface);
    border-radius: 0.6rem;
    padding: 1rem;
    color: var(--color-ink-muted);
  }

  .segment-window-hint {
    margin: 0;
    font-size: 0.82rem;
    color: var(--color-ink-muted);
  }

  .segment-load-actions {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
    padding-bottom: 0.5rem;
  }

  .resizer {
    width: 7px;
    flex-shrink: 0;
    cursor: col-resize;
    background: var(--color-paper-muted);
    border: none;
    padding: 0;
  }

  .resizer.active,
  .resizer:hover {
    background: color-mix(in srgb, var(--color-active) 30%, var(--color-paper-muted));
  }

  .btn {
    border: 1px solid var(--color-rule);
    background: var(--color-surface);
    color: var(--color-ink);
    padding: 0.46rem 0.76rem;
    cursor: pointer;
    font-size: 0.86rem;
    border-radius: 0.48rem;
  }

  .btn:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }

  .btn.primary {
    border-color: var(--color-active);
    background: var(--color-active);
    color: var(--color-surface);
  }

  .btn.success {
    border-color: var(--color-active);
    background: var(--color-active);
    color: var(--color-surface);
  }

  .btn.warning {
    border-color: var(--color-warning);
    background: var(--color-warning);
    color: var(--color-surface);
  }

  .btn.danger {
    border-color: var(--color-danger);
    background: var(--color-danger);
    color: var(--color-surface);
  }

  .btn.btn-ghost {
    background: transparent;
    color: var(--color-ink-muted);
  }

  .workspace-error {
    margin: 0 1rem;
    border: 1px solid color-mix(in srgb, var(--color-danger) 36%, var(--color-rule));
    background: var(--color-danger-muted);
    color: var(--color-danger);
    padding: 0.58rem 0.72rem;
    font-size: 0.85rem;
    border-radius: 0.55rem;
  }

  @media (max-width: 1100px) {
    .workspace-page {
      height: auto;
      min-height: 100vh;
    }

    .workspace-body {
      flex-direction: column;
    }

    .resizer {
      display: none;
    }

  }

</style>
