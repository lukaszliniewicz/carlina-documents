<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import type {
    FrontendApiClient,
    FrontendBootstrap,
    FrontendKnowledgeCollection,
    FrontendKnowledgeDashboard,
    FrontendKnowledgeItem,
    FrontendKnowledgeItemPage,
    FrontendKnowledgeJobStatus,
    FrontendKnowledgeLogLink,
    FrontendKnowledgeMutationResponse,
    FrontendKnowledgeStreamEvent,
    FrontendKnowledgeXray,
  } from "@carlina/sdk";
  import {
    Badge,
    Button,
    Dialog,
    EmptyState,
    Icon,
    PageHeader,
    SearchField,
  } from "@carlina/ui";

  import {
    collectionMetadataRows,
    filterKnowledgeCollections,
    knowledgeItemDetails,
    knowledgeItemTitle,
    knowledgeJobFailed,
    knowledgeJobFinished,
    knowledgeModeLabel,
    knowledgeSourceLabel,
    parseKnowledgeSearchResult,
    streamKnowledgeEvents,
    type KnowledgeCollectionMode,
    type KnowledgeSearchResult,
  } from "../lib/knowledge.ts";
  import KnowledgeCollectionDialog, {
    type KnowledgeCollectionPayload,
  } from "./knowledge/KnowledgeCollectionDialog.svelte";
  import KnowledgeXrayDialog from "./knowledge/KnowledgeXrayDialog.svelte";

  interface Props {
    api: FrontendApiClient;
    bootstrap: FrontendBootstrap;
    route: string;
    navigate: (
      href: string,
      clientRoute?: boolean,
      options?: { replace?: boolean },
    ) => "client" | "document";
  }

  interface ActivityEntry {
    id: number;
    label: string;
    message: string;
    status: "running" | "success" | "error";
    percent?: number;
    links?: FrontendKnowledgeLogLink[];
  }

  interface Notice {
    tone: "success" | "error" | "info";
    text: string;
  }

  let { api, bootstrap, route, navigate }: Props = $props();

  let dashboard = $state<FrontendKnowledgeDashboard | null>(null);
  let loading = $state(true);
  let loadError = $state("");
  let notice = $state<Notice | null>(null);

  let view = $state<"library" | "ask">("library");
  let query = $state("");
  let modeFilter = $state<KnowledgeCollectionMode>("all");
  let sourceFilter = $state("all");
  let selectedCollectionId = $state<number | null>(null);

  let itemPage = $state<FrontendKnowledgeItemPage | null>(null);
  let itemsLoading = $state(false);
  let itemsError = $state("");
  let itemRequest = 0;

  let collectionDialogOpen = $state(false);
  let editingCollection = $state<FrontendKnowledgeCollection | null>(null);
  let collectionActionPending = $state<number | null>(null);
  let deleteCollectionTarget =
    $state<FrontendKnowledgeCollection | null>(null);
  let collectionConfirmation = $state("");

  let deleteItemTarget = $state<FrontendKnowledgeItem | null>(null);
  let itemDeletePending = $state(false);

  let uploadInput = $state<HTMLInputElement>();
  let uploadFiles = $state<File[]>([]);
  let uploadEngine = $state("auto");
  let uploadOcrStrategy = $state("auto");
  let uploadEvaluate = $state(false);
  let uploadPaginate = $state(false);
  let uploadPending = $state(false);
  let uploadAbort: AbortController | null = null;

  let activities = $state<ActivityEntry[]>([]);
  let activitySequence = 0;
  let streamAbort: AbortController | null = null;

  let xrayOpen = $state(false);
  let xrayItem = $state<FrontendKnowledgeItem | null>(null);
  let xray = $state<FrontendKnowledgeXray | null>(null);
  let xrayLoading = $state(false);
  let xrayError = $state("");
  let xrayRequest = 0;

  let askQuery = $state("");
  let searchCollectionIds = $state<number[]>([]);
  let searchInitialized = false;
  let searching = $state(false);
  let searchError = $state("");
  let searchResults = $state<KnowledgeSearchResult[]>([]);

  const collections = $derived(dashboard?.collections ?? []);
  const summary = $derived(
    dashboard?.summary ?? {
      total: 0,
      vector: 0,
      regular: 0,
      items: 0,
      source_types: {},
    },
  );
  const selectedCollection = $derived(
    collections.find((collection) => collection.id === selectedCollectionId) ??
      null,
  );
  const vectorCollections = $derived(
    collections.filter((collection) => collection.collection_mode === "vector"),
  );
  const availableSources = $derived.by(() =>
    [...new Set(collections.map((collection) => collection.source_type))].sort(),
  );
  const filteredCollections = $derived(
    filterKnowledgeCollections(collections, {
      query,
      mode: modeFilter,
      source: sourceFilter,
    }),
  );

  $effect(() => {
    const current = new URL(route, "https://iarf.local");
    view = current.searchParams.get("view") === "ask" ? "ask" : "library";
    const requestedCollection = Number(current.searchParams.get("collection"));
    if (Number.isInteger(requestedCollection) && requestedCollection > 0) {
      selectedCollectionId = requestedCollection;
    }
  });

  $effect(() => {
    const collectionId = selectedCollection?.id;
    if (!collectionId) {
      itemPage = null;
      return;
    }
    void loadItems(collectionId, 1);
  });

  onMount(() => {
    void refreshDashboard();
  });

  onDestroy(() => {
    streamAbort?.abort();
    uploadAbort?.abort();
    xrayRequest += 1;
  });

  function routeTo(
    nextView: "library" | "ask",
    collectionId: number | null = selectedCollectionId,
    replace = false,
  ): void {
    const url = new URL("/knowledge/", window.location.origin);
    if (nextView === "ask") url.searchParams.set("view", "ask");
    if (collectionId) url.searchParams.set("collection", String(collectionId));
    navigate(`${url.pathname}${url.search}`, true, { replace });
  }

  function selectCollection(collectionId: number): void {
    selectedCollectionId = collectionId;
    routeTo("library", collectionId);
  }

  async function refreshDashboard(preferredId?: number): Promise<void> {
    loading = !dashboard;
    loadError = "";
    try {
      const payload = await api.request<FrontendKnowledgeDashboard>(
        "/api/frontend/knowledge",
      );
      dashboard = payload;
      const knownIds = new Set(payload.collections.map((item) => item.id));
      if (preferredId && knownIds.has(preferredId)) {
        selectedCollectionId = preferredId;
      } else if (
        selectedCollectionId === null ||
        !knownIds.has(selectedCollectionId)
      ) {
        selectedCollectionId = payload.collections[0]?.id ?? null;
      }

      const vectorIds = payload.collections
        .filter((item) => item.collection_mode === "vector")
        .map((item) => item.id);
      if (!searchInitialized) {
        searchCollectionIds = vectorIds;
        searchInitialized = true;
      } else {
        searchCollectionIds = searchCollectionIds.filter((id) =>
          vectorIds.includes(id),
        );
      }

      if (
        !payload.options.engines.some((engine) => engine.id === uploadEngine)
      ) {
        uploadEngine = payload.options.engines[0]?.id ?? "auto";
      }
    } catch (reason) {
      loadError =
        reason instanceof Error
          ? reason.message
          : "The Knowledge desk could not be loaded.";
    } finally {
      loading = false;
    }
  }

  async function loadItems(
    collectionId: number,
    page: number,
  ): Promise<void> {
    const requestId = ++itemRequest;
    itemsLoading = true;
    itemsError = "";
    try {
      const payload = await api.request<FrontendKnowledgeItemPage>(
        `/api/frontend/knowledge/collections/${collectionId}/items?page=${page}&per_page=30`,
      );
      if (requestId === itemRequest && selectedCollectionId === collectionId) {
        itemPage = payload;
      }
    } catch (reason) {
      if (requestId === itemRequest) {
        itemsError =
          reason instanceof Error
            ? reason.message
            : "Collection items could not be loaded.";
      }
    } finally {
      if (requestId === itemRequest) itemsLoading = false;
    }
  }

  function jsonRequest(
    method: string,
    body: unknown,
  ): RequestInit & { signal?: AbortSignal } {
    return {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    };
  }

  async function saveCollection(
    payload: KnowledgeCollectionPayload,
  ): Promise<void> {
    const current = editingCollection;
    const saved = current
      ? await api.request<FrontendKnowledgeCollection>(
          `/api/frontend/knowledge/collections/${current.id}`,
          jsonRequest("PUT", payload),
        )
      : await api.request<FrontendKnowledgeCollection>(
          "/api/frontend/knowledge/collections",
          jsonRequest("POST", payload),
        );
    collectionDialogOpen = false;
    editingCollection = null;
    notice = {
      tone: "success",
      text: current
        ? `${saved.name} was updated.`
        : `${saved.name} is ready for sources.`,
    };
    await refreshDashboard(saved.id);
    routeTo("library", saved.id, true);
  }

  async function deleteCollection(): Promise<void> {
    const target = deleteCollectionTarget;
    if (!target || collectionConfirmation.trim() !== target.name) return;
    collectionActionPending = target.id;
    try {
      const response = await api.request<FrontendKnowledgeMutationResponse>(
        `/api/frontend/knowledge/collections/${target.id}`,
        { method: "DELETE" },
      );
      deleteCollectionTarget = null;
      collectionConfirmation = "";
      notice = {
        tone: "success",
        text: response.message || `${target.name} was deleted.`,
      };
      await refreshDashboard();
      routeTo("library", selectedCollectionId, true);
    } catch (reason) {
      notice = {
        tone: "error",
        text:
          reason instanceof Error
            ? reason.message
            : "The collection could not be deleted.",
      };
    } finally {
      collectionActionPending = null;
    }
  }

  async function deleteItem(): Promise<void> {
    const target = deleteItemTarget;
    if (!target || !selectedCollection) return;
    itemDeletePending = true;
    try {
      const response = await api.request<FrontendKnowledgeMutationResponse>(
        target.item_kind === "canonical"
          ? `/api/frontend/knowledge/sources/${encodeURIComponent(String(target.id))}?collection_id=${selectedCollection.id}`
          : `/api/frontend/knowledge/items/${target.id}`,
        { method: "DELETE" },
      );
      deleteItemTarget = null;
      notice = {
        tone: "success",
        text: response.message || `${knowledgeItemTitle(target)} was removed.`,
      };
      await refreshDashboard(selectedCollection.id);
      await loadItems(selectedCollection.id, itemPage?.current_page ?? 1);
    } catch (reason) {
      notice = {
        tone: "error",
        text:
          reason instanceof Error
            ? reason.message
            : "The source could not be removed.",
      };
    } finally {
      itemDeletePending = false;
    }
  }

  function addActivity(
    label: string,
    message: string,
    status: ActivityEntry["status"] = "running",
  ): number {
    const id = ++activitySequence;
    activities = [
      { id, label, message, status },
      ...activities,
    ].slice(0, 12);
    return id;
  }

  function updateActivity(
    id: number,
    update: Partial<Omit<ActivityEntry, "id">>,
  ): void {
    activities = activities.map((entry) =>
      entry.id === id ? { ...entry, ...update } : entry,
    );
  }

  function applyStreamEvent(
    activityId: number,
    event: FrontendKnowledgeStreamEvent,
  ): void {
    const isError = event.type === "error";
    const isFinished = ["finish", "finished", "complete", "success"].includes(
      event.type.toLocaleLowerCase(),
    );
    updateActivity(activityId, {
      message: event.message,
      status: isError ? "error" : isFinished ? "success" : "running",
      ...(typeof event.percent === "number"
        ? { percent: Math.max(0, Math.min(100, event.percent)) }
        : {}),
    });
  }

  async function runCollectionStream(
    collection: FrontendKnowledgeCollection,
    action: "sync" | "enable-rag",
  ): Promise<void> {
    streamAbort?.abort();
    streamAbort = new AbortController();
    collectionActionPending = collection.id;
    const label = action === "sync" ? "Source sync" : "Semantic indexing";
    const activityId = addActivity(
      label,
      action === "sync"
        ? `Preparing ${collection.name}…`
        : `Preparing ${collection.name} for semantic search…`,
    );
    try {
      let streamError = "";
      await streamKnowledgeEvents(
        `/api/frontend/knowledge/collections/${collection.id}/${action}`,
        { method: action === "sync" ? "GET" : "POST" },
        (event) => {
          applyStreamEvent(activityId, event);
          if (event.type === "error") streamError = event.message;
        },
        streamAbort.signal,
      );
      if (streamError) throw new Error(streamError);
      updateActivity(activityId, { status: "success" });
      await refreshDashboard(collection.id);
      await loadItems(collection.id, 1);
    } catch (reason) {
      if (streamAbort.signal.aborted) return;
      const message =
        reason instanceof Error
          ? reason.message
          : "The collection activity did not complete.";
      updateActivity(activityId, { message, status: "error" });
      notice = { tone: "error", text: message };
    } finally {
      collectionActionPending = null;
    }
  }

  function waitFor(ms: number, signal: AbortSignal): Promise<void> {
    return new Promise((resolve, reject) => {
      const timer = window.setTimeout(resolve, ms);
      signal.addEventListener(
        "abort",
        () => {
          window.clearTimeout(timer);
          reject(new DOMException("Upload cancelled.", "AbortError"));
        },
        { once: true },
      );
    });
  }

  async function pollKnowledgeJob(
    jobId: string,
    activityId: number,
    signal: AbortSignal,
  ): Promise<void> {
    for (let attempt = 0; attempt < 240; attempt += 1) {
      if (signal.aborted) throw new DOMException("Aborted", "AbortError");
      const status = await api.request<FrontendKnowledgeJobStatus>(
        `/api/frontend/knowledge/jobs/${encodeURIComponent(jobId)}`,
        { signal },
      );
      const message =
        status.message ||
        status.error ||
        `Document processing is ${status.status || "running"}.`;
      updateActivity(activityId, {
        message,
        ...(typeof status.progress === "number"
          ? { percent: status.progress }
          : {}),
        ...(status.log_links?.length ? { links: status.log_links } : {}),
      });
      if (knowledgeJobFinished(status.status)) {
        updateActivity(activityId, { status: "success" });
        return;
      }
      if (knowledgeJobFailed(status.status)) {
        throw new Error(status.error || status.message || "Processing failed.");
      }
      await waitFor(1_250, signal);
    }
    throw new Error(
      "Processing is still running. Its latest state remains in Activity.",
    );
  }

  async function uploadSelectedFiles(): Promise<void> {
    const collection = selectedCollection;
    if (!collection || !uploadFiles.length) return;
    uploadAbort?.abort();
    uploadAbort = new AbortController();
    uploadPending = true;
    notice = null;
    try {
      for (const file of uploadFiles) {
        const activityId = addActivity(
          "Document intake",
          `Uploading ${file.name}…`,
        );
        try {
          const data = new FormData();
          data.append("file", file);
          data.append("engine", uploadEngine);
          data.append("ocr_strategy", uploadOcrStrategy);
          data.append("evaluate", String(uploadEvaluate));
          data.append("paginate_output", String(uploadPaginate));
          const response =
            await api.request<FrontendKnowledgeMutationResponse>(
              `/api/frontend/knowledge/collections/${collection.id}/upload`,
              {
                method: "POST",
                body: data,
                signal: uploadAbort.signal,
              },
            );
          updateActivity(activityId, {
            message: response.message || `${file.name} was accepted.`,
            ...(response.log_links?.length
              ? { links: response.log_links }
              : {}),
          });
          if (response.job_id) {
            await pollKnowledgeJob(
              response.job_id,
              activityId,
              uploadAbort.signal,
            );
          } else {
            updateActivity(activityId, { status: "success" });
          }
        } catch (reason) {
          if (uploadAbort.signal.aborted) throw reason;
          updateActivity(activityId, {
            message:
              reason instanceof Error
                ? reason.message
                : `${file.name} could not be processed.`,
            status: "error",
          });
        }
      }
      uploadFiles = [];
      if (uploadInput) uploadInput.value = "";
      await refreshDashboard(collection.id);
      await loadItems(collection.id, 1);
    } catch (reason) {
      if (
        !(reason instanceof DOMException && reason.name === "AbortError")
      ) {
        notice = {
          tone: "error",
          text:
            reason instanceof Error
              ? reason.message
              : "The upload could not be completed.",
        };
      }
    } finally {
      uploadPending = false;
    }
  }

  function openXray(item: FrontendKnowledgeItem): void {
    xrayItem = item;
    xray = null;
    xrayError = "";
    xrayOpen = true;
    void loadXray(item.id, 1);
  }

  async function loadXray(
    itemId: number | string,
    page: number,
  ): Promise<void> {
    const requestId = ++xrayRequest;
    xrayLoading = true;
    xrayError = "";
    try {
      const payload = await api.request<FrontendKnowledgeXray>(
        typeof itemId === "string"
          ? `/api/frontend/knowledge/sources/${encodeURIComponent(itemId)}/xray?page=${page}&per_page=30`
          : `/api/frontend/knowledge/contexts/${itemId}/xray?page=${page}&per_page=30`,
      );
      if (requestId === xrayRequest) xray = payload;
    } catch (reason) {
      if (requestId === xrayRequest) {
        xrayError =
          reason instanceof Error
            ? reason.message
            : "The source inspection could not be loaded.";
      }
    } finally {
      if (requestId === xrayRequest) xrayLoading = false;
    }
  }

  function toggleSearchCollection(collectionId: number): void {
    searchCollectionIds = searchCollectionIds.includes(collectionId)
      ? searchCollectionIds.filter((id) => id !== collectionId)
      : [...searchCollectionIds, collectionId];
  }

  async function searchKnowledge(): Promise<void> {
    if (!askQuery.trim() || !searchCollectionIds.length) return;
    searching = true;
    searchError = "";
    searchResults = [];
    try {
      const payload = await api.request<unknown>(
        "/api/frontend/knowledge/search",
        jsonRequest("POST", {
          query: askQuery.trim(),
          collection_ids: searchCollectionIds,
        }),
      );
      searchResults = Array.isArray(payload)
        ? payload.map(parseKnowledgeSearchResult)
        : [];
    } catch (reason) {
      searchError =
        reason instanceof Error
          ? reason.message
          : "The knowledge search could not be completed.";
    } finally {
      searching = false;
    }
  }
