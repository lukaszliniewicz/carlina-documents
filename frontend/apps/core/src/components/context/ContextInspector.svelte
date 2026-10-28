<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import { shouldUseClientNavigation } from "@carlina/sdk";
  import type {
    FrontendApiClient,
    FrontendArtifactDetail,
    FrontendArtifactPage,
    FrontendArtifactSummary,
    FrontendContextDetail,
    FrontendContextItem,
    FrontendContextLifecycleState,
    FrontendContextPage,
    FrontendContextUpdate,
    FrontendInspectorTab,
    FrontendMemoryEntry,
    FrontendMemoryPage,
    FrontendRunPage,
    FrontendRunSummary,
    FrontendWorkspaceSummary,
  } from "@carlina/sdk";
  import { Icon } from "@carlina/ui";

  import {
    mergeWorkspacePage,
    workspaceRefreshTargets,
    type WorkspaceRefreshTarget,
  } from "../../lib/workspace-state.ts";
  import ArtifactsPanel from "./ArtifactsPanel.svelte";
  import ContextItemsPanel from "./ContextItemsPanel.svelte";
  import MemoryPanel from "./MemoryPanel.svelte";
  import OverviewPanel from "./OverviewPanel.svelte";
  import RunsPanel from "./RunsPanel.svelte";

  interface Props {
    api: FrontendApiClient;
    sessionId: string | null;
    navigate: (href: string, clientRoute?: boolean) => void;
  }

  interface WorkspaceEventDetail {
    session_id?: string | null;
    sessionId?: string | null;
    event?: Readonly<Record<string, unknown>>;
    artifact_id?: string;
    artifactId?: string;
    tab?: FrontendInspectorTab;
  }

  let { api, sessionId, navigate }: Props = $props();

  function navigateRun(href: string, event: MouseEvent) {
    if (!shouldUseClientNavigation(event)) return;
    event.preventDefault();
    navigate(href, true);
  }

  const tabs: ReadonlyArray<{
    id: FrontendInspectorTab;
    label: string;
    icon: "workspaces" | "activity" | "brain" | "file";
  }> = [
    { id: "overview", label: "Overview", icon: "workspaces" },
    { id: "runs", label: "Runs", icon: "activity" },
    { id: "memory", label: "Memory", icon: "brain" },
    { id: "context", label: "Context", icon: "workspaces" },
    { id: "artifacts", label: "Artifacts", icon: "file" },
  ];

  let open = $state(false);
  let expanded = $state(false);
  let width = $state(410);
  let activeTab = $state<FrontendInspectorTab>("overview");
  let announcement = $state("");
  let summary = $state<FrontendWorkspaceSummary | null>(null);
  let summaryLoading = $state(false);
  let summaryError = $state("");

  let runs = $state<FrontendRunSummary[]>([]);
  let runTotal = $state(0);
  let runCursor = $state<string | null>(null);
  let runView = $state<"all" | "active" | "attention" | "completed">("all");
  let runsLoading = $state(false);
  let runsLoadingMore = $state(false);
  let runsError = $state("");
  let runsLoaded = $state(false);

  let memory = $state<FrontendMemoryEntry[]>([]);
  let memoryTotal = $state(0);
  let memoryCursor = $state<string | null>(null);
  let memoryScope = $state<"all" | "session" | "personal">("all");
  let memoryQuery = $state("");
  let memoryLoading = $state(false);
  let memoryLoadingMore = $state(false);
  let memoryError = $state("");
  let memoryLoaded = $state(false);

  let contextItems = $state<FrontendContextItem[]>([]);
  let contextTotal = $state(0);
  let contextCursor = $state<string | null>(null);
  let contextState = $state<"all" | FrontendContextLifecycleState>("all");
  let contextQuery = $state("");
  let contextSearchTruncated = $state(false);
  let contextLoading = $state(false);
  let contextLoadingMore = $state(false);
  let contextError = $state("");
  let contextLoaded = $state(false);
  let contextDetail = $state<FrontendContextDetail | null>(null);
  let contextDetailLoading = $state(false);
  let contextDetailLoadingMore = $state(false);
  let contextDetailError = $state("");
  let contextUpdatingId = $state<string | null>(null);

  let artifacts = $state<FrontendArtifactSummary[]>([]);
  let artifactTotal = $state(0);
  let artifactCursor = $state<string | null>(null);
  let artifactStatus = $state("all");
  let artifactQuery = $state("");
  let artifactSearchTruncated = $state(false);
  let artifactsLoading = $state(false);
  let artifactsLoadingMore = $state(false);
  let artifactsError = $state("");
  let artifactsLoaded = $state(false);
  let artifactDetail = $state<FrontendArtifactDetail | null>(null);
  let artifactDetailLoading = $state(false);
  let artifactDetailError = $state("");
  let reviewingArtifact = $state(false);

  let summaryController: AbortController | null = null;
  let runsController: AbortController | null = null;
  let memoryController: AbortController | null = null;
  let contextController: AbortController | null = null;
  let contextDetailController: AbortController | null = null;
  let artifactsController: AbortController | null = null;
  let detailController: AbortController | null = null;
  let pollingTimer: number | null = null;
  let refreshTimer: number | null = null;
  let pollFailures = 0;
  let generation = 0;
  let mounted = false;
  let lastSessionId: string | null | undefined = undefined;
  let resizeStartX = 0;
  let resizeStartWidth = 0;
  let resizing = false;
  let previousBodyCursor = "";
  let previousBodyUserSelect = "";

  const inspectorWidth = $derived(
    expanded ? Math.max(width, 680) : width,
  );
  const memoryCount = $derived(
    (summary?.counts.session_memory ?? 0) +
      (summary?.counts.personal_memory ?? 0),
  );

  $effect(() => {
    const nextSessionId = sessionId;
    if (!mounted || nextSessionId === lastSessionId) return;
    lastSessionId = nextSessionId;
    resetForSession(nextSessionId);
  });

  onMount(() => {
    mounted = true;
    width = readNumber("iarf:context-inspector-width", 410, 320, 720);
    const storedOpen = readStorage("iarf:context-inspector-open");
    open =
      storedOpen === "open" ||
      (storedOpen === null &&
        window.matchMedia("(min-width: 60rem)").matches);
    lastSessionId = sessionId;
    resetForSession(sessionId);

    const handleWorkspaceEvent = (event: Event) => {
      const detail = (event as CustomEvent<WorkspaceEventDetail>).detail ?? {};
      const targetSession = detail.session_id ?? detail.sessionId ?? null;
      if (targetSession && targetSession !== sessionId) return;
      const targets = detail.event
        ? workspaceRefreshTargets(detail.event)
        : new Set<WorkspaceRefreshTarget>([
            "summary",
             "runs",
             "memory",
             "context",
             "artifacts",
          ]);
      scheduleTargetRefresh(targets);
    };
    const handleOpenContext = (event: Event) => {
      const detail = (event as CustomEvent<WorkspaceEventDetail>).detail ?? {};
      const targetSession = detail.session_id ?? detail.sessionId ?? null;
      if (targetSession && targetSession !== sessionId) return;
      const tab = detail.tab ?? (detail.artifact_id || detail.artifactId
        ? "artifacts"
        : "overview");
      selectTab(tab);
      const artifactId = detail.artifact_id ?? detail.artifactId;
      if (artifactId) void loadArtifactDetail(artifactId);
    };
    const handleVisibility = () => {
      if (!document.hidden) {
        void refreshSummary(true);
      } else {
        clearPolling();
      }
    };
    const handleKeydown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && open && isOverlayLayout()) {
        closeInspector();
      }
    };

    window.addEventListener("iarf:workspace-event", handleWorkspaceEvent);
    window.addEventListener("iarf:workspace-refresh", handleWorkspaceEvent);
    window.addEventListener("iarf:open-context", handleOpenContext);
    document.addEventListener("visibilitychange", handleVisibility);
    window.addEventListener("keydown", handleKeydown);
    return () => {
      window.removeEventListener("iarf:workspace-event", handleWorkspaceEvent);
      window.removeEventListener("iarf:workspace-refresh", handleWorkspaceEvent);
      window.removeEventListener("iarf:open-context", handleOpenContext);
      document.removeEventListener("visibilitychange", handleVisibility);
      window.removeEventListener("keydown", handleKeydown);
    };
  });

  onDestroy(() => {
    generation += 1;
    abortRequests();
    clearPolling();
    if (refreshTimer !== null) window.clearTimeout(refreshTimer);
    stopResize();
  });

  function readStorage(key: string): string | null {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  }

  function writeStorage(key: string, value: string) {
    try {
      localStorage.setItem(key, value);
    } catch {
      // The inspector remains usable when browser storage is unavailable.
    }
  }

  function readNumber(
    key: string,
    fallback: number,
    minimum: number,
    maximum: number,
  ): number {
    const stored = readStorage(key);
    if (stored === null || stored.trim() === "") return fallback;
    const parsed = Number(stored);
    return Number.isFinite(parsed)
      ? Math.max(minimum, Math.min(maximum, parsed))
      : fallback;
  }

  function storedTab(id: string | null): FrontendInspectorTab {
    const stored = readStorage(`iarf:context-inspector-tab:${id ?? "new"}`);
    return tabs.some((tab) => tab.id === stored)
      ? (stored as FrontendInspectorTab)
      : "overview";
  }

  function announce(message: string) {
    announcement = "";
    queueMicrotask(() => {
      announcement = message;
    });
  }

  function abortRequests() {
    summaryController?.abort();
    runsController?.abort();
    memoryController?.abort();
    contextController?.abort();
    contextDetailController?.abort();
    artifactsController?.abort();
    detailController?.abort();
  }

  function resetForSession(id: string | null) {
    generation += 1;
    abortRequests();
    clearPolling();
    summary = null;
    summaryError = "";
    summaryLoading = false;
    runs = [];
    runTotal = 0;
    runCursor = null;
    runsLoading = false;
    runsLoadingMore = false;
    runsError = "";
    runsLoaded = false;
    memory = [];
    memoryTotal = 0;
    memoryCursor = null;
    memoryLoading = false;
    memoryLoadingMore = false;
    memoryError = "";
    memoryLoaded = false;
    contextItems = [];
    contextTotal = 0;
    contextCursor = null;
    contextSearchTruncated = false;
    contextLoading = false;
    contextLoadingMore = false;
    contextError = "";
    contextLoaded = false;
    contextDetail = null;
    contextDetailLoading = false;
    contextDetailLoadingMore = false;
    contextDetailError = "";
    contextUpdatingId = null;
    artifacts = [];
    artifactTotal = 0;
    artifactCursor = null;
    artifactsLoading = false;
    artifactsLoadingMore = false;
    artifactSearchTruncated = false;
    artifactsError = "";
    artifactsLoaded = false;
    artifactDetail = null;
    artifactDetailLoading = false;
    artifactDetailError = "";
    reviewingArtifact = false;
    pollFailures = 0;
    activeTab = storedTab(id);
    if (!id) return;
    void refreshSummary(false).then(() => {
      if (id === sessionId) void ensureActiveTabLoaded();
    });
  }

  function workspacePath(suffix: string): string {
    if (!sessionId) return "";
    return `/api/frontend/chats/${encodeURIComponent(sessionId)}/${suffix}`;
  }

  function isOverlayLayout(): boolean {
    return window.matchMedia("(max-width: 59.99rem)").matches;
  }

  function openInspector() {
    open = true;
    writeStorage("iarf:context-inspector-open", "open");
    void refreshSummary(false);
    void ensureActiveTabLoaded();
    queueMicrotask(() => {
      document
        .querySelector<HTMLElement>(".context-inspector__tabs button[aria-selected='true']")
        ?.focus({ preventScroll: true });
    });
  }

  function closeInspector() {
    open = false;
    expanded = false;
    writeStorage("iarf:context-inspector-open", "closed");
    queueMicrotask(() => {
      document
        .querySelector<HTMLElement>(".context-inspector__trigger")
        ?.focus({ preventScroll: true });
    });
  }

  function selectTab(tab: FrontendInspectorTab) {
    activeTab = tab;
    open = true;
    artifactDetail = tab === "artifacts" ? artifactDetail : null;
    contextDetail = tab === "context" ? contextDetail : null;
    contextDetailError = "";
    artifactDetailError = "";
    writeStorage("iarf:context-inspector-open", "open");
    writeStorage(
      `iarf:context-inspector-tab:${sessionId ?? "new"}`,
      tab,
    );
    void ensureActiveTabLoaded();
  }

  function handleTabKeydown(event: KeyboardEvent) {
    const currentIndex = tabs.findIndex((tab) => tab.id === activeTab);
    let nextIndex = currentIndex;
    if (event.key === "ArrowRight") nextIndex = (currentIndex + 1) % tabs.length;
    else if (event.key === "ArrowLeft") {
      nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
    } else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = tabs.length - 1;
    else return;
    event.preventDefault();
    selectTab(tabs[nextIndex]!.id);
    queueMicrotask(() => {
      document.getElementById(`context-tab-${tabs[nextIndex]!.id}`)?.focus();
    });
  }

  async function ensureActiveTabLoaded(force = false) {
    if (!sessionId) return;
    if (activeTab === "runs" && (force || !runsLoaded)) {
      await loadRuns(true);
    } else if (activeTab === "memory" && (force || !memoryLoaded)) {
      await loadMemory(true);
    } else if (activeTab === "context" && (force || !contextLoaded)) {
      await loadContextItems(true);
    } else if (activeTab === "artifacts" && (force || !artifactsLoaded)) {
      await loadArtifacts(true);
    }
  }

  async function refreshSummary(fromPoll: boolean) {
    if (!sessionId || document.hidden) return;
    if (summaryLoading && fromPoll) return;
    const requestGeneration = generation;
    summaryController?.abort();
    const controller = new AbortController();
    summaryController = controller;
    summaryLoading = true;
    if (!fromPoll) summaryError = "";
    const previousRevision = summary?.revision ?? null;
    try {
      const payload = await api.request<FrontendWorkspaceSummary>(
        workspacePath("workspace"),
        { signal: controller.signal },
      );
      if (requestGeneration !== generation) return;
      summary = payload;
      summaryError = "";
      pollFailures = 0;
      if (
        fromPoll &&
        previousRevision &&
        payload.revision !== previousRevision
      ) {
        await ensureActiveTabLoaded(true);
        if (artifactDetail) {
          await loadArtifactDetail(artifactDetail.artifact.id, false);
        }
        announce("Conversation workspace updated.");
      }
    } catch (reason) {
      if (controller.signal.aborted) return;
      pollFailures += 1;
      summaryError =
        reason instanceof Error
          ? reason.message
          : "The conversation workspace could not be loaded.";
    } finally {
      if (
        requestGeneration === generation &&
        summaryController === controller
      ) {
        summaryLoading = false;
        schedulePolling();
      }
    }
  }

  function scheduleTargetRefresh(targets: Set<WorkspaceRefreshTarget>) {
    if (!sessionId) return;
    if (refreshTimer !== null) window.clearTimeout(refreshTimer);
    refreshTimer = window.setTimeout(() => {
      refreshTimer = null;
      void refreshSummary(false);
      if (targets.has("runs") && (activeTab === "runs" || runsLoaded)) {
        void loadRuns(true);
      }
      if (targets.has("memory") && (activeTab === "memory" || memoryLoaded)) {
        void loadMemory(true);
      }
      if (
        targets.has("context") &&
        (activeTab === "context" || contextLoaded)
      ) {
        void loadContextItems(true);
      }
      if (
        targets.has("artifacts") &&
        (activeTab === "artifacts" || artifactsLoaded)
      ) {
        void loadArtifacts(true);
        if (artifactDetail) {
          void loadArtifactDetail(artifactDetail.artifact.id, false);
        }
      }
    }, 220);
  }

  function clearPolling() {
    if (pollingTimer !== null) window.clearTimeout(pollingTimer);
    pollingTimer = null;
  }

  function schedulePolling() {
    clearPolling();
    if (!sessionId || document.hidden) return;
    const active =
      (summary?.counts.active_runs ?? 0) > 0 ||
      (summary?.counts.attention ?? 0) > 0;
    const base = !open ? 30_000 : active ? 5_000 : 20_000;
    const delay = Math.min(60_000, base * Math.max(1, 2 ** pollFailures));
    pollingTimer = window.setTimeout(() => {
      pollingTimer = null;
      void refreshSummary(true);
    }, delay);
  }

  function runQueryPath(cursor?: string | null): string {
    const params = new URLSearchParams({ limit: "30", view: runView });
    if (cursor) params.set("cursor", cursor);
    return `${workspacePath("runs")}?${params}`;
  }

  async function loadRuns(reset: boolean) {
    if (!sessionId) return;
    const cursor = reset ? null : runCursor;
    if (!reset && !cursor) return;
    const requestGeneration = generation;
    runsController?.abort();
    const controller = new AbortController();
    runsController = controller;
    runsError = "";
    if (reset) runsLoading = true;
    else runsLoadingMore = true;
    try {
      const page = await api.request<FrontendRunPage>(runQueryPath(cursor), {
        signal: controller.signal,
      });
      if (requestGeneration !== generation) return;
      runs = reset ? page.items : mergeWorkspacePage(runs, page.items);
      runTotal = page.total;
      runCursor = page.next_cursor;
      runsLoaded = true;
    } catch (reason) {
      if (controller.signal.aborted) return;
      runsError =
        reason instanceof Error ? reason.message : "Runs could not be loaded.";
    } finally {
      if (requestGeneration === generation && runsController === controller) {
        runsLoading = false;
        runsLoadingMore = false;
      }
    }
  }

  function changeRunView(
    view: "all" | "active" | "attention" | "completed",
  ) {
    if (view === runView) return;
    runView = view;
    runsLoaded = false;
    runs = [];
    runCursor = null;
    void loadRuns(true);
  }

  function memoryQueryPath(cursor?: string | null): string {
    const params = new URLSearchParams({
      limit: "30",
      scope: memoryScope,
    });
    if (memoryQuery) params.set("q", memoryQuery);
    if (cursor) params.set("cursor", cursor);
    return `${workspacePath("memory")}?${params}`;
  }

  async function loadMemory(reset: boolean) {
    if (!sessionId) return;
    const cursor = reset ? null : memoryCursor;
    if (!reset && !cursor) return;
    const requestGeneration = generation;
    memoryController?.abort();
    const controller = new AbortController();
    memoryController = controller;
    memoryError = "";
    if (reset) memoryLoading = true;
    else memoryLoadingMore = true;
    try {
      const page = await api.request<FrontendMemoryPage>(
        memoryQueryPath(cursor),
        { signal: controller.signal },
      );
      if (requestGeneration !== generation) return;
      memory = reset ? page.items : mergeWorkspacePage(memory, page.items);
      memoryTotal = page.total;
      memoryCursor = page.next_cursor;
      memoryLoaded = true;
    } catch (reason) {
      if (controller.signal.aborted) return;
      memoryError =
        reason instanceof Error
          ? reason.message
          : "Workspace memory could not be loaded.";
    } finally {
      if (requestGeneration === generation && memoryController === controller) {
        memoryLoading = false;
        memoryLoadingMore = false;
      }
    }
  }

  function changeMemoryScope(scope: "all" | "session" | "personal") {
    if (scope === memoryScope) return;
    memoryScope = scope;
    memory = [];
    memoryCursor = null;
    memoryLoaded = false;
    void loadMemory(true);
  }

  function changeMemoryQuery(query: string) {
    const normalized = query.trim();
    if (normalized === memoryQuery) return;
    memoryQuery = normalized;
    memory = [];
    memoryCursor = null;
    memoryLoaded = false;
    void loadMemory(true);
  }

  function contextQueryPath(cursor?: string | null): string {
    const params = new URLSearchParams({
      limit: "30",
      state: contextState,
    });
    if (contextQuery) params.set("q", contextQuery);
    if (cursor) params.set("cursor", cursor);
    return `${workspacePath("context-items")}?${params}`;
  }

  async function loadContextItems(reset: boolean) {
    if (!sessionId) return;
    const cursor = reset ? null : contextCursor;
    if (!reset && !cursor) return;
    const requestGeneration = generation;
    contextController?.abort();
    const controller = new AbortController();
    contextController = controller;
    contextError = "";
    if (reset) contextLoading = true;
    else contextLoadingMore = true;
    try {
      const page = await api.request<FrontendContextPage>(
        contextQueryPath(cursor),
        { signal: controller.signal },
      );
      if (requestGeneration !== generation) return;
      contextItems = reset
        ? page.items
        : mergeWorkspacePage(contextItems, page.items);
      contextTotal = page.total;
      contextCursor = page.next_cursor;
      contextSearchTruncated = page.search_truncated;
      contextLoaded = true;
    } catch (reason) {
      if (controller.signal.aborted) return;
      contextError =
        reason instanceof Error
          ? reason.message
          : "Context items could not be loaded.";
    } finally {
      if (
        requestGeneration === generation &&
        contextController === controller
      ) {
        contextLoading = false;
        contextLoadingMore = false;
      }
    }
  }

  function changeContextState(
    nextState: "all" | FrontendContextLifecycleState,
  ) {
    if (nextState === contextState) return;
    contextState = nextState;
    contextItems = [];
    contextCursor = null;
    contextLoaded = false;
    contextDetail = null;
    void loadContextItems(true);
  }

  function changeContextQuery(query: string) {
    const normalized = query.trim();
    if (normalized === contextQuery) return;
    contextQuery = normalized;
    contextItems = [];
    contextCursor = null;
    contextLoaded = false;
    contextDetail = null;
    void loadContextItems(true);
  }

  async function loadContextDetail(
    itemId: string,
    append = false,
  ) {
    if (!sessionId) return;
    const requestGeneration = generation;
    const nextOffset =
      append && contextDetail?.item.id === itemId
        ? contextDetail.next_offset
        : 0;
    if (append && nextOffset === null) return;
    contextDetailController?.abort();
    const controller = new AbortController();
    contextDetailController = controller;
    contextDetailError = "";
    if (append) contextDetailLoadingMore = true;
    else {
      contextDetailLoading = true;
      contextDetail = null;
    }
    try {
      const detail = await api.request<FrontendContextDetail>(
        `${workspacePath("context-items")}/${encodeURIComponent(
          itemId,
        )}?start_offset=${nextOffset ?? 0}&max_chars=20000`,
        { signal: controller.signal },
      );
      if (requestGeneration !== generation) return;
      if (append && contextDetail?.item.id === itemId) {
        contextDetail = {
          ...detail,
          content: contextDetail.content + detail.content,
          start_offset: contextDetail.start_offset,
        };
      } else {
        contextDetail = detail;
      }
    } catch (reason) {
      if (controller.signal.aborted) return;
      contextDetailError =
        reason instanceof Error
          ? reason.message
          : "The exact evidence preview could not be loaded.";
    } finally {
      if (
        requestGeneration === generation &&
        contextDetailController === controller
      ) {
        contextDetailLoading = false;
        contextDetailLoadingMore = false;
      }
    }
  }

  function openContextDetail(item: FrontendContextItem) {
    void loadContextDetail(item.id);
  }

  function closeContextDetail() {
    contextDetailController?.abort();
    contextDetail = null;
    contextDetailError = "";
    contextDetailLoading = false;
    contextDetailLoadingMore = false;
  }

  async function updateContextItem(
    item: FrontendContextItem,
    action: string,
  ) {
    if (!sessionId || contextUpdatingId) return;
    contextUpdatingId = item.id;
    contextError = "";
    try {
      const payload = await api.request<FrontendContextUpdate>(
        `${workspacePath("context-items")}/${encodeURIComponent(item.id)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            action,
            expected_version: item.version,
            reason: `User selected ${action} in Context Inspector.`,
          }),
        },
      );
      contextItems = contextItems.map((current) =>
        current.id === payload.item.id ? payload.item : current
      );
      if (contextDetail?.item.id === payload.item.id) {
        contextDetail = { ...contextDetail, item: payload.item };
      }
      await Promise.all([
        loadContextItems(true),
        refreshSummary(false),
      ]);
      announce(`${payload.item.title} updated.`);
    } catch (reason) {
      contextError =
        reason instanceof Error
          ? reason.message
          : "The context transition could not be saved.";
      if (
        reason instanceof Error &&
        reason.message.toLowerCase().includes("version")
      ) {
        await loadContextItems(true);
      }
    } finally {
      contextUpdatingId = null;
    }
  }

  function artifactQueryPath(cursor?: string | null): string {
    const params = new URLSearchParams({
      limit: "24",
      status: artifactStatus,
    });
    if (artifactQuery) params.set("q", artifactQuery);
    if (cursor) params.set("cursor", cursor);
    return `${workspacePath("artifacts")}?${params}`;
  }

  async function loadArtifacts(reset: boolean) {
    if (!sessionId) return;
    const cursor = reset ? null : artifactCursor;
    if (!reset && !cursor) return;
    const requestGeneration = generation;
    artifactsController?.abort();
    const controller = new AbortController();
    artifactsController = controller;
    artifactsError = "";
    if (reset) artifactsLoading = true;
    else artifactsLoadingMore = true;
    try {
      const page = await api.request<FrontendArtifactPage>(
        artifactQueryPath(cursor),
        { signal: controller.signal },
      );
      if (requestGeneration !== generation) return;
      artifacts = reset
        ? page.items
        : mergeWorkspacePage(artifacts, page.items);
      artifactTotal = page.total;
      artifactCursor = page.next_cursor;
      artifactSearchTruncated = page.search_truncated;
      artifactsLoaded = true;
    } catch (reason) {
      if (controller.signal.aborted) return;
      artifactsError =
        reason instanceof Error
          ? reason.message
          : "Artifacts could not be loaded.";
    } finally {
      if (
        requestGeneration === generation &&
        artifactsController === controller
      ) {
        artifactsLoading = false;
        artifactsLoadingMore = false;
      }
    }
  }

  function changeArtifactFilter(status: string) {
    if (status === artifactStatus) return;
    artifactStatus = status;
    artifacts = [];
    artifactCursor = null;
    artifactsLoaded = false;
    artifactDetail = null;
    void loadArtifacts(true);
  }

  function changeArtifactQuery(query: string) {
    const normalized = query.trim();
    if (normalized === artifactQuery) return;
    artifactQuery = normalized;
    artifacts = [];
    artifactCursor = null;
    artifactsLoaded = false;
    artifactDetail = null;
    void loadArtifacts(true);
  }

  async function loadArtifactDetail(
    artifactId: string,
    announceOpen = true,
  ) {
    if (!sessionId) return;
    const requestGeneration = generation;
    detailController?.abort();
    const controller = new AbortController();
    detailController = controller;
    if (artifactDetail?.artifact.id !== artifactId) artifactDetail = null;
    artifactDetailError = "";
    artifactDetailLoading = true;
    activeTab = "artifacts";
    open = true;
    try {
      const detail = await api.request<FrontendArtifactDetail>(
        `${workspacePath("artifacts")}/${encodeURIComponent(artifactId)}`,
        { signal: controller.signal },
      );
      if (requestGeneration !== generation) return;
      artifactDetail = detail;
      if (announceOpen) announce(`${detail.artifact.label} opened.`);
    } catch (reason) {
      if (controller.signal.aborted) return;
      artifactDetailError =
        reason instanceof Error
          ? reason.message
          : "The artifact could not be opened.";
    } finally {
      if (
        requestGeneration === generation &&
        detailController === controller
      ) {
        artifactDetailLoading = false;
      }
    }
  }

  function selectArtifact(artifact: FrontendArtifactSummary) {
    selectTab("artifacts");
    void loadArtifactDetail(artifact.id);
  }

  async function reviewArtifact(status: string, note: string) {
    if (!artifactDetail || reviewingArtifact) return;
    const artifactId = artifactDetail.artifact.id;
    reviewingArtifact = true;
    try {
      await api.request(
        `/api/runtime/artifacts/${encodeURIComponent(
          artifactId,
        )}/review`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            review_status: status,
            review_note: note.trim(),
          }),
        },
      );
      await Promise.all([
        loadArtifactDetail(artifactId, false),
        loadArtifacts(true),
        refreshSummary(false),
      ]);
      announce(`Artifact marked ${status.replaceAll("_", " ")}.`);
      window.dispatchEvent(
        new CustomEvent("iarf:workspace-refresh", {
          detail: {
            session_id: sessionId,
            event: {
              kind: "artifact.reviewed",
              subject: {
                type: "artifact",
                id: artifactId,
              },
            },
          },
        }),
      );
    } catch (reason) {
      artifactDetailError =
        reason instanceof Error
          ? reason.message
          : "The review decision could not be saved.";
    } finally {
      reviewingArtifact = false;
    }
  }

  function startResize(event: PointerEvent) {
    if (isOverlayLayout()) return;
    resizing = true;
    resizeStartX = event.clientX;
    resizeStartWidth = width;
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    window.addEventListener("pointermove", continueResize);
    window.addEventListener("pointerup", stopResize, { once: true });
    window.addEventListener("pointercancel", stopResize, { once: true });
    window.addEventListener("blur", stopResize, { once: true });
    previousBodyCursor = document.body.style.cursor;
    previousBodyUserSelect = document.body.style.userSelect;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }

  function continueResize(event: PointerEvent) {
    if (!resizing) return;
    const delta = resizeStartX - event.clientX;
    width = Math.max(320, Math.min(720, resizeStartWidth + delta));
  }

  function stopResize() {
    if (!resizing) return;
    resizing = false;
    window.removeEventListener("pointermove", continueResize);
    window.removeEventListener("pointerup", stopResize);
    window.removeEventListener("pointercancel", stopResize);
    window.removeEventListener("blur", stopResize);
    document.body.style.cursor = previousBodyCursor;
    document.body.style.userSelect = previousBodyUserSelect;
    writeStorage("iarf:context-inspector-width", String(Math.round(width)));
  }

  function resizeWithKeyboard(event: KeyboardEvent) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const delta = event.key === "ArrowLeft" ? 16 : -16;
    width = Math.max(320, Math.min(720, width + delta));
    writeStorage("iarf:context-inspector-width", String(Math.round(width)));
  }
</script>

{#if open}
  <button
    class="context-inspector__backdrop"
    type="button"
    aria-label="Close Context Inspector"
    onclick={closeInspector}
  ></button>
{/if}

<div
  class="context-inspector-frame"
  class:is-open={open}
  class:is-expanded={expanded}
  style={`--context-inspector-width:${inspectorWidth}px`}
>
  {#if open}
    <aside
      class="context-inspector"
      aria-labelledby="context-inspector-title"
      aria-busy={summaryLoading && !summary}
    >
      <button
        type="button"
        class="context-inspector__resize"
        aria-label={`Resize Context Inspector, ${Math.round(width)} pixels wide`}
        title="Drag to resize; use the left and right arrow keys for precise adjustments"
        onpointerdown={startResize}
        onkeydown={resizeWithKeyboard}
      ></button>

      <header class="context-inspector__header">
        <div>
          <p>Conversation workspace</p>
          <h2 id="context-inspector-title">Context Inspector</h2>
        </div>
        <div>
          <button
            type="button"
            title="Refresh workspace"
            aria-label="Refresh workspace"
            disabled={summaryLoading}
            onclick={() => void refreshSummary(false)}
          >
            <Icon name="refresh" size={14} />
          </button>
          <button
            type="button"
            title={expanded ? "Restore inspector width" : "Expand inspector"}
            aria-label={expanded ? "Restore inspector width" : "Expand inspector"}
            aria-pressed={expanded}
            onclick={() => (expanded = !expanded)}
          >
            <Icon
              name={expanded ? "chevron-right" : "chevron-left"}
              size={14}
            />
          </button>
          <button
            type="button"
            title="Close inspector"
            aria-label="Close Context Inspector"
            onclick={closeInspector}
          >
            <Icon name="close" size={14} />
          </button>
        </div>
      </header>

      <div
        class="context-inspector__tabs"
        role="tablist"
        aria-label="Context Inspector views"
        tabindex="-1"
        onkeydown={handleTabKeydown}
      >
        {#each tabs as tab (tab.id)}
          <button
            id={`context-tab-${tab.id}`}
            type="button"
            role="tab"
            aria-selected={activeTab === tab.id}
            aria-controls={`context-panel-${tab.id}`}
            tabindex={activeTab === tab.id ? 0 : -1}
            class:is-active={activeTab === tab.id}
            onclick={() => selectTab(tab.id)}
          >
            <Icon name={tab.icon} size={13} />
            <span>{tab.label}</span>
            {#if tab.id === "runs" && (summary?.counts.runs ?? 0)}
              <i>{summary?.counts.runs}</i>
            {:else if tab.id === "memory" && memoryCount}
              <i>{memoryCount}</i>
            {:else if tab.id === "context" && (summary?.counts.context_items ?? 0)}
              <i>{summary?.counts.context_items}</i>
            {:else if tab.id === "artifacts" && (summary?.counts.artifacts ?? 0)}
              <i>{summary?.counts.artifacts}</i>
            {/if}
          </button>
        {/each}
      </div>

      <div class="context-inspector__panels">
        {#if activeTab === "overview"}
          <OverviewPanel
            {summary}
            loading={summaryLoading}
            error={summaryError}
            onselecttab={selectTab}
            onopenartifact={selectArtifact}
            onrefresh={() => void refreshSummary(false)}
            onnavigate={navigateRun}
          />
        {:else if activeTab === "runs"}
          <RunsPanel
            items={runs}
            total={runTotal}
            nextCursor={runCursor}
            loading={runsLoading}
            loadingMore={runsLoadingMore}
            error={runsError}
            view={runView}
            onchangeview={changeRunView}
            onloadmore={() => void loadRuns(false)}
            onrefresh={() => void loadRuns(true)}
            onnavigate={navigateRun}
          />
        {:else if activeTab === "memory"}
          <MemoryPanel
            items={memory}
            total={memoryTotal}
            nextCursor={memoryCursor}
            loading={memoryLoading}
            loadingMore={memoryLoadingMore}
            error={memoryError}
            scope={memoryScope}
            query={memoryQuery}
            onchangescope={changeMemoryScope}
            onquery={changeMemoryQuery}
            onloadmore={() => void loadMemory(false)}
            onrefresh={() => void loadMemory(true)}
          />
        {:else if activeTab === "context"}
          <ContextItemsPanel
            items={contextItems}
            total={contextTotal}
            nextCursor={contextCursor}
            loading={contextLoading}
            loadingMore={contextLoadingMore}
            error={contextError}
            lifecycleState={contextState}
            query={contextQuery}
            searchTruncated={contextSearchTruncated}
            detail={contextDetail}
            detailLoading={contextDetailLoading}
            detailLoadingMore={contextDetailLoadingMore}
            detailError={contextDetailError}
            updatingId={contextUpdatingId}
            onchangestate={changeContextState}
            onquery={changeContextQuery}
            onloadmore={() => void loadContextItems(false)}
            onrefresh={() => void loadContextItems(true)}
            onopen={openContextDetail}
            onclosedetail={closeContextDetail}
            onloaddetailmore={() => {
              if (contextDetail) {
                void loadContextDetail(contextDetail.item.id, true);
              }
            }}
            onaction={(item, action) => void updateContextItem(item, action)}
          />
        {:else}
          <ArtifactsPanel
            {api}
            items={artifacts}
            total={artifactTotal}
            nextCursor={artifactCursor}
            searchTruncated={artifactSearchTruncated}
            loading={artifactsLoading}
            loadingMore={artifactsLoadingMore}
            error={artifactsError}
            status={artifactStatus}
            query={artifactQuery}
            detail={artifactDetail}
            detailLoading={artifactDetailLoading}
            detailError={artifactDetailError}
            reviewing={reviewingArtifact}
            onchangefilter={changeArtifactFilter}
            onquery={changeArtifactQuery}
            onloadmore={() => void loadArtifacts(false)}
            onrefresh={() => void loadArtifacts(true)}
            onselect={selectArtifact}
            onclose={() => {
              detailController?.abort();
              artifactDetail = null;
              artifactDetailError = "";
              artifactDetailLoading = false;
            }}
            onreview={(status, note) => void reviewArtifact(status, note)}
          />
        {/if}
      </div>

      <p class="context-inspector__status">
        {#if summaryError && summary}
          Updates paused · {summaryError}
        {:else if summary?.updated_at}
          Live workspace · refreshes while visible
        {:else}
          Ready for conversation resources
        {/if}
      </p>
    </aside>
  {:else}
    <button
      class="context-inspector__trigger"
      type="button"
      aria-label="Open Context Inspector"
      title="Open Context Inspector"
      onclick={openInspector}
    >
      <Icon name="workspaces" size={16} />
      <span>Context</span>
      {#if (summary?.counts.attention ?? 0) > 0}
        <i>{summary?.counts.attention}</i>
      {/if}
    </button>
  {/if}
</div>

<p class="sr-only" aria-live="polite">{announcement}</p>

<style>
  .context-inspector-frame {
    position: relative;
    z-index: 16;
    width: 2.7rem;
    min-width: 0;
    height: 100%;
    overflow: visible;
    border-left: 1px solid var(--color-rule);
    background: var(--color-surface-muted);
    transition: width 180ms ease;
  }

  .context-inspector-frame.is-open {
    width: min(
      var(--context-inspector-width),
      calc(100vw - var(--shell-sidebar-rail-width) - 20rem)
    );
    background: var(--color-paper);
  }

  .context-inspector {
    position: relative;
    width: 100%;
    height: 100%;
    min-width: 0;
    display: grid;
    grid-template-rows: auto auto minmax(0, 1fr) auto;
    border-left: 1px solid var(--color-rule-strong);
    background: var(--color-paper);
    box-shadow: -0.6rem 0 1.8rem color-mix(in srgb, var(--color-ink) 6%, transparent);
  }

  .context-inspector__resize {
    position: absolute;
    z-index: 4;
    top: 0;
    bottom: 0;
    left: -4px;
    width: 8px;
    border: 0;
    padding: 0;
    background: transparent;
    cursor: col-resize;
    outline: none;
  }

  .context-inspector__resize::after {
    position: absolute;
    top: 0;
    bottom: 0;
    left: 3px;
    width: 1px;
    background: transparent;
    content: "";
  }

  .context-inspector__resize:hover::after,
  .context-inspector__resize:focus-visible::after {
    background: var(--color-focus);
  }

  .context-inspector__header {
    min-height: 4.6rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.8rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.55rem 0.7rem 0.5rem 0.9rem;
    background: color-mix(in srgb, var(--color-paper) 92%, transparent);
  }

  .context-inspector__header p {
    margin: 0 0 0.15rem;
    color: var(--color-clay);
    font-size: 0.5rem;
    font-weight: 750;
    letter-spacing: 0.13em;
    text-transform: uppercase;
  }

  .context-inspector__header h2 {
    margin: 0;
    font-family: var(--font-editorial);
    font-size: 1.08rem;
    font-weight: 540;
    line-height: 1;
  }

  .context-inspector__header > div:last-child {
    display: flex;
    gap: 0.2rem;
  }

  .context-inspector__header button {
    width: 1.9rem;
    height: 1.9rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-rule);
    padding: 0;
    background: var(--color-surface);
    color: var(--color-ink-muted);
    cursor: pointer;
  }

  .context-inspector__header button:hover {
    border-color: var(--color-rule-strong);
    color: var(--color-ink);
  }

  .context-inspector__header button:disabled {
    cursor: wait;
    opacity: 0.55;
  }

  .context-inspector__tabs {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    border-bottom: 1px solid var(--color-rule);
    background: var(--color-surface);
  }

  .context-inspector__tabs button {
    position: relative;
    min-width: 0;
    min-height: 3rem;
    display: grid;
    grid-template-columns: auto minmax(0, auto) auto;
    align-items: center;
    justify-content: center;
    gap: 0.24rem;
    border: 0;
    padding: 0 0.18rem;
    background: transparent;
    color: var(--color-ink-muted);
    cursor: pointer;
    font-size: 0.54rem;
  }

  .context-inspector__tabs button::after {
    position: absolute;
    right: 0.25rem;
    bottom: -1px;
    left: 0.25rem;
    height: 2px;
    background: transparent;
    content: "";
  }

  .context-inspector__tabs button:hover,
  .context-inspector__tabs button.is-active {
    color: var(--color-clay);
  }

  .context-inspector__tabs button.is-active::after {
    background: var(--color-clay);
  }

  .context-inspector__tabs button i {
    min-width: 1rem;
    height: 1rem;
    display: grid;
    place-items: center;
    background: var(--color-surface-muted);
    color: var(--color-ink-muted);
    font-size: 0.45rem;
    font-style: normal;
  }

  .context-inspector__panels {
    min-height: 0;
    overflow-y: auto;
    overscroll-behavior: contain;
    scrollbar-gutter: stable;
  }

  .context-inspector__status {
    min-height: 1.7rem;
    display: flex;
    align-items: center;
    border-top: 1px solid var(--color-rule);
    margin: 0;
    padding: 0 0.65rem;
    background: var(--color-surface-muted);
    color: var(--color-ink-muted);
    font-size: 0.48rem;
  }

  .context-inspector__trigger {
    width: 100%;
    height: 100%;
    display: flex;
    align-items: center;
    justify-content: flex-start;
    gap: 0.5rem;
    border: 0;
    padding: 0.8rem 0;
    background: var(--color-surface-muted);
    color: var(--color-ink-muted);
    cursor: pointer;
    writing-mode: vertical-rl;
  }

  .context-inspector__trigger:hover {
    background: var(--color-clay-muted);
    color: var(--color-clay);
  }

  .context-inspector__trigger span {
    font-size: 0.52rem;
    font-weight: 750;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .context-inspector__trigger i {
    min-width: 1.1rem;
    height: 1.1rem;
    display: grid;
    place-items: center;
    background: var(--color-tobacco);
    color: var(--color-surface);
    font-size: 0.48rem;
    font-style: normal;
    writing-mode: horizontal-tb;
  }

  .context-inspector__backdrop {
    display: none;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  @media (max-width: 59.99rem) {
    .context-inspector__backdrop {
      position: absolute;
      z-index: 14;
      inset: 0;
      display: block;
      border: 0;
      background: color-mix(in srgb, var(--color-ink) 26%, transparent);
      backdrop-filter: blur(2px);
    }

    .context-inspector-frame {
      position: absolute;
      z-index: 16;
      top: 0;
      right: 0;
      bottom: 0;
      width: 2.7rem;
      border-left: 1px solid var(--color-rule);
      background: var(--color-surface-muted);
      pointer-events: auto;
    }

    .context-inspector-frame.is-open {
      width: min(36rem, calc(100% - 2.5rem));
      pointer-events: auto;
    }

    .context-inspector__trigger {
      position: static;
      width: 100%;
      height: 100%;
      border: 0;
      padding: 0.8rem 0;
      background: var(--color-surface-muted);
      box-shadow: none;
      pointer-events: auto;
      writing-mode: vertical-rl;
    }

    .context-inspector__trigger span {
      letter-spacing: 0.12em;
    }

    .context-inspector__resize {
      display: none;
    }
  }

  @media (max-width: 42rem) {
    .context-inspector-frame.is-open {
      width: 100%;
    }

    .context-inspector__backdrop {
      display: none;
    }

    .context-inspector {
      box-shadow: none;
    }

    .context-inspector__header {
      min-height: 4rem;
      padding-left: 0.7rem;
    }

    .context-inspector__tabs button {
      min-height: 2.8rem;
    }

  }

  @media (prefers-reduced-motion: reduce) {
    .context-inspector-frame {
      transition: none;
    }
  }
</style>