</script>

<div class="knowledge-page">
  <PageHeader
    eyebrow={`Knowledge desk / ${summary.total} collections`}
    title="Knowledge"
    description="Curate evidence, inspect how it was prepared, and retrieve the passages that matter—without stepping through provider payloads."
  >
    {#snippet actions()}
      <Button
        onclick={() => {
          editingCollection = null;
          collectionDialogOpen = true;
        }}
      >
        <Icon name="plus" size={15} />
        New collection
      </Button>
    {/snippet}
  </PageHeader>

  <div class="view-bar">
    <nav aria-label="Knowledge views">
      <button
        type="button"
        class:active={view === "library"}
        aria-current={view === "library" ? "page" : undefined}
        onclick={() => routeTo("library")}
      >
        <span>01</span>
        Library
      </button>
      <button
        type="button"
        class:active={view === "ask"}
        aria-current={view === "ask" ? "page" : undefined}
        onclick={() => routeTo("ask")}
      >
        <span>02</span>
        Ask the library
      </button>
    </nav>
    <p>
      {view === "library"
        ? "Sources, preparation, and collection health"
        : "Evidence retrieval across semantic collections"}
    </p>
  </div>

  {#if notice}
    <div class:notice-error={notice.tone === "error"} class="page-notice" role="status">
      <Icon
        name={notice.tone === "error" ? "activity" : "check"}
        size={16}
      />
      <span>{notice.text}</span>
      <button type="button" aria-label="Dismiss notice" onclick={() => (notice = null)}>
        <Icon name="close" size={14} />
      </button>
    </div>
  {/if}

  {#if loading}
    <div class="loading-state" role="status">
      <span class="loading-mark">K</span>
      <p>Opening the Knowledge desk…</p>
    </div>
  {:else if loadError}
    <EmptyState
      icon="book"
      title="Knowledge is unavailable"
      description={loadError}
    >
      {#snippet actions()}
        <Button variant="outline" onclick={() => void refreshDashboard()}>
          <Icon name="refresh" size={15} />
          Try again
        </Button>
      {/snippet}
    </EmptyState>
  {:else}
    <section class="knowledge-summary" aria-label="Knowledge summary">
      <div>
        <span>Collections</span>
        <strong>{summary.total}</strong>
        <small>curated source groups</small>
      </div>
      <div>
        <span>Searchable</span>
        <strong>{summary.vector}</strong>
        <small>semantic collections</small>
      </div>
      <div>
        <span>Direct</span>
        <strong>{summary.regular}</strong>
        <small>analysis collections</small>
      </div>
      <div>
        <span>Items</span>
        <strong>{summary.items}</strong>
        <small>available sources</small>
      </div>
    </section>

    {#if view === "library"}
      <div class="library-layout">
        <aside class="collection-index" aria-label="Knowledge collections">
          <div class="index-heading">
            <div>
              <p>Collection index</p>
              <strong>{filteredCollections.length} shown</strong>
            </div>
            <button
              type="button"
              aria-label="Refresh collections"
              onclick={() => void refreshDashboard(selectedCollectionId ?? undefined)}
            >
              <Icon name="refresh" size={16} />
            </button>
          </div>
          <div class="filters">
            <SearchField
              id="knowledge-collection-search"
              value={query}
              label="Filter collections"
              placeholder="Name, source, account…"
              oninput={(value) => (query = value)}
            />
            <div class="filter-row">
              <label>
                <span>Use</span>
                <select bind:value={modeFilter}>
                  <option value="all">All modes</option>
                  <option value="vector">Semantic</option>
                  <option value="regular">Direct</option>
                </select>
              </label>
              <label>
                <span>Source</span>
                <select bind:value={sourceFilter}>
                  <option value="all">All sources</option>
                  {#each availableSources as source (source)}
                    <option value={source}>{knowledgeSourceLabel(source)}</option>
                  {/each}
                </select>
              </label>
            </div>
          </div>

          {#if filteredCollections.length}
            <div class="collection-list">
              {#each filteredCollections as collection, index (collection.id)}
                <button
                  type="button"
                  class:selected={selectedCollectionId === collection.id}
                  aria-pressed={selectedCollectionId === collection.id}
                  onclick={() => selectCollection(collection.id)}
                >
                  <span class="collection-number">
                    {(index + 1).toString().padStart(2, "0")}
                  </span>
                  <span class="collection-copy">
                    <strong>{collection.name}</strong>
                    <small>
                      {knowledgeSourceLabel(collection.source_type)}
                      ·
                      {knowledgeModeLabel(collection.collection_mode)}
                    </small>
                  </span>
                  <span class="collection-count">{collection.item_count}</span>
                </button>
              {/each}
            </div>
          {:else}
            <div class="index-empty">
              <Icon name="search" size={18} />
              <p>No collections match these filters.</p>
              <button
                type="button"
                onclick={() => {
                  query = "";
                  modeFilter = "all";
                  sourceFilter = "all";
                }}
              >
                Clear filters
              </button>
            </div>
          {/if}
        </aside>

        <main class="collection-detail">
          {#if selectedCollection}
            <header class="detail-header">
              <div class="detail-title">
                <div class="detail-kicker">
                  <span>{knowledgeSourceLabel(selectedCollection.source_type)}</span>
                  <Badge tone={selectedCollection.collection_mode === "vector" ? "success" : "neutral"}>
                    {knowledgeModeLabel(selectedCollection.collection_mode)}
                  </Badge>
                </div>
                <h2>{selectedCollection.name}</h2>
                <p>
                  {selectedCollection.description ||
                    `${selectedCollection.item_count} ${
                      selectedCollection.item_count === 1 ? "source" : "sources"
                    } in this collection.`}
                </p>
              </div>
              <div class="detail-actions">
                <Button
                  variant="outline"
                  size="sm"
                  onclick={() => {
                    editingCollection = selectedCollection;
                    collectionDialogOpen = true;
                  }}
                >
                  <Icon name="edit" size={14} />
                  Edit
                </Button>
                {#if selectedCollection.source_type !== "document_upload"}
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={collectionActionPending === selectedCollection.id}
                    onclick={() => void runCollectionStream(selectedCollection, "sync")}
                  >
                    <Icon name="refresh" size={14} />
                    Sync
                  </Button>
                {/if}
                {#if selectedCollection.collection_mode === "regular"}
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={collectionActionPending === selectedCollection.id}
                    onclick={() => void runCollectionStream(selectedCollection, "enable-rag")}
                  >
                    <Icon name="brain" size={14} />
                    Enable search
                  </Button>
                {/if}
                {#if selectedCollection.source_type !== "document_upload"}
                  <a
                    class="small-action"
                    href={`/api/frontend/knowledge/collections/${selectedCollection.id}/export`}
                  >
                    <Icon name="download" size={14} />
                    Export
                  </a>
                {/if}
                <button
                  type="button"
                  class="danger-action"
                  onclick={() => {
                    deleteCollectionTarget = selectedCollection;
                    collectionConfirmation = "";
                  }}
                >
                  <Icon name="trash" size={14} />
                  Delete
                </button>
              </div>
            </header>

            {#if collectionMetadataRows(selectedCollection).length}
              <dl class="collection-metadata">
                {#each collectionMetadataRows(selectedCollection) as row (`${row.label}:${row.value}`)}
                  <div>
                    <dt>{row.label}</dt>
                    <dd>{row.value}</dd>
                  </div>
                {/each}
              </dl>
            {/if}

            {#if selectedCollection.source_type === "document_upload"}
              <section class="upload-studio" aria-labelledby="upload-title">
                <header>
                  <div>
                    <p>Source intake</p>
                    <h3 id="upload-title">Add documents</h3>
                  </div>
                  <span>
                    {selectedCollection.collection_mode === "vector"
                      ? "Extract and index"
                      : "Keep for direct analysis"}
                  </span>
                </header>
                <div class="upload-grid">
                  <label class="file-picker">
                    <Icon name="attach" size={18} />
                    <span>
                      <strong>
                        {uploadFiles.length
                          ? `${uploadFiles.length} ${
                              uploadFiles.length === 1 ? "file" : "files"
                            } selected`
                          : "Choose source documents"}
                      </strong>
                      <small>PDF, Word, text, Markdown, EPUB, RTF, or ODT</small>
                    </span>
                    <input
                      bind:this={uploadInput}
                      type="file"
                      multiple
                      accept=".pdf,.doc,.docx,.txt,.md,.epub,.rtf,.odt"
                      disabled={uploadPending}
                      onchange={(event) => {
                        uploadFiles = Array.from(
                          event.currentTarget.files ?? [],
                        );
                      }}
                    />
                  </label>
                  <details class="upload-options">
                    <summary>Extraction and OCR options</summary>
                    <div class="upload-settings">
                      <label>
                        <span>Extractor</span>
                        <select bind:value={uploadEngine} disabled={uploadPending}>
                          {#each dashboard?.options.engines ?? [] as engine (engine.id)}
                            <option value={engine.id}>{engine.name}</option>
                          {/each}
                        </select>
                      </label>
                      {#if selectedCollection.collection_mode === "vector"}
                        <label>
                          <span>OCR strategy</span>
                          <select bind:value={uploadOcrStrategy} disabled={uploadPending}>
                            <option value="auto">Automatic</option>
                            <option value="force_ocr">Force OCR</option>
                            <option value="no_ocr">Skip OCR</option>
                          </select>
                        </label>
                        <label class="check-option">
                          <input
                            type="checkbox"
                            bind:checked={uploadEvaluate}
                            disabled={uploadPending}
                          />
                          <span>Evaluate extraction quality</span>
                        </label>
                        <label class="check-option">
                          <input
                            type="checkbox"
                            bind:checked={uploadPaginate}
                            disabled={uploadPending}
                          />
                          <span>Preserve page markers</span>
                        </label>
                      {/if}
                    </div>
                  </details>
                </div>
                <div class="upload-footer">
                  <p>
                    Files are processed one at a time so each source has a clear
                    status in Activity.
                  </p>
                  <div>
                    {#if uploadPending}
                      <Button
                        variant="quiet"
                        size="sm"
                        onclick={() => uploadAbort?.abort()}
                      >
                        <Icon name="stop" size={14} />
                        Stop
                      </Button>
                    {/if}
                    <Button
                      size="sm"
                      disabled={!uploadFiles.length || uploadPending}
                      onclick={() => void uploadSelectedFiles()}
                    >
                      <Icon name="arrow-right" size={14} />
                      {uploadPending ? "Processing…" : "Add to collection"}
                    </Button>
                  </div>
                </div>
              </section>
            {/if}

            <div class="detail-columns">
              <section class="items-panel" aria-labelledby="items-title">
                <header class="section-heading">
                  <div>
                    <p>Collection contents</p>
                    <h3 id="items-title">Sources</h3>
                  </div>
                  <span>{itemPage?.total ?? selectedCollection.item_count}</span>
                </header>

                {#if itemsLoading && !itemPage}
                  <div class="section-state" role="status">
                    Reading collection sources…
                  </div>
                {:else if itemsError}
                  <div class="section-error" role="alert">
                    <p>{itemsError}</p>
                    <Button
                      variant="outline"
                      size="sm"
                      onclick={() => void loadItems(selectedCollection.id, 1)}
                    >
                      Try again
                    </Button>
                  </div>
                {:else if itemPage?.items.length}
                  <div class="item-list">
                    {#each itemPage.items as item (item.id)}
                      <article>
                        <div class="item-main">
                          <span
                            class:item-ready={selectedCollection.collection_mode === "vector"}
                            class="item-mark"
                          >
                            <Icon
                              name={selectedCollection.collection_mode === "vector"
                                ? "brain"
                                : "file"}
                              size={16}
                            />
                          </span>
                          <div>
                            <strong>{knowledgeItemTitle(item)}</strong>
                            <small>
                              {knowledgeSourceLabel(item.source_type)}
                              ·
                              {selectedCollection.collection_mode === "vector"
                                ? "Semantic source"
                                : "Direct source"}
                            </small>
                          </div>
                        </div>
                        {#if knowledgeItemDetails(item).length}
                          <dl>
                            {#each knowledgeItemDetails(item) as row (`${item.id}:${row.label}`)}
                              <div>
                                <dt>{row.label}</dt>
                                <dd>{row.value}</dd>
                              </div>
                            {/each}
                          </dl>
                        {/if}
                        {#if item.content_snippet}
                          <p>{item.content_snippet}</p>
                        {/if}
                        <div class="item-actions">
                          {#if item.source_id?.startsWith("/uploads/")}
                            <a href={item.source_id} target="_blank" rel="noreferrer">
                              Open source
                              <Icon name="arrow-up-right" size={13} />
                            </a>
                          {/if}
                          <button type="button" onclick={() => openXray(item)}>
                            Inspect preparation
                          </button>
                          <button
                            type="button"
                            class="item-delete"
                            onclick={() => (deleteItemTarget = item)}
                          >
                            Remove
                          </button>
                        </div>
                      </article>
                    {/each}
                  </div>
                  {#if itemPage.pages > 1}
                    <nav class="pagination" aria-label="Source pages">
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={!itemPage.has_prev || itemsLoading}
                        onclick={() =>
                          void loadItems(
                            selectedCollection.id,
                            (itemPage?.current_page ?? 2) - 1,
                          )}
                      >
                        <Icon name="chevron-left" size={14} />
                        Previous
                      </Button>
                      <span>
                        Page {itemPage.current_page} of {itemPage.pages}
                      </span>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={!itemPage.has_next || itemsLoading}
                        onclick={() =>
                          void loadItems(
                            selectedCollection.id,
                            (itemPage?.current_page ?? 0) + 1,
                          )}
                      >
                        Next
                        <Icon name="chevron-right" size={14} />
                      </Button>
                    </nav>
                  {/if}
                {:else}
                  <EmptyState
                    icon="file"
                    title="This collection is empty"
                    description={selectedCollection.source_type === "document_upload"
                      ? "Choose source documents above to begin."
                      : "Sync the provider to collect matching sources."}
                  />
                {/if}
              </section>

              <aside class="activity-panel" aria-labelledby="activity-title">
                <header class="section-heading">
                  <div>
                    <p>Live operations</p>
                    <h3 id="activity-title">Activity</h3>
                  </div>
                  {#if activities.length}
                    <button type="button" onclick={() => (activities = [])}>
                      Clear
                    </button>
                  {/if}
                </header>
                {#if activities.length}
                  <div class="activity-list" aria-live="polite">
                    {#each activities as entry (entry.id)}
                      <article class:activity-error={entry.status === "error"}>
                        <div class="activity-topline">
                          <span>
                            {#if entry.status === "success"}
                              <Icon name="check" size={14} />
                            {:else if entry.status === "error"}
                              <Icon name="activity" size={14} />
                            {:else}
                              <Icon name="refresh" size={14} />
                            {/if}
                            {entry.label}
                          </span>
                          <small>{entry.status}</small>
                        </div>
                        <p>{entry.message}</p>
                        {#if typeof entry.percent === "number"}
                          <div
                            class="progress-track"
                            role="progressbar"
                            aria-valuenow={entry.percent}
                            aria-valuemin="0"
                            aria-valuemax="100"
                          >
                            <span style={`width: ${entry.percent}%`}></span>
                          </div>
                        {/if}
                        {#if entry.links?.length}
                          <div class="activity-links">
                            {#each entry.links as link, index (`${entry.id}:${link.href}:${index}`)}
                              <a href={link.href}>
                                {link.label || link.name || "Open job log"}
                                <Icon name="arrow-up-right" size={12} />
                              </a>
                            {/each}
                          </div>
                        {/if}
                      </article>
                    {/each}
                  </div>
                {:else}
                  <div class="activity-empty">
                    <Icon name="activity" size={18} />
                    <p>
                      Uploads, provider syncs, and indexing progress will appear
                      here without interrupting the source list.
                    </p>
                  </div>
                {/if}
              </aside>
            </div>
          {:else}
            <EmptyState
              icon="book"
              title="Start a knowledge collection"
              description="Group related material by purpose, then choose whether the assistant should search it semantically or inspect it directly."
            >
              {#snippet actions()}
                <Button
                  onclick={() => {
                    editingCollection = null;
                    collectionDialogOpen = true;
                  }}
                >
                  <Icon name="plus" size={15} />
                  Create collection
                </Button>
              {/snippet}
            </EmptyState>
          {/if}
        </main>
      </div>
    {:else}
      <div class="ask-layout">
        <section class="ask-composer" aria-labelledby="ask-title">
          <p class="section-index">01 / Question</p>
          <h2 id="ask-title">What do you need to find?</h2>
          <p>
            Search returns the most relevant prepared passages. It does not
            generate an answer or alter your collections.
          </p>
          <form
            onsubmit={(event) => {
              event.preventDefault();
              void searchKnowledge();
            }}
          >
            <label>
              <span>Your question</span>
              <textarea
                rows="5"
                placeholder="Which sources discuss…"
                bind:value={askQuery}
              ></textarea>
            </label>
            <div class="ask-submit">
              <small>
                {searchCollectionIds.length}
                {searchCollectionIds.length === 1 ? "collection" : "collections"}
                selected
              </small>
              <Button
                type="submit"
                disabled={searching ||
                  !askQuery.trim() ||
                  !searchCollectionIds.length}
              >
                <Icon name="search" size={15} />
                {searching ? "Searching…" : "Find evidence"}
              </Button>
            </div>
          </form>
        </section>

        <aside class="search-scope" aria-labelledby="scope-title">
          <div class="scope-heading">
            <div>
              <p class="section-index">02 / Scope</p>
              <h2 id="scope-title">Search collections</h2>
            </div>
            {#if vectorCollections.length}
              <div>
                <button
                  type="button"
                  onclick={() =>
                    (searchCollectionIds = vectorCollections.map(
                      (collection) => collection.id,
                    ))}
                >
                  All
                </button>
                <button type="button" onclick={() => (searchCollectionIds = [])}>
                  None
                </button>
              </div>
            {/if}
          </div>
          {#if vectorCollections.length}
            <div class="scope-list">
              {#each vectorCollections as collection (collection.id)}
                <label>
                  <input
                    type="checkbox"
                    checked={searchCollectionIds.includes(collection.id)}
                    onchange={() => toggleSearchCollection(collection.id)}
                  />
                  <span>
                    <strong>{collection.name}</strong>
                    <small>
                      {knowledgeSourceLabel(collection.source_type)}
                      ·
                      {collection.item_count}
                      {collection.item_count === 1 ? "item" : "items"}
                    </small>
                  </span>
                </label>
              {/each}
            </div>
          {:else}
            <div class="scope-empty">
              <Icon name="brain" size={18} />
              <p>Enable semantic search on a collection before asking it questions.</p>
              <button type="button" onclick={() => routeTo("library")}>
                Return to library
              </button>
            </div>
          {/if}
        </aside>

        <section class="search-results" aria-labelledby="results-title">
          <header>
            <div>
              <p class="section-index">03 / Evidence</p>
              <h2 id="results-title">Retrieved passages</h2>
            </div>
            {#if searchResults.length}
              <span>{searchResults.length} results</span>
            {/if}
          </header>
          {#if searching}
            <div class="results-state" role="status">
              <Icon name="search" size={19} />
              <p>Comparing your question with prepared passages…</p>
            </div>
          {:else if searchError}
            <div class="results-error" role="alert">
              <Icon name="activity" size={18} />
              <p>{searchError}</p>
            </div>
          {:else if searchResults.length}
            <div class="result-list">
              {#each searchResults as result, index (`${index}:${result.metadata}`)}
                <article>
                  <div class="result-number">
                    <span>{(index + 1).toString().padStart(2, "0")}</span>
                    <small>{result.metadata || "Prepared source"}</small>
                  </div>
                  <p>{result.text}</p>
                </article>
              {/each}
            </div>
          {:else}
            <div class="results-state">
              <Icon name="book" size={19} />
              <p>
                Ask a precise question to retrieve passages from the selected
                collections.
              </p>
            </div>
          {/if}
        </section>
      </div>
    {/if}
  {/if}
</div>

{#if dashboard}
  <KnowledgeCollectionDialog
    open={collectionDialogOpen}
    collection={editingCollection}
    options={dashboard.options}
    {bootstrap}
    onclose={() => {
      collectionDialogOpen = false;
      editingCollection = null;
    }}
    onsave={saveCollection}
  />
{/if}

<KnowledgeXrayDialog
  open={xrayOpen}
  title={xrayItem ? knowledgeItemTitle(xrayItem) : "source"}
  {xray}
  loading={xrayLoading}
  error={xrayError}
  onclose={() => {
    xrayOpen = false;
    xrayRequest += 1;
  }}
  onpage={(page) => {
    if (xrayItem) void loadXray(xrayItem.id, page);
  }}
/>

<Dialog
  open={Boolean(deleteCollectionTarget)}
  title="Delete collection and sources"
  description="This removes the collection, its stored files, contexts, chunks, and embeddings. Items shared with another collection are also removed."
  onclose={() => {
    if (!collectionActionPending) {
      deleteCollectionTarget = null;
      collectionConfirmation = "";
    }
  }}
  size="sm"
>
  <label class="typed-confirmation">
    <span>
      Type <strong>{deleteCollectionTarget?.name}</strong> to confirm
    </span>
    <input
      type="text"
      autocomplete="off"
      spellcheck="false"
      bind:value={collectionConfirmation}
    />
  </label>
  {#snippet footer()}
    <Button
      variant="quiet"
      disabled={Boolean(collectionActionPending)}
      onclick={() => {
        deleteCollectionTarget = null;
        collectionConfirmation = "";
      }}
    >
      Cancel
    </Button>
    <Button
      variant="danger"
      disabled={Boolean(collectionActionPending) ||
        collectionConfirmation.trim() !== deleteCollectionTarget?.name}
      onclick={() => void deleteCollection()}
    >
      {collectionActionPending ? "Deleting…" : "Delete collection"}
    </Button>
  {/snippet}
</Dialog>

<style>
  .knowledge-page {
    width: min(100%, 104rem);
    margin: 0 auto;
    padding: clamp(1.35rem, 3.5vw, 3.4rem);
  }

  .small-action {
    min-height: 2.5rem;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.45rem;
    border: 1px solid var(--color-rule-strong);
    padding: 0.55rem 0.78rem;
    background: var(--color-surface);
    color: var(--color-ink);
    font-size: 0.72rem;
    font-weight: 680;
    text-decoration: none;
  }

  .small-action:hover {
    background: var(--color-surface-muted);
  }

  .view-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    border-block: 1px solid var(--color-rule-strong);
    margin-top: 1.5rem;
  }

  .view-bar nav {
    display: flex;
    align-self: stretch;
  }

  .view-bar nav button {
    min-width: 10rem;
    display: flex;
    align-items: center;
    gap: 0.55rem;
    border: 0;
    border-right: 1px solid var(--color-rule);
    padding: 0.78rem 0.9rem;
    background: transparent;
    color: var(--color-ink-muted);
    cursor: pointer;
    font-size: 0.73rem;
    font-weight: 700;
  }

  .view-bar nav button:first-child {
    border-left: 1px solid var(--color-rule);
  }

  .view-bar nav button span {
    color: var(--color-clay);
    font-family: var(--font-editorial);
    font-size: 0.8rem;
  }

  .view-bar nav button.active {
    background: var(--color-clay-muted);
    color: var(--color-ink);
    box-shadow: inset 0 -2px 0 var(--color-clay);
  }

  .view-bar > p {
    margin: 0;
    padding-right: 0.35rem;
    color: var(--color-ink-muted);
    font-size: 0.66rem;
    letter-spacing: 0.04em;
  }

  .page-notice {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    border-left: 3px solid var(--color-lichen);
    margin-top: 1rem;
    padding: 0.72rem 0.85rem;
    background: var(--color-lichen-muted);
    color: var(--color-lichen);
    font-size: 0.76rem;
  }

  .page-notice.notice-error {
    border-color: var(--color-danger);
    background: var(--color-danger-muted);
    color: var(--color-danger);
  }

  .page-notice span {
    flex: 1;
  }

  .page-notice button,
  .index-heading button,
  .activity-panel .section-heading button {
    display: grid;
    width: 2rem;
    height: 2rem;
    flex: 0 0 auto;
    place-items: center;
    border: 0;
    background: transparent;
    color: inherit;
    cursor: pointer;
  }

  .loading-state {
    min-height: 28rem;
    display: grid;
    place-content: center;
    justify-items: center;
    gap: 0.7rem;
  }

  .loading-state p {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.77rem;
  }

  .loading-mark {
    display: grid;
    width: 3.2rem;
    height: 3.2rem;
    place-items: center;
    border: 1px solid var(--color-rule-strong);
    color: var(--color-clay);
    font-family: var(--font-editorial);
    font-size: 1.5rem;
  }

  .knowledge-summary {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    border: 1px solid var(--color-rule-strong);
    border-right: 0;
    margin-top: 1.25rem;
    background: var(--color-surface);
  }

  .knowledge-summary > div {
    min-width: 0;
    display: grid;
    grid-template-columns: 1fr auto;
    align-items: end;
    gap: 0.25rem 0.5rem;
    border-right: 1px solid var(--color-rule-strong);
    padding: 0.85rem 0.95rem;
  }

  .knowledge-summary span {
    color: var(--color-ink-soft);
    font-size: 0.64rem;
    font-weight: 750;
    letter-spacing: 0.09em;
    text-transform: uppercase;
  }

  .knowledge-summary strong {
    grid-row: 1 / 3;
    grid-column: 2;
    color: var(--color-clay);
    font-family: var(--font-editorial);
    font-size: 1.75rem;
    font-weight: 480;
    line-height: 1;
  }

  .knowledge-summary small {
    color: var(--color-ink-muted);
    font-size: 0.64rem;
  }

  .library-layout {
    display: grid;
    grid-template-columns: minmax(16rem, 21rem) minmax(0, 1fr);
    gap: 1.4rem;
    margin-top: 1.4rem;
  }

  .collection-index {
    min-width: 0;
    align-self: start;
    border: 1px solid var(--color-rule-strong);
    background: var(--color-surface);
  }

  .index-heading,
  .section-heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule-strong);
    padding: 0.78rem 0.85rem;
    background: var(--color-paper);
  }

  .index-heading p,
  .section-heading p,
  .upload-studio header p {
    margin: 0 0 0.17rem;
    color: var(--color-clay);
    font-size: 0.57rem;
    font-weight: 800;
    letter-spacing: 0.13em;
    text-transform: uppercase;
  }

  .index-heading strong,
  .section-heading h3,
  .upload-studio header h3 {
    display: block;
    margin: 0;
    font-family: var(--font-editorial);
    font-size: 1rem;
    font-weight: 500;
  }

  .filters {
    display: grid;
    gap: 0.7rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.8rem;
  }

  .filter-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.55rem;
  }

  .filter-row label,
  .upload-settings > label {
    display: grid;
    gap: 0.3rem;
  }

  .filter-row label > span,
  .upload-settings label > span,
  .ask-composer form label > span {
    color: var(--color-ink-muted);
    font-size: 0.58rem;
    font-weight: 750;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  select,
  textarea,
  .typed-confirmation input {
    width: 100%;
    border: 1px solid var(--color-rule);
    border-radius: 0;
    padding: 0.58rem 0.62rem;
    background: var(--color-surface);
  }

  select {
    min-height: 2.35rem;
    font-size: 0.72rem;
  }

  .collection-list {
    display: grid;
  }

  .collection-list > button {
    min-width: 0;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.65rem;
    border: 0;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.85rem 0.78rem;
    background: transparent;
    color: var(--color-ink);
    cursor: pointer;
    text-align: left;
  }

  .collection-list > button:last-child {
    border-bottom: 0;
  }

  .collection-list > button:hover {
    background: var(--color-surface-muted);
  }

  .collection-list > button.selected {
    background: var(--color-clay-muted);
    box-shadow: inset 3px 0 0 var(--color-clay);
  }

  .collection-number {
    color: var(--color-clay);
    font-family: var(--font-editorial);
    font-size: 0.78rem;
  }

  .collection-copy {
    min-width: 0;
  }

  .collection-copy strong {
    display: block;
    overflow: hidden;
    font-family: var(--font-editorial);
    font-size: 0.9rem;
    font-weight: 550;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .collection-copy small {
    display: block;
    overflow: hidden;
    margin-top: 0.2rem;
    color: var(--color-ink-muted);
    font-size: 0.62rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .collection-count {
    min-width: 1.65rem;
    border: 1px solid var(--color-rule);
    padding: 0.2rem;
    color: var(--color-ink-muted);
    font-size: 0.62rem;
    text-align: center;
  }

  .index-empty,
  .activity-empty,
  .scope-empty {
    display: grid;
    justify-items: start;
    gap: 0.55rem;
    padding: 1.2rem;
    color: var(--color-ink-muted);
  }

  .index-empty p,
  .activity-empty p,
  .scope-empty p {
    margin: 0;
    font-size: 0.73rem;
    line-height: 1.5;
  }

  .index-empty button,
  .scope-empty button {
    border: 0;
    padding: 0;
    background: transparent;
    color: var(--color-clay);
    cursor: pointer;
    font-size: 0.7rem;
    font-weight: 700;
    text-decoration: underline;
    text-underline-offset: 0.2rem;
  }

  .collection-detail {
    min-width: 0;
  }

  .detail-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1.3rem;
    border-top: 2px solid var(--color-ink-soft);
    border-bottom: 1px solid var(--color-rule-strong);
    padding: 1rem 0.15rem 1.1rem;
  }

  .detail-title {
    min-width: 0;
  }

  .detail-kicker {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    color: var(--color-clay);
    font-size: 0.62rem;
    font-weight: 750;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .detail-title h2 {
    margin: 0.35rem 0 0;
    font-family: var(--font-editorial);
    font-size: clamp(1.8rem, 3vw, 2.65rem);
    font-weight: 460;
    line-height: 1.05;
  }

  .detail-title > p {
    max-width: 42rem;
    margin: 0.45rem 0 0;
    color: var(--color-ink-muted);
    font-size: 0.78rem;
    line-height: 1.55;
  }

  .detail-actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 0.45rem;
  }

  .small-action {
    min-height: 2.1rem;
    padding: 0.48rem 0.68rem;
    font-size: 0.73rem;
  }

  .danger-action {
    min-height: 2.1rem;
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    border: 0;
    padding: 0.48rem 0.55rem;
    background: transparent;
    color: var(--color-danger);
    cursor: pointer;
    font-size: 0.71rem;
    font-weight: 680;
  }

  .danger-action:hover {
    background: var(--color-danger-muted);
  }

  .collection-metadata {
    display: flex;
    flex-wrap: wrap;
    gap: 0;
    margin: 0;
    border-bottom: 1px solid var(--color-rule);
    background: var(--color-surface);
  }

  .collection-metadata > div {
    min-width: 10rem;
    flex: 1;
    border-right: 1px solid var(--color-rule);
    padding: 0.7rem 0.8rem;
  }

  .collection-metadata dt,
  .item-list dt {
    color: var(--color-ink-muted);
    font-size: 0.56rem;
    font-weight: 750;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .collection-metadata dd {
    margin: 0.22rem 0 0;
    color: var(--color-ink-soft);
    font-size: 0.7rem;
    line-height: 1.35;
  }

  .upload-studio {
    border: 1px solid var(--color-rule-strong);
    margin-top: 1.2rem;
    background: var(--color-surface);
  }

  .upload-studio > header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.78rem 0.9rem;
    background: var(--color-tobacco-muted);
  }

  .upload-studio header > span {
    color: var(--color-tobacco);
    font-size: 0.63rem;
    font-weight: 700;
  }

  .upload-grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 1rem;
    padding: 0.9rem;
  }

  .file-picker {
    min-width: 0;
    position: relative;
    min-height: 7.2rem;
    display: flex;
    align-items: center;
    gap: 0.75rem;
    border: 1px dashed var(--color-rule-strong);
    padding: 1rem;
    background: var(--color-paper);
    color: var(--color-tobacco);
    cursor: pointer;
  }

  .file-picker > span {
    min-width: 0;
  }

  .file-picker:hover {
    background: var(--color-tobacco-muted);
  }

  .file-picker strong,
  .file-picker small {
    display: block;
  }

  .file-picker strong {
    color: var(--color-ink);
    font-family: var(--font-editorial);
    font-size: 0.95rem;
    font-weight: 550;
  }

  .file-picker small {
    margin-top: 0.24rem;
    color: var(--color-ink-muted);
    font-size: 0.63rem;
    line-height: 1.45;
  }

  .file-picker input {
    position: absolute;
    inset: 0;
    opacity: 0;
    cursor: pointer;
  }

  .upload-options {
    min-width: 0;
    border: 1px solid var(--color-rule);
    background: var(--color-paper);
  }

  .upload-options summary {
    min-height: 2.75rem;
    display: flex;
    align-items: center;
    padding: 0.55rem 0.7rem;
    color: var(--color-ink-soft);
    cursor: pointer;
    font-size: 0.68rem;
    font-weight: 700;
  }

  .upload-options[open] summary {
    border-bottom: 1px solid var(--color-rule);
  }

  .upload-settings {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    align-content: start;
    gap: 0.7rem;
    padding: 0.7rem;
  }

  .check-option {
    display: flex !important;
    align-items: center;
    gap: 0.5rem !important;
    min-height: 2.35rem;
    border: 1px solid var(--color-rule);
    padding: 0.55rem 0.65rem;
    color: var(--color-ink-soft);
    font-size: 0.68rem;
  }

  .check-option input {
    accent-color: var(--color-lichen);
  }

  .upload-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    border-top: 1px solid var(--color-rule);
    padding: 0.75rem 0.9rem;
  }

  .upload-footer p {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.66rem;
    line-height: 1.45;
  }

  .upload-footer > div {
    display: flex;
    gap: 0.45rem;
  }

  .detail-columns {
    display: grid;
    grid-template-columns: minmax(0, 1.55fr) minmax(15rem, 0.65fr);
    gap: 1.2rem;
    align-items: start;
    margin-top: 1.25rem;
  }

  .items-panel,
  .activity-panel {
    min-width: 0;
    border: 1px solid var(--color-rule-strong);
    background: var(--color-surface);
  }

  .section-heading > span {
    min-width: 2rem;
    border: 1px solid var(--color-rule);
    padding: 0.22rem 0.35rem;
    color: var(--color-ink-muted);
    font-family: var(--font-editorial);
    font-size: 0.75rem;
    text-align: center;
  }

  .activity-panel .section-heading button {
    width: auto;
    padding-inline: 0.35rem;
    color: var(--color-ink-muted);
    font-size: 0.64rem;
  }

  .section-state,
  .section-error {
    padding: 1.2rem;
    color: var(--color-ink-muted);
    font-size: 0.74rem;
  }

  .section-error {
    display: grid;
    justify-items: start;
    gap: 0.7rem;
    background: var(--color-danger-muted);
    color: var(--color-danger);
  }

  .section-error p {
    margin: 0;
  }

  .item-list {
    display: grid;
  }

  .item-list article {
    border-bottom: 1px solid var(--color-rule);
    padding: 0.85rem;
  }

  .item-main {
    display: flex;
    align-items: center;
    gap: 0.65rem;
  }

  .item-mark {
    display: grid;
    width: 2.2rem;
    height: 2.2rem;
    flex: 0 0 auto;
    place-items: center;
    border: 1px solid var(--color-rule);
    background: var(--color-paper);
    color: var(--color-tobacco);
  }

  .item-mark.item-ready {
    background: var(--color-lichen-muted);
    color: var(--color-lichen);
  }

  .item-main strong {
    display: block;
    font-family: var(--font-editorial);
    font-size: 0.9rem;
    font-weight: 550;
  }

  .item-main small {
    display: block;
    margin-top: 0.16rem;
    color: var(--color-ink-muted);
    font-size: 0.62rem;
  }

  .item-list dl {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem 0.85rem;
    margin: 0.65rem 0 0;
  }

  .item-list dl > div {
    display: flex;
    gap: 0.28rem;
  }

  .item-list dd {
    margin: 0;
    color: var(--color-ink-soft);
    font-size: 0.62rem;
  }

  .item-list article > p {
    display: -webkit-box;
    overflow: hidden;
    margin: 0.55rem 0 0;
    color: var(--color-ink-muted);
    font-size: 0.69rem;
    line-height: 1.5;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
  }

  .item-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.65rem;
    margin-top: 0.7rem;
  }

  .item-actions button,
  .item-actions a {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    border: 0;
    padding: 0;
    background: transparent;
    color: var(--color-clay);
    cursor: pointer;
    font-size: 0.64rem;
    font-weight: 700;
    text-decoration: none;
  }

  .item-actions .item-delete {
    color: var(--color-danger);
  }

  .pagination {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.7rem;
    border-top: 1px solid var(--color-rule);
    padding: 0.75rem;
  }

  .pagination span {
    color: var(--color-ink-muted);
    font-size: 0.65rem;
  }

  .activity-list {
    display: grid;
  }

  .activity-list article {
    border-left: 3px solid var(--color-lichen);
    border-bottom: 1px solid var(--color-rule);
    padding: 0.75rem;
  }

  .activity-list article.activity-error {
    border-left-color: var(--color-danger);
    background: var(--color-danger-muted);
  }

  .activity-topline {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
  }

  .activity-topline span {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    color: var(--color-lichen);
    font-size: 0.6rem;
    font-weight: 800;
    letter-spacing: 0.07em;
    text-transform: uppercase;
  }

  .activity-error .activity-topline span {
    color: var(--color-danger);
  }

  .activity-topline small {
    color: var(--color-ink-muted);
    font-size: 0.57rem;
    text-transform: capitalize;
  }

  .activity-list article > p {
    margin: 0.45rem 0 0;
    color: var(--color-ink-soft);
    font-size: 0.69rem;
    line-height: 1.5;
  }

  .progress-track {
    height: 0.25rem;
    overflow: hidden;
    margin-top: 0.55rem;
    background: var(--color-surface-sunken);
  }

  .progress-track span {
    display: block;
    height: 100%;
    background: var(--color-lichen);
    transition: width var(--shell-transition);
  }

  .activity-links {
    display: grid;
    gap: 0.2rem;
    margin-top: 0.55rem;
  }

  .activity-links a {
    display: flex;
    align-items: center;
    gap: 0.25rem;
    color: var(--color-clay);
    font-size: 0.62rem;
    text-decoration: none;
  }

  .ask-layout {
    display: grid;
    grid-template-columns: minmax(0, 1.35fr) minmax(18rem, 0.65fr);
    gap: 1.4rem;
    margin-top: 1.4rem;
  }

  .ask-composer,
  .search-scope,
  .search-results {
    min-width: 0;
    border-top: 2px solid var(--color-ink-soft);
    padding-top: 1rem;
  }

  .section-index {
    margin: 0 0 0.28rem;
    color: var(--color-clay);
    font-size: 0.6rem;
    font-weight: 800;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  .ask-composer h2,
  .search-scope h2,
  .search-results h2 {
    margin: 0;
    font-family: var(--font-editorial);
    font-size: clamp(1.4rem, 3vw, 2rem);
    font-weight: 480;
  }

  .ask-composer > p:not(.section-index) {
    max-width: 42rem;
    margin: 0.45rem 0 1rem;
    color: var(--color-ink-muted);
    font-size: 0.76rem;
    line-height: 1.55;
  }

  .ask-composer form {
    border: 1px solid var(--color-rule-strong);
    background: var(--color-surface);
  }

  .ask-composer form > label {
    display: grid;
    gap: 0.42rem;
    padding: 0.9rem;
  }

  textarea {
    min-height: 8.5rem;
    resize: vertical;
    color: var(--color-ink);
    font-family: var(--font-editorial);
    font-size: 1.02rem;
    line-height: 1.55;
  }

  .ask-submit {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    border-top: 1px solid var(--color-rule);
    padding: 0.7rem 0.9rem;
    background: var(--color-paper);
  }

  .ask-submit small {
    color: var(--color-ink-muted);
    font-size: 0.65rem;
  }

  .scope-heading,
  .search-results > header {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule-strong);
    padding: 0 0.1rem 0.8rem;
  }

  .scope-heading > div:last-child {
    display: flex;
    gap: 0.5rem;
  }

  .scope-heading button {
    border: 0;
    padding: 0;
    background: transparent;
    color: var(--color-clay);
    cursor: pointer;
    font-size: 0.63rem;
    font-weight: 750;
  }

  .scope-list {
    border-inline: 1px solid var(--color-rule);
  }

  .scope-list label {
    display: flex;
    align-items: center;
    gap: 0.65rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.78rem;
    background: var(--color-surface);
    cursor: pointer;
  }

  .scope-list label:hover {
    background: var(--color-surface-muted);
  }

  .scope-list input {
    width: 1rem;
    height: 1rem;
    accent-color: var(--color-lichen);
  }

  .scope-list strong,
  .scope-list small {
    display: block;
  }

  .scope-list strong {
    font-family: var(--font-editorial);
    font-size: 0.86rem;
    font-weight: 550;
  }

  .scope-list small {
    margin-top: 0.15rem;
    color: var(--color-ink-muted);
    font-size: 0.61rem;
  }

  .search-results {
    grid-column: 1 / -1;
    margin-top: 0.6rem;
  }

  .search-results > header > span {
    color: var(--color-ink-muted);
    font-size: 0.66rem;
  }

  .results-state,
  .results-error {
    min-height: 10rem;
    display: grid;
    place-content: center;
    justify-items: center;
    gap: 0.55rem;
    border: 1px solid var(--color-rule);
    border-top: 0;
    padding: 1.2rem;
    color: var(--color-ink-muted);
    text-align: center;
  }

  .results-state p,
  .results-error p {
    max-width: 30rem;
    margin: 0;
    font-size: 0.73rem;
    line-height: 1.5;
  }

  .results-error {
    background: var(--color-danger-muted);
    color: var(--color-danger);
  }

  .result-list {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    border-left: 1px solid var(--color-rule);
  }

  .result-list article {
    min-width: 0;
    border-right: 1px solid var(--color-rule);
    border-bottom: 1px solid var(--color-rule);
    padding: 1rem;
    background: var(--color-surface);
  }

  .result-number {
    display: flex;
    align-items: center;
    gap: 0.65rem;
    border-bottom: 1px solid var(--color-rule);
    padding-bottom: 0.6rem;
  }

  .result-number span {
    color: var(--color-clay);
    font-family: var(--font-editorial);
    font-size: 1rem;
  }

  .result-number small {
    overflow: hidden;
    color: var(--color-ink-muted);
    font-size: 0.61rem;
    line-height: 1.4;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .result-list article > p {
    margin: 0.75rem 0 0;
    color: var(--color-ink-soft);
    font-size: 0.76rem;
    line-height: 1.65;
    white-space: pre-wrap;
  }

  .typed-confirmation {
    display: grid;
    gap: 0.55rem;
  }

  .typed-confirmation > span {
    color: var(--color-ink-soft);
    font-size: 0.74rem;
    line-height: 1.5;
  }

  .typed-confirmation strong {
    color: var(--color-danger);
  }

  .typed-confirmation input {
    min-height: 2.55rem;
  }

  .dialog-copy {
    margin: 0;
    border-left: 3px solid var(--color-danger);
    padding: 0.8rem;
    background: var(--color-danger-muted);
    color: var(--color-danger);
    font-size: 0.74rem;
    line-height: 1.55;
  }

  @media (max-width: 76rem) {
    .library-layout {
      grid-template-columns: minmax(14rem, 18rem) minmax(0, 1fr);
    }

    .detail-header {
      align-items: stretch;
      flex-direction: column;
    }

    .detail-actions {
      justify-content: flex-start;
    }

    .detail-columns {
      grid-template-columns: minmax(0, 1fr);
    }

    .activity-panel {
      order: -1;
    }

    .activity-list {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }

  @media (max-width: 58rem) {
    .knowledge-summary {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .library-layout {
      grid-template-columns: 1fr;
    }

    .collection-index {
      max-height: 28rem;
      overflow: auto;
    }

    .collection-list {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .collection-list > button:nth-last-child(2):nth-child(odd) {
      border-bottom: 0;
    }

    .upload-grid,
    .ask-layout {
      grid-template-columns: 1fr;
    }

    .search-results {
      grid-column: auto;
    }
  }

  @media (max-width: 44rem) {
    .knowledge-page {
      padding: 1rem;
    }

    .view-bar {
      align-items: stretch;
      flex-direction: column;
      gap: 0;
    }

    .view-bar nav {
      display: grid;
      grid-template-columns: 1fr 1fr;
    }

    .view-bar nav button {
      min-width: 0;
      justify-content: center;
    }

    .view-bar > p {
      padding: 0.65rem 0.7rem;
    }

    .knowledge-summary {
      grid-template-columns: 1fr 1fr;
    }

    .knowledge-summary > div {
      padding: 0.7rem;
    }

    .filter-row,
    .collection-list,
    .upload-settings,
    .activity-list,
    .result-list {
      grid-template-columns: 1fr;
    }

    .collection-list > button {
      border-bottom: 1px solid var(--color-rule) !important;
    }

    .collection-list > button:last-child {
      border-bottom: 0 !important;
    }

    .collection-metadata {
      display: grid;
      grid-template-columns: 1fr;
    }

    .collection-metadata > div {
      border-right: 0;
      border-bottom: 1px solid var(--color-rule);
    }

    .upload-studio > header,
    .upload-footer,
    .ask-submit,
    .scope-heading,
    .search-results > header {
      align-items: stretch;
      flex-direction: column;
    }

    .upload-footer > div {
      justify-content: flex-end;
    }

    .pagination {
      align-items: stretch;
      flex-direction: column;
    }
  }
</style>

<Dialog
  open={Boolean(deleteItemTarget)}
  title="Remove this source?"
  description={deleteItemTarget
    ? `${knowledgeItemTitle(deleteItemTarget)} and its semantic index will be deleted.`
    : undefined}
  onclose={() => {
    if (!itemDeletePending) deleteItemTarget = null;
  }}
  size="sm"
>
  <p class="dialog-copy">
    The source will no longer be available to this or any other collection that
    currently references it.
  </p>
  {#snippet footer()}
    <Button
      variant="quiet"
      disabled={itemDeletePending}
      onclick={() => (deleteItemTarget = null)}
    >
      Keep source
    </Button>
    <Button
      variant="danger"
      disabled={itemDeletePending}
      onclick={() => void deleteItem()}
    >
      {itemDeletePending ? "Removing…" : "Remove source"}
    </Button>
  {/snippet}
</Dialog>
