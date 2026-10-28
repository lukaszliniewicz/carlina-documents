<script lang="ts">
  import {
    onDestroy,
    onMount,
    tick,
    untrack,
    type Component,
  } from "svelte";
  import type {
    FrontendApiClient,
    FrontendAssistantPolicyDashboard,
    FrontendBootstrap,
    FrontendCapability,
    FrontendChatHistoryPage,
    FrontendChatModel,
    FrontendChatSession,
    FrontendChatTimelineItem,
    FrontendRuntimeInteraction,
    FrontendSlashCommand,
  } from "@carlina/sdk";
  import { createAddonApiClient } from "@carlina/sdk";
  import { Icon } from "@carlina/ui";

  import ChatComposer from "../components/chat/ChatComposer.svelte";
  import type { ChatSkill } from "../lib/skill-picker.ts";
  import ChatTimeline from "../components/chat/ChatTimeline.svelte";
  import RuntimeInteractionCard from "../components/chat/RuntimeInteractionCard.svelte";
  import {
    chatDraftKey,
    chatRuntimePolicyPayload,
    chatRuntimePolicyStorageKey,
    effectiveChatRuntimePolicy,
    mergeChatHistory,
    normalizeInlineSearchResults,
    parseSseChunk,
    parseChatRuntimePolicyOverride,
    persistedChatComposerConfig,
    reconcileChatTimeline,
    resolveInlineSearchRequestTarget,
    streamPacketCommitsComposer,
    type ChatComposerConfig,
    type ComposerAttachment,
    type InlineComposerSuggestion,
  } from "../lib/chat-state.ts";

  interface Props {
    api: FrontendApiClient;
    bootstrap: FrontendBootstrap;
    route: string;
    navigate: (href: string, clientRoute?: boolean) => void;
  }

  interface StreamPacket {
    type?: string;
    message?: string;
    session_id?: string;
    turn_id?: string;
    user_message_id?: number | null;
    response?: string;
    markdown_content?: string;
    event?: Record<string, unknown>;
    loop_index?: number;
    usage_metadata?: {
      total?: number;
    };
    cost?: number | string | null;
  }

  interface VoiceOption {
    id: string | number;
    name?: string;
    display_name?: string;
    mode?: string;
  }

  interface ContextInspectorProps {
    api: FrontendApiClient;
    sessionId: string | null;
    navigate: (href: string, clientRoute?: boolean) => void;
  }

  let { api, bootstrap, route, navigate }: Props = $props();

  const defaultConfig: ChatComposerConfig = {
    model: "",
    thinkingLevel: "default",
    assistantMode: "default",
    capabilityIds: [],
    runtimeProfileIds: ["python.scratch", "python.session"],
    enableDelegation: true,
    runtimePolicy: "review",
    runtimePolicySource: "administrator",
    contextMode: "assisted",
    voiceModel: "",
    voice: "",
  };

  let session = $state<FrontendChatSession | null>(null);
  let items = $state<FrontendChatTimelineItem[]>([]);
  let nextCursor = $state<string | null>(null);
  let hasMore = $state(false);
  let loading = $state(false);
  let loadingOlder = $state(false);
  let busy = $state(false);
  let uploading = $state(false);
  let error = $state("");
  let status = $state("");
  let announcement = $state("");
  let debug = $state(false);
  let followRevision = $state(0);
  let draft = $state("");
  let draftOwner = $state<string | null>(null);
  let draftHydrated = $state(false);
  let attachments = $state<ComposerAttachment[]>([]);
  let config = $state<ChatComposerConfig>({ ...defaultConfig });
  let configHydrated = $state(false);
  let administratorApprovalMode = $state<"review" | "full_access">("review");
  let models = $state<FrontendChatModel[]>([]);
  let capabilities = $state<FrontendCapability[]>([]);
  let commands = $state<FrontendSlashCommand[]>([]);
  let skills = $state<ChatSkill[]>([]);
  let optionsLoading = $state(true);
  let voiceModels = $state<VoiceOption[]>([]);
  let voices = $state<VoiceOption[]>([]);
  let voiceLoading = $state(false);
  let ContextInspector = $state<Component<ContextInspectorProps> | null>(null);
  let contextInspectorLoading = $state(false);
  let contextInspectorPromise: Promise<Component<ContextInspectorProps>> | null =
    null;
  let sessionMenuOpen = $state(false);
  let renameOpen = $state(false);
  let renameValue = $state("");
  let requestController: AbortController | null = null;
  let voiceController: AbortController | null = null;
  let activeAudio: HTMLAudioElement | null = null;
  let historyRequest = 0;
  let liveSequence = 1_000_000;
  const pendingComposerInsertionKey = "iarf:pending-composer-insertion";

  const requestedSessionId = $derived(
    new URL(route, "https://iarf.local").searchParams.get("chat"),
  );
  const contextInspectorEnabled = $derived(
    bootstrap.feature_flags["frontend.context_inspector"] !== false,
  );
  const contextSessionId = $derived(session?.id ?? requestedSessionId);
  const pendingInteractions = $derived(
    (session?.interactions ?? []).filter(
      (interaction) => interaction.status === "pending",
    ),
  );
  const pageTitle = $derived(session?.title ?? "New conversation");

  function readLocalStorage(key: string): string | null {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  }

  function writeLocalStorage(key: string, value: string) {
    try {
      localStorage.setItem(key, value);
    } catch {
      // Storage can be unavailable or full; the in-memory draft still works.
    }
  }

  function removeLocalStorage(key: string) {
    try {
      localStorage.removeItem(key);
    } catch {
      // Storage can be unavailable or full; the in-memory preference still works.
    }
  }

  function applyConversationRuntimePolicy(sessionId: string | null): void {
    const override = parseChatRuntimePolicyOverride(
      readLocalStorage(chatRuntimePolicyStorageKey(sessionId)),
    );
    config = {
      ...config,
      ...effectiveChatRuntimePolicy(administratorApprovalMode, override),
    };
  }

  function setConversationRuntimePolicy(
    profile: "review" | "full_access" | null,
  ): void {
    const sessionId = session?.id ?? requestedSessionId;
    const key = chatRuntimePolicyStorageKey(sessionId);
    if (profile) {
      writeLocalStorage(key, JSON.stringify({ profile }));
    } else {
      removeLocalStorage(key);
    }
    config = {
      ...config,
      ...effectiveChatRuntimePolicy(
        administratorApprovalMode,
        profile ? { profile } : null,
      ),
    };
  }

  function moveConversationRuntimePolicy(
    fromSessionId: string | null,
    toSessionId: string,
  ): void {
    if (fromSessionId === toSessionId) return;
    const fromKey = chatRuntimePolicyStorageKey(fromSessionId);
    const toKey = chatRuntimePolicyStorageKey(toSessionId);
    const override = parseChatRuntimePolicyOverride(readLocalStorage(fromKey));
    if (!override) return;
    writeLocalStorage(toKey, JSON.stringify(override));
    removeLocalStorage(fromKey);
  }

  function applyAdministratorPolicy(
    policy: FrontendAssistantPolicyDashboard,
  ): void {
    administratorApprovalMode = policy.runtime.approval_mode;
    if (config.runtimePolicySource !== "conversation") {
      applyConversationRuntimePolicy(session?.id ?? requestedSessionId);
    }
  }

  async function openContextInspector(
    detail: Readonly<Record<string, unknown>> = {},
  ): Promise<void> {
    if (!contextInspectorEnabled) return;
    contextInspectorLoading = true;
    try {
      contextInspectorPromise ??= import(
        "../components/context/ContextInspector.svelte"
      ).then((module) => module.default);
      ContextInspector = await contextInspectorPromise;
      await tick();
      window.dispatchEvent(
        new CustomEvent("iarf:open-context", { detail }),
      );
    } catch (reason) {
      contextInspectorPromise = null;
      announce(
        reason instanceof Error
          ? reason.message
          : "The Context Inspector could not be loaded.",
      );
    } finally {
      contextInspectorLoading = false;
    }
  }

  $effect(() => {
    const requested = requestedSessionId;
    void untrack(() => switchConversation(requested));
  });

  $effect(() => {
    if (typeof localStorage === "undefined") return;
    if (!draftHydrated) return;
    writeLocalStorage(chatDraftKey(draftOwner), draft);
  });

  $effect(() => {
    if (typeof localStorage === "undefined") return;
    if (!configHydrated) return;
    writeLocalStorage(
      "iarf:chat-composer-config",
      JSON.stringify(persistedChatComposerConfig(config)),
    );
  });

  onMount(() => {
    const handleComposerInsertion = (event: Event) => {
      const text = (event as CustomEvent<{ text?: unknown }>).detail?.text;
      if (typeof text !== "string" || !text.trim()) return;
      localStorage.removeItem(pendingComposerInsertionKey);
      appendToComposer(text);
    };
    const handleLazyContextOpen = (event: Event) => {
      if (ContextInspector || !contextInspectorEnabled) return;
      event.stopImmediatePropagation();
      const detail = (
        event as CustomEvent<Readonly<Record<string, unknown>>>
      ).detail;
      void openContextInspector(detail ?? {});
    };
    window.addEventListener(
      "iarf:add-to-composer",
      handleComposerInsertion,
    );
    window.addEventListener(
      "iarf:open-context",
      handleLazyContextOpen,
    );
    try {
      const stored = JSON.parse(
        readLocalStorage("iarf:chat-composer-config") || "{}",
      ) as Partial<ChatComposerConfig>;
      const {
        runtimePolicy: _legacyRuntimePolicy,
        runtimePolicySource: _legacyRuntimePolicySource,
        ...storedConfig
      } = stored;
      config = {
        ...defaultConfig,
        ...storedConfig,
        capabilityIds: Array.isArray(storedConfig.capabilityIds)
          ? storedConfig.capabilityIds.map(String)
          : [],
        runtimeProfileIds: Array.isArray(storedConfig.runtimeProfileIds)
          ? storedConfig.runtimeProfileIds.map(String)
          : [...defaultConfig.runtimeProfileIds],
      };
    } catch {
      config = { ...defaultConfig };
    }
    configHydrated = true;
    applyConversationRuntimePolicy(requestedSessionId);
    void loadComposerOptions();
    return () => {
      requestController?.abort();
      window.removeEventListener(
        "iarf:add-to-composer",
        handleComposerInsertion,
      );
      window.removeEventListener(
        "iarf:open-context",
        handleLazyContextOpen,
      );
    };
  });

  onDestroy(() => {
    voiceController?.abort();
    activeAudio?.pause();
    if ("speechSynthesis" in window) window.speechSynthesis.cancel();
  });

  function announce(message: string) {
    announcement = "";
    queueMicrotask(() => {
      announcement = message;
    });
  }

  function appendToComposer(text: string): void {
    const insertion = text.trim();
    if (!insertion) return;
    draft = draft.trim()
      ? `${draft.replace(/\s+$/u, "")}\n\n${insertion}`
      : insertion;
    announce("Selected material added to the message composer.");
  }

  function consumePendingComposerInsertion(): void {
    const pending = readLocalStorage(pendingComposerInsertionKey);
    if (!pending) return;
    localStorage.removeItem(pendingComposerInsertionKey);
    appendToComposer(pending);
  }

  function historyUrl(sessionId: string, before?: string | null): string {
    const url = new URL(
      `/api/frontend/chats/${encodeURIComponent(sessionId)}/history`,
      "https://iarf.local",
    );
    url.searchParams.set("limit", "60");
    if (before) url.searchParams.set("before", before);
    if (debug) url.searchParams.set("debug", "1");
    return `${url.pathname}${url.search}`;
  }

  async function switchConversation(sessionId: string | null) {
    const requestId = ++historyRequest;
    if (typeof localStorage !== "undefined") {
      if (draftHydrated) {
        writeLocalStorage(chatDraftKey(draftOwner), draft);
      }
      draftOwner = sessionId;
      draft = readLocalStorage(chatDraftKey(sessionId)) ?? "";
      draftHydrated = true;
      consumePendingComposerInsertion();
    } else {
      draftOwner = sessionId;
      draft = "";
    }
    if (configHydrated) applyConversationRuntimePolicy(sessionId);
    attachments = [];
    sessionMenuOpen = false;
    renameOpen = false;
    error = "";
    if (!busy) status = "";

    if (!sessionId) {
      session = null;
      items = [];
      nextCursor = null;
      hasMore = false;
      loading = false;
      followRevision += 1;
      return;
    }
    loading = true;
    try {
      const page = await api.request<FrontendChatHistoryPage>(
        historyUrl(sessionId),
      );
      if (requestId !== historyRequest) return;
      applyHistoryPage(page);
      followRevision += 1;
    } catch (reason) {
      if (requestId !== historyRequest) return;
      session = null;
      items = [];
      error =
        reason instanceof Error
          ? reason.message
          : "The conversation could not be loaded.";
    } finally {
      if (requestId === historyRequest) loading = false;
    }
  }

  function applyHistoryPage(page: FrontendChatHistoryPage) {
    session = page.session;
    items = reconcileChatTimeline(page.items);
    nextCursor = page.next_cursor;
    hasMore = page.has_more;
    const scope = page.session.assistant_scope;
    if (scope.mode === "direct" || scope.mode === "default") {
      config = {
        ...config,
        assistantMode: scope.mode,
        capabilityIds: Array.isArray(scope.capability_ids)
          ? scope.capability_ids.map(String)
          : config.capabilityIds,
      };
    }
  }

  async function reloadConversation(sessionId = session?.id ?? requestedSessionId) {
    if (!sessionId) return;
    const page = await api.request<FrontendChatHistoryPage>(
      historyUrl(sessionId),
    );
    applyHistoryPage(page);
    followRevision += 1;
  }

  async function loadOlder() {
    if (!session || !nextCursor || loadingOlder) return;
    loadingOlder = true;
    try {
      const page = await api.request<FrontendChatHistoryPage>(
        historyUrl(session.id, nextCursor),
      );
      items = mergeChatHistory(page.items, items);
      nextCursor = page.next_cursor;
      hasMore = page.has_more;
      session = page.session;
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "Earlier messages could not be loaded.";
    } finally {
      loadingOlder = false;
    }
  }

  async function loadComposerOptions() {
    optionsLoading = true;
    const [modelResult, capabilityResult, commandResult, skillResult, policyResult] =
      await Promise.allSettled([
        api.request<FrontendChatModel[]>("/api/llm/models"),
        api.request<FrontendCapability[]>("/api/capabilities"),
        api.request<FrontendSlashCommand[]>("/api/slash-commands"),
        api.request<Array<Record<string, unknown>>>("/api/skills"),
        api.request<FrontendAssistantPolicyDashboard>(
          "/api/frontend/settings/policy",
        ),
      ]);

    if (modelResult.status === "fulfilled") {
      models = modelResult.value;
      if (!config.model) {
        const preferred =
          models.find((item) => item.is_preset_powerful) ??
          models.find((item) => item.is_preset_medium) ??
          models[0];
        if (preferred) config = { ...config, model: preferred.id };
      }
    }
    if (capabilityResult.status === "fulfilled") {
      capabilities = capabilityResult.value
        .map((item) => ({
          ...item,
          id: String(item.id ?? item.capability_id ?? ""),
        }))
        .filter((item) => item.id);
    }
    if (skillResult.status === "fulfilled") {
      skills = skillResult.value.flatMap((skill) => {
        const id = typeof skill.id === "string" ? skill.id : "";
        if (!id) return [];
        return [{
          id,
          name: typeof skill.name === "string" ? skill.name : id,
          description: typeof skill.description === "string" ? skill.description : "No description provided.",
          source: typeof skill.addon_id === "string" ? skill.addon_id : "Personal skill",
        }];
      });
    }
    if (policyResult.status === "fulfilled") {
      applyAdministratorPolicy(policyResult.value);
    }
    const nativeCommands: FrontendSlashCommand[] = [
      { command: "fork", description: "Fork this conversation" },
      { command: "compress", description: "Compact older tool context" },
      { command: "new", description: "Begin a new conversation" },
    ];
    const allCommands = [
      ...nativeCommands,
      ...(commandResult.status === "fulfilled" ? commandResult.value : []),
    ];
    commands = allCommands.filter(
      (command, index) =>
        allCommands.findIndex(
          (candidate) =>
            candidate.command.toLocaleLowerCase() ===
            command.command.toLocaleLowerCase(),
        ) === index,
    );
    optionsLoading = false;
  }

  async function loadVoiceOptions() {
    voiceLoading = true;
    try {
      const [modelResponse, voiceResponse] = await Promise.all([
        fetch("/addons/audio_core/api/models?mode=tts", {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        }),
        fetch("/addons/audio_core/api/voices", {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        }),
      ]);
      voiceModels = modelResponse.ok
        ? ((await modelResponse.json()) as VoiceOption[])
        : [];
      voices = voiceResponse.ok
        ? ((await voiceResponse.json()) as VoiceOption[])
        : [];
    } catch {
      voiceModels = [];
      voices = [];
    } finally {
      voiceLoading = false;
    }
  }

  function defaultAttachmentStrategy(file: File): string {
    return file.type === "application/pdf" ? "pdf_native" : "full_content";
  }

  async function uploadFiles(files: File[]) {
    uploading = true;
    error = "";
    const settled = await Promise.allSettled(
      files.map(async (file) => {
        const formData = new FormData();
        formData.append("file", file);
        const response = await fetch("/api/upload", {
          method: "POST",
          credentials: "same-origin",
          body: formData,
        });
        const payload = (await response.json().catch(() => ({}))) as {
          file_path?: string;
          filename?: string;
          error?: string;
        };
        if (!response.ok || !payload.file_path) {
          throw new Error(payload.error || `${file.name} could not be uploaded.`);
        }
        return {
          id: `${payload.file_path}:${crypto.randomUUID()}`,
          filePath: payload.file_path,
          fileName: payload.filename || file.name,
          fileSize: file.size,
          mimeType: file.type || "application/octet-stream",
          pass_as: defaultAttachmentStrategy(file),
          source: "local-upload",
        } satisfies ComposerAttachment;
      }),
    );
    const uploaded = settled
      .filter(
        (result): result is PromiseFulfilledResult<ComposerAttachment> =>
          result.status === "fulfilled",
      )
      .map((result) => result.value);
    attachments = [...attachments, ...uploaded];
    const failed = settled.filter(
      (result): result is PromiseRejectedResult => result.status === "rejected",
    );
    if (failed.length) {
      error = failed
        .map((result) =>
          result.reason instanceof Error
            ? result.reason.message
            : "A file could not be uploaded.",
        )
        .join(" ");
    } else if (uploaded.length) {
      announce(
        `${uploaded.length} ${uploaded.length === 1 ? "file" : "files"} attached.`,
      );
    }
    uploading = false;
  }

  function removeAttachment(id: string) {
    attachments = attachments.filter((item) => item.id !== id);
  }

  function updateAttachmentStrategy(id: string, strategy: string) {
    attachments = attachments.map((item) =>
      item.id === id ? { ...item, pass_as: strategy } : item,
    );
  }

  function escapeLiveMarkdown(value: string): string {
    const escaped = value
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
    return `<p>${escaped.replaceAll("\n", "<br>")}</p>`;
  }

  function optimisticUserMessage(
    text: string,
    sentAttachments: ComposerAttachment[],
  ): FrontendChatTimelineItem {
    const stamp = Date.now();
    return {
      id: `optimistic:${stamp}`,
      message_id: stamp,
      sequence: liveSequence++,
      created_at: new Date().toISOString(),
      role: "user",
      text,
      attachments: sentAttachments.map((attachment) => ({
        id: attachment.id,
        name: attachment.fileName,
        url: attachment.filePath,
        size: attachment.fileSize,
        media_type: attachment.mimeType,
        strategy: attachment.pass_as,
        source: attachment.source,
      })),
    };
  }

  function newClientTurnId(): string {
    if (typeof globalThis.crypto?.randomUUID === "function") {
      return globalThis.crypto.randomUUID();
    }
    const hex = Array.from(
      { length: 32 },
      () => Math.floor(Math.random() * 16).toString(16),
    );
    hex[12] = "4";
    hex[16] = ((Number.parseInt(hex[16] ?? "0", 16) & 3) | 8).toString(16);
    return [
      hex.slice(0, 8).join(""),
      hex.slice(8, 12).join(""),
      hex.slice(12, 16).join(""),
      hex.slice(16, 20).join(""),
      hex.slice(20).join(""),
    ].join("-");
  }

  function acceptStreamPacket(
    packet: StreamPacket,
    liveId: number,
  ): string | null {
    if (packet.type === "turn_accepted") {
      status = String(packet.message || "Request saved · working…");
      return packet.session_id ?? null;
    }
    if (packet.type === "status") {
      status = String(packet.message || "Working…");
      return packet.session_id ?? null;
    }
    if (packet.type === "assistant_message") {
      const text = String(packet.markdown_content ?? "");
      const item: FrontendChatTimelineItem = {
        id: `live-assistant:${packet.loop_index ?? liveId}`,
        message_id: 2_000_000_000 + liveId,
        sequence: liveSequence++,
        created_at: new Date().toISOString(),
        role: "assistant",
        text,
        html: escapeLiveMarkdown(text),
        tts_audio_url: null,
      };
      items = mergeChatHistory(items, [item]);
      followRevision += 1;
      return null;
    }
    if (packet.type === "agent_event" && packet.event?.kind) {
      const eventId = String(packet.event.id ?? `live-event:${liveId}`);
      const item: FrontendChatTimelineItem = {
        id: eventId,
        message_id: 2_100_000_000 + liveId,
        sequence: liveSequence++,
        created_at: new Date().toISOString(),
        role: "event",
        event: {
          ...packet.event,
          kind: String(packet.event.kind),
          id: eventId,
        },
      };
      items = mergeChatHistory(items, [item]);
      followRevision += 1;
      if (contextInspectorEnabled) {
        window.dispatchEvent(
          new CustomEvent("iarf:workspace-event", {
            detail: {
              session_id:
                packet.session_id ?? session?.id ?? requestedSessionId,
              event: packet.event,
            },
          }),
        );
      }
      return null;
    }
    if (packet.type === "error") {
      throw new Error(String(packet.message || "The response failed."));
    }
    if (packet.type === "final") {
      const tokens = Number(packet.usage_metadata?.total ?? 0);
      const cost =
        packet.cost === null || packet.cost === undefined
          ? ""
          : ` · $${Number(packet.cost).toFixed(4)}`;
      status = tokens ? `${tokens.toLocaleString()} tokens${cost}` : "Complete";
      if (contextInspectorEnabled) {
        window.dispatchEvent(
          new CustomEvent("iarf:workspace-refresh", {
            detail: {
              session_id:
                packet.session_id ?? session?.id ?? requestedSessionId,
            },
          }),
        );
      }
      return packet.session_id ?? null;
    }
    return null;
  }

  async function streamChat(
    extra: Readonly<Record<string, unknown>>,
    options: {
      optimistic?: boolean;
      clearComposer?: boolean;
    } = {},
  ) {
    if (busy) return;
    const originalDraft = draft;
    const prompt = originalDraft.trim();
    const sentAttachments = [...attachments];
    const shouldOptimisticallyAdd = options.optimistic !== false;
    const shouldClearComposer = options.clearComposer !== false;
    if (
      shouldOptimisticallyAdd &&
      !prompt &&
      !sentAttachments.length
    ) {
      return;
    }

    const initialSessionId = session?.id ?? requestedSessionId;
    const turnId = newClientTurnId();
    let optimisticId: string | null = null;
    if (shouldOptimisticallyAdd) {
      const optimistic = optimisticUserMessage(prompt, sentAttachments);
      optimisticId = optimistic.id;
      items = mergeChatHistory(items, [optimistic]);
      followRevision += 1;
    }

    busy = true;
    error = "";
    status = extra.resume_interaction_id
      ? "Resuming the suspended turn…"
      : "Preparing the response…";
    requestController = new AbortController();
    let responseCommitted = false;
    let completed = false;
    let resolvedSessionId = initialSessionId;
    let acceptedNavigationDone = false;
    let packetIndex = 0;
    const sentAttachmentIds = new Set(
      sentAttachments.map((attachment) => attachment.id),
    );

    function commitComposerForPacket(packet: StreamPacket): void {
      if (
        responseCommitted ||
        !streamPacketCommitsComposer(packet.type)
      ) {
        return;
      }
      responseCommitted = true;
      if (!shouldClearComposer) return;
      if (draft === originalDraft) draft = "";
      attachments = attachments.filter(
        (attachment) => !sentAttachmentIds.has(attachment.id),
      );
    }

    function rememberPacketSession(
      packet: StreamPacket,
      packetSessionId: string | null,
    ): void {
      if (!packetSessionId) return;
      resolvedSessionId = packetSessionId;
      moveConversationRuntimePolicy(initialSessionId, packetSessionId);
      if (
        packet.type === "turn_accepted" &&
        packetSessionId !== requestedSessionId &&
        !acceptedNavigationDone
      ) {
        acceptedNavigationDone = true;
        navigate(`/?chat=${encodeURIComponent(packetSessionId)}`, true);
        window.dispatchEvent(new CustomEvent("iarf:chats-changed"));
      }
    }

    const payload = {
      session_id: initialSessionId,
      turn_id: turnId,
      prompt,
      files: sentAttachments.map(({ id: _id, ...attachment }) => attachment),
      model: config.model,
      thinking_config: { level: config.thinkingLevel },
      assistant: {
        mode: config.assistantMode,
        capability_ids: config.capabilityIds,
        runtime_profile_ids: config.runtimeProfileIds,
        enable_delegation: config.enableDelegation,
        allow_scope_expansion_request: true,
      },
      runtime_policy: chatRuntimePolicyPayload(config),
      capture_debug_prompt: debug,
      ...extra,
    };

    try {
      const response = await fetch("/api/frontend/chat", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          Accept: "text/event-stream",
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
        signal: requestController.signal,
      });
      if (response.redirected) {
        window.location.assign(response.url);
        return;
      }
      if (!response.ok) {
        const detail = (await response.json().catch(() => ({}))) as {
          error?: string;
        };
        throw new Error(
          detail.error || `The response failed with HTTP ${response.status}.`,
        );
      }
      if (!response.body) throw new Error("The response stream was unavailable.");

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        const parsed = parseSseChunk(
          buffer,
          decoder.decode(value ?? new Uint8Array(), { stream: !done }),
        );
        buffer = parsed.buffer;
        for (const rawPacket of parsed.events) {
          const packet = rawPacket as StreamPacket;
          packetIndex += 1;
          commitComposerForPacket(packet);
          const packetSessionId = acceptStreamPacket(packet, packetIndex);
          rememberPacketSession(packet, packetSessionId);
          if (packet.type === "final") completed = true;
        }
        if (done) {
          const finalParsed = parseSseChunk(buffer, "\n\n");
          for (const rawPacket of finalParsed.events) {
            const packet = rawPacket as StreamPacket;
            packetIndex += 1;
            commitComposerForPacket(packet);
            const packetSessionId = acceptStreamPacket(packet, packetIndex);
            rememberPacketSession(packet, packetSessionId);
            if (packet.type === "final") completed = true;
          }
          break;
        }
      }
      if (!completed) {
        throw new Error(
          "The connection ended before completion. Saved history has been reconciled.",
        );
      }
      if (resolvedSessionId) {
        if (resolvedSessionId !== requestedSessionId) {
          navigate(`/?chat=${encodeURIComponent(resolvedSessionId)}`, true);
        }
        await reloadConversation(resolvedSessionId);
        window.dispatchEvent(new CustomEvent("iarf:chats-changed"));
        if (contextInspectorEnabled) {
          window.dispatchEvent(
            new CustomEvent("iarf:workspace-refresh", {
              detail: { session_id: resolvedSessionId },
            }),
          );
        }
      }
    } catch (reason) {
      const aborted =
        reason instanceof DOMException && reason.name === "AbortError";
      const failureMessage =
        reason instanceof Error
          ? reason.message
          : "The response could not be completed.";
      if (!responseCommitted && optimisticId) {
        items = items.filter((item) => item.id !== optimisticId);
      }
      status = aborted
        ? "Stopped · reconciled with saved history"
        : `Connection interrupted · ${failureMessage}`;
      if (!aborted) {
        error = failureMessage;
      }
      if (resolvedSessionId) {
        try {
          await reloadConversation(resolvedSessionId);
        } catch {
          // Keep the useful stream state visible when reconciliation also fails.
        }
        if (resolvedSessionId !== requestedSessionId) {
          navigate(`/?chat=${encodeURIComponent(resolvedSessionId)}`, true);
          window.dispatchEvent(new CustomEvent("iarf:chats-changed"));
        }
      }
    } finally {
      busy = false;
      requestController = null;
      followRevision += 1;
    }
  }

  function sendMessage() {
    const command = draft.trim().match(/^\/([a-z-]+)(?:\s+(.*))?$/s);
    if (command?.[1] === "new") {
      newConversation();
      return;
    }
    if (command?.[1] === "fork" && session) {
      void forkConversation();
      return;
    }
    if (command?.[1] === "compress" && session) {
      void compressConversation();
      return;
    }
    const registered = commands.find(
      (item) =>
        item.command.toLocaleLowerCase() ===
        command?.[1]?.toLocaleLowerCase(),
    );
    if (
      registered?.ui_component_id &&
      !registered.is_prompt_passthrough
    ) {
      draft = "";
      openSlashCommand(registered, command?.[2] ?? "");
      return;
    }
    void streamChat({}, { optimistic: true, clearComposer: true });
  }

  function openSlashCommand(
    command: FrontendSlashCommand,
    query = "",
  ): void {
    if (!command.ui_component_id) return;
    window.dispatchEvent(
      new CustomEvent("iarf:open-ui-component", {
        detail: {
          componentId: command.ui_component_id,
          input: query.trim() ? { query: query.trim() } : {},
        },
      }),
    );
    announce(`Opening /${command.command}.`);
  }

  async function searchInlineCommand(
    command: FrontendSlashCommand,
    query: string,
    signal: AbortSignal,
  ): Promise<InlineComposerSuggestion[]> {
    const contract = command.inline_search;
    const endpoint =
      contract?.endpoint ||
      (command.helper_id === "googlecontacts"
        ? "/addons/googlecontacts/api/search"
        : "");
    if (!endpoint) return [];

    const url = new URL(endpoint, window.location.origin);
    url.searchParams.set("query", query.trim());
    const target = resolveInlineSearchRequestTarget(
      `${url.pathname}${url.search}`,
      window.location.origin,
    );
    const requestClient =
      target.kind === "addon"
        ? createAddonApiClient(window.location.origin, target.addonId)
        : api;
    const payload = await requestClient.request<unknown>(target.path, {
      signal,
    });
    return normalizeInlineSearchResults(payload);
  }

  function stopResponse() {
    requestController?.abort();
  }

  function newConversation() {
    if (busy) return;
    navigate("/", true);
  }

  async function editMessage(item: FrontendChatTimelineItem, text: string) {
    if (!session) return;
    try {
      await api.request(
        `/api/frontend/chats/${encodeURIComponent(session.id)}/messages/${item.message_id}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text }),
        },
      );
      await reloadConversation();
      announce("Message updated.");
    } catch (reason) {
      error =
        reason instanceof Error ? reason.message : "The message could not be updated.";
      throw reason;
    }
  }

  async function deleteMessage(item: FrontendChatTimelineItem) {
    if (!session || !window.confirm("Delete this message from the conversation?")) {
      return;
    }
    try {
      await api.request(
        `/api/frontend/chats/${encodeURIComponent(session.id)}/messages/${item.message_id}`,
        { method: "DELETE" },
      );
      await reloadConversation();
      announce("Message deleted.");
    } catch (reason) {
      error =
        reason instanceof Error ? reason.message : "The message could not be deleted.";
    }
  }

  function rerunMessage(item: FrontendChatTimelineItem) {
    if (!session || item.role !== "user") return;
    void streamChat(
      { rerun_message_index: item.sequence, prompt: "", files: [] },
      { optimistic: false, clearComposer: false },
    );
  }

  async function forkConversation() {
    if (!session || busy) return;
    try {
      const result = await api.request<{
        success: boolean;
        new_session_id: string;
      }>(`/api/chats/${encodeURIComponent(session.id)}/fork`, {
        method: "POST",
      });
      navigate(`/?chat=${encodeURIComponent(result.new_session_id)}`, true);
      window.dispatchEvent(new CustomEvent("iarf:chats-changed"));
      announce("Conversation forked.");
    } catch (reason) {
      error =
        reason instanceof Error ? reason.message : "The conversation could not be forked.";
    }
  }

  async function exportConversation(): Promise<string> {
    if (!session) return "";
    const payload = await api.request<{
      title: string;
      text: string;
      truncated: boolean;
    }>(`/api/frontend/chats/${encodeURIComponent(session.id)}/export`);
    if (payload.truncated) {
      announce("The copied export was truncated at the safety limit.");
    }
    return payload.text;
  }

  async function copyConversation() {
    try {
      const text = await exportConversation();
      await navigator.clipboard.writeText(text);
      sessionMenuOpen = false;
      announce("Conversation copied.");
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "The conversation could not be copied.";
    }
  }

  async function forkAsBrief() {
    try {
      const text = await exportConversation();
      writeLocalStorage(
        chatDraftKey(null),
        `Continue from this conversation:\n\n${text}`,
      );
      sessionMenuOpen = false;
      navigate("/", true);
      announce("Conversation copied into a new editable brief.");
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "The editable fork could not be prepared.";
    }
  }

  async function compressConversation() {
    if (!session || busy) return;
    status = "Compacting older tool context…";
    try {
      const result = await api.request<{ message?: string }>(
        `/api/chats/${encodeURIComponent(session.id)}/compress`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode: "sumtools" }),
        },
      );
      status = result.message ?? "Context compacted.";
      await reloadConversation();
    } catch (reason) {
      error =
        reason instanceof Error ? reason.message : "Context could not be compacted.";
    }
  }

  async function saveRename() {
    if (!session || !renameValue.trim()) return;
    try {
      const result = await api.request<{
        chat: { id: string; title: string; updated_at: string | null };
      }>(`/api/frontend/chats/${encodeURIComponent(session.id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: renameValue }),
      });
      session = { ...session, ...result.chat };
      renameOpen = false;
      sessionMenuOpen = false;
      window.dispatchEvent(new CustomEvent("iarf:chats-changed"));
      announce("Conversation renamed.");
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "The conversation could not be renamed.";
    }
  }

  async function deleteConversation() {
    if (
      !session ||
      !window.confirm(
        "Delete this conversation and its stored messages? This cannot be undone.",
      )
    ) {
      return;
    }
    try {
      await api.request(
        `/api/frontend/chats/${encodeURIComponent(session.id)}`,
        { method: "DELETE" },
      );
      window.dispatchEvent(new CustomEvent("iarf:chats-changed"));
      navigate("/", true);
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "The conversation could not be deleted.";
    }
  }

  async function toggleDebug() {
    debug = !debug;
    if (session) {
      loading = true;
      try {
        await reloadConversation();
      } catch (reason) {
        error =
          reason instanceof Error
            ? reason.message
            : "Developer details could not be loaded.";
      } finally {
        loading = false;
      }
    }
  }

  async function resolveInteraction(
    interaction: FrontendRuntimeInteraction,
    response: Readonly<Record<string, unknown>>,
  ) {
    await api.request(
      `/api/chat/interactions/${encodeURIComponent(interaction.id)}/resolve`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          response,
          expected_version: interaction.version,
        }),
      },
    );
    await streamChat(
      { resume_interaction_id: interaction.id, prompt: "", files: [] },
      { optimistic: false, clearComposer: false },
    );
  }

  async function waitForAudioJob(
    statusUrl: string,
    signal: AbortSignal,
  ): Promise<{ audio_url?: string }> {
    const deadline = Date.now() + 300_000;
    while (Date.now() < deadline) {
      const response = await fetch(statusUrl, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        signal,
      });
      const payload = (await response.json().catch(() => ({}))) as {
        status?: string;
        error?: string;
        result?: { audio_url?: string };
      };
      if (!response.ok) {
        throw new Error(payload.error || "Voice synthesis status failed.");
      }
      if (payload.status === "completed") return payload.result ?? {};
      if (payload.status === "failed") {
        throw new Error(payload.error || "Voice synthesis failed.");
      }
      await new Promise<void>((resolve, reject) => {
        const abort = () => {
          window.clearTimeout(timeout);
          reject(new DOMException("Aborted", "AbortError"));
        };
        const timeout = window.setTimeout(() => {
          signal.removeEventListener("abort", abort);
          resolve();
        }, 900);
        signal.addEventListener("abort", abort, { once: true });
      });
    }
    throw new Error("Voice synthesis timed out.");
  }

  async function playAudio(url: string) {
    const target = new URL(url, window.location.origin);
    if (
      target.origin !== window.location.origin ||
      !["/uploads/", "/addons/"].some((prefix) =>
        target.pathname.startsWith(prefix),
      )
    ) {
      throw new Error("The generated audio location was not trusted.");
    }
    activeAudio?.pause();
    activeAudio = new Audio(`${target.pathname}${target.search}`);
    await activeAudio.play();
  }

  async function listenToMessage(item: FrontendChatTimelineItem) {
    if (item.role !== "assistant") return;
    if (item.tts_audio_url) {
      await playAudio(item.tts_audio_url);
      return;
    }
    if (!config.voiceModel) {
      if (!("speechSynthesis" in window)) {
        throw new Error("Read aloud is unavailable in this browser.");
      }
      window.speechSynthesis.cancel();
      window.speechSynthesis.speak(new SpeechSynthesisUtterance(item.text));
      return;
    }

    voiceController?.abort();
    voiceController = new AbortController();
    const response = await fetch("/addons/audio_core/api/synthesize", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        text: item.text,
        model_id: Number(config.voiceModel),
        voice_name: "alloy",
        voice_profile_id: config.voice ? Number(config.voice) : null,
        message_id: item.message_id,
      }),
      signal: voiceController.signal,
    });
    const queued = (await response.json().catch(() => ({}))) as {
      error?: string;
      status_url?: string;
      audio_url?: string;
    };
    if (!response.ok) {
      throw new Error(queued.error || "Voice synthesis could not start.");
    }
    const result = queued.status_url
      ? await waitForAudioJob(queued.status_url, voiceController.signal)
      : queued;
    if (!result.audio_url) {
      throw new Error("Voice synthesis completed without an audio file.");
    }
    await playAudio(result.audio_url);
  }

</script>

<svelte:head>
  <meta
    name="description"
    content="A fast, durable conversation workspace for Carlina."
  />
</svelte:head>

<div class="chat-workspace">
<div class="chat-page">
  <header class="chat-heading">
    <div class="chat-heading__identity">
      <span class="chat-heading__eyebrow">Conversation</span>
      <h1>{pageTitle}</h1>
      {#if session}
        <p>
          {session.message_count.toLocaleString()} records
          <i></i>
          {session.stats.tokens.total.toLocaleString()} tokens
          <i></i>
          ${session.stats.cost.toFixed(4)}
        </p>
      {:else}
        <p>Unhurried writing, capable execution.</p>
      {/if}
    </div>
    <div class="chat-heading__actions">
      <button type="button" onclick={newConversation}>
        <Icon name="plus" size={15} />
        <span>New</span>
      </button>
      <div class="session-menu">
        <button
          type="button"
          aria-label="Conversation actions"
          aria-expanded={sessionMenuOpen}
          onclick={() => (sessionMenuOpen = !sessionMenuOpen)}
        >
          <Icon name="more" size={17} />
        </button>
        {#if sessionMenuOpen}
          <div class="session-menu__panel">
            {#if renameOpen && session}
              <label>
                <span>Conversation title</span>
                <input bind:value={renameValue} />
              </label>
              <div>
                <button type="button" onclick={() => (renameOpen = false)}>Cancel</button>
                <button type="button" onclick={saveRename}>Save</button>
              </div>
            {:else}
              <label
                class="session-menu__toggle"
                title="Show bounded developer events and prompts"
              >
                <input type="checkbox" checked={debug} onchange={toggleDebug} />
                <Icon name="code" size={14} />
                <span>Developer details</span>
                <small>{debug ? "On" : "Off"}</small>
              </label>
              {#if session}
                <button
                  type="button"
                  onclick={() => {
                    renameValue = session?.title ?? "";
                    renameOpen = true;
                  }}
                >
                  <Icon name="edit" size={14} /> Rename
                </button>
                <button type="button" onclick={forkConversation}>
                  <Icon name="fork" size={14} /> Fork conversation
                </button>
                <button type="button" onclick={forkAsBrief}>
                  <Icon name="edit" size={14} /> Fork as editable brief
                </button>
                <button type="button" onclick={copyConversation}>
                  <Icon name="copy" size={14} /> Copy conversation
                </button>
                <button type="button" onclick={compressConversation}>
                  <Icon name="brain" size={14} /> Compact tool context
                </button>
                <button class="danger" type="button" onclick={deleteConversation}>
                  <Icon name="trash" size={14} /> Delete conversation
                </button>
              {/if}
            {/if}
          </div>
        {/if}
      </div>
    </div>
  </header>

  <ChatTimeline
    {items}
    {loading}
    {loadingOlder}
    {hasMore}
    {busy}
    {error}
    {status}
    {followRevision}
    onloadolder={loadOlder}
    onedit={editMessage}
    ondelete={deleteMessage}
    onrerun={rerunMessage}
    onfork={() => void forkConversation()}
    onlisten={listenToMessage}
    onannounce={announce}
  />

  <div class="chat-dock">
    {#if status && !busy}
      <p class="chat-dock__status" role="status">{status}</p>
    {/if}
    {#each pendingInteractions as interaction (interaction.id)}
      <RuntimeInteractionCard
        {interaction}
        {busy}
        onresolve={resolveInteraction}
      />
    {/each}
    <ChatComposer
      bind:value={draft}
      bind:config
      {attachments}
      {models}
      {capabilities}
      {commands}
      {skills}
      {optionsLoading}
      {busy}
      {uploading}
      {voiceModels}
      {voices}
      {voiceLoading}
      {administratorApprovalMode}
      onsend={sendMessage}
      onstop={stopResponse}
      onupload={uploadFiles}
      onremoveattachment={removeAttachment}
      onstrategy={updateAttachmentStrategy}
      onloadvoice={loadVoiceOptions}
      oncommand={(command) => openSlashCommand(command)}
      oninlinequery={searchInlineCommand}
      onruntimepolicychange={setConversationRuntimePolicy}
      onrouting={() => navigate("/settings/routing", true)}
      onannounce={announce}
    />
  </div>

  <p class="sr-only" aria-live="polite">{announcement}</p>
</div>
  {#if contextInspectorEnabled}
    {#if ContextInspector}
      <ContextInspector api={api} sessionId={contextSessionId} {navigate} />
    {:else}
      <button
        class="context-inspector-lazy-trigger"
        type="button"
        aria-label="Open Context Inspector"
        title="Open Context Inspector"
        disabled={contextInspectorLoading}
        onclick={() => void openContextInspector()}
      >
        <Icon name="workspaces" size={16} />
        <span>{contextInspectorLoading ? "Loading…" : "Context"}</span>
      </button>
    {/if}
  {/if}
</div>

<style>
  .chat-workspace {
    position: relative;
    height: calc(100vh - var(--shell-header-height));
    min-height: 32rem;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    grid-template-rows: minmax(0, 1fr);
    overflow: hidden;
  }

  .context-inspector-lazy-trigger {
    width: 2.7rem;
    height: 100%;
    display: grid;
    align-content: center;
    justify-items: center;
    gap: 0.45rem;
    border: 0;
    border-left: 1px solid var(--color-rule);
    background: var(--color-surface-muted);
    color: var(--color-ink-muted);
    cursor: pointer;
  }

  .context-inspector-lazy-trigger:hover:not(:disabled),
  .context-inspector-lazy-trigger:focus-visible {
    color: var(--color-clay);
    background: var(--color-surface);
  }

  .context-inspector-lazy-trigger:disabled {
    cursor: progress;
  }

  .context-inspector-lazy-trigger span {
    writing-mode: vertical-rl;
    font-size: 0.62rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .chat-page {
    height: auto;
    min-width: 0;
    min-height: 0;
    display: grid;
    grid-template-rows: auto minmax(0, 1fr) auto;
    overflow: hidden;
    background:
      linear-gradient(
        90deg,
        transparent 0,
        color-mix(in srgb, var(--color-clay-muted) 18%, transparent) 50%,
        transparent 100%
      ),
      var(--color-paper);
  }

  .chat-heading {
    position: relative;
    z-index: 10;
    min-height: 3.65rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.35rem clamp(0.75rem, 2vw, 1.5rem);
    background: color-mix(in srgb, var(--color-paper) 91%, transparent);
    backdrop-filter: blur(10px);
  }

  .chat-heading__identity {
    min-width: 0;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: baseline;
    column-gap: 0.65rem;
  }

  .chat-heading__eyebrow {
    grid-column: 1 / -1;
    color: var(--color-clay);
    font-size: 0.51rem;
    font-weight: 750;
    letter-spacing: 0.15em;
    text-transform: uppercase;
  }

  .chat-heading h1 {
    overflow: hidden;
    margin: 0;
    font-family: var(--font-editorial);
    font-size: clamp(1.1rem, 2vw, 1.35rem);
    font-weight: 550;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .chat-heading__identity p {
    display: flex;
    align-items: center;
    gap: 0.42rem;
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.59rem;
    white-space: nowrap;
  }

  .chat-heading__identity p i {
    width: 2px;
    height: 2px;
    background: var(--color-rule-strong);
  }

  .chat-heading__actions {
    display: flex;
    align-items: center;
    gap: 0.35rem;
  }

  .chat-heading__actions > button,
  .session-menu > button {
    min-height: 2rem;
    display: inline-flex;
    align-items: center;
    gap: 0.42rem;
    border: 1px solid var(--color-rule);
    padding: 0 0.65rem;
    background: var(--color-surface);
    color: var(--color-ink-soft);
    cursor: pointer;
    font-size: 0.65rem;
  }

  .session-menu {
    position: relative;
  }

  .session-menu > button {
    width: 2.2rem;
    justify-content: center;
    padding: 0;
  }

  .session-menu__panel {
    position: absolute;
    z-index: 20;
    top: calc(100% + 0.4rem);
    right: 0;
    width: 15.5rem;
    display: grid;
    border: 1px solid var(--color-rule-strong);
    padding: 0.35rem;
    background: var(--color-surface);
    box-shadow: 0 0.8rem 2rem color-mix(in srgb, var(--color-ink) 15%, transparent);
  }

  .session-menu__panel > button {
    min-height: 2.45rem;
    display: flex;
    align-items: center;
    gap: 0.55rem;
    border: 0;
    padding: 0 0.65rem;
    background: transparent;
    color: var(--color-ink-soft);
    cursor: pointer;
    font-size: 0.67rem;
    text-align: left;
    text-decoration: none;
  }

  .session-menu__panel > button:hover {
    background: var(--color-surface-muted);
    color: var(--color-ink);
  }

  .session-menu__panel > button.danger {
    border-top: 1px solid var(--color-rule);
    color: var(--color-danger);
  }

  .session-menu__panel label {
    display: grid;
    gap: 0.35rem;
    padding: 0.5rem;
  }

  .session-menu__panel .session-menu__toggle {
    min-height: 2.45rem;
    display: grid;
    grid-template-columns: auto auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.5rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0 0.65rem;
    color: var(--color-ink-soft);
    cursor: pointer;
  }

  .session-menu__toggle input {
    width: 0.85rem;
    height: 0.85rem;
    margin: 0;
    accent-color: var(--color-clay);
  }

  .session-menu__panel .session-menu__toggle span {
    color: inherit;
    font-size: 0.67rem;
    font-weight: 500;
    letter-spacing: 0;
    text-transform: none;
  }

  .session-menu__toggle small {
    color: var(--color-ink-muted);
    font-size: 0.55rem;
    font-weight: 700;
    text-transform: uppercase;
  }

  .session-menu__panel label span {
    color: var(--color-ink-muted);
    font-size: 0.58rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .session-menu__panel > label:not(.session-menu__toggle) input {
    min-height: 2.35rem;
    border: 1px solid var(--color-rule);
    padding: 0 0.55rem;
    background: var(--color-surface);
    font-size: 0.7rem;
  }

  .session-menu__panel > div {
    display: flex;
    justify-content: flex-end;
    gap: 0.35rem;
    padding: 0 0.5rem 0.5rem;
  }

  .session-menu__panel > div button {
    min-height: 2rem;
    border: 1px solid var(--color-rule);
    padding: 0 0.6rem;
    background: var(--color-surface);
    cursor: pointer;
    font-size: 0.63rem;
  }

  .chat-dock {
    position: relative;
    z-index: 8;
    padding-top: 0.15rem;
    background:
      linear-gradient(
        180deg,
        transparent,
        color-mix(in srgb, var(--color-paper) 95%, transparent) 1.2rem
      );
  }

  .chat-dock__status {
    width: min(54rem, calc(100% - 2rem));
    margin: 0 auto 0.35rem;
    color: var(--color-ink-muted);
    font-size: 0.61rem;
    text-align: right;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  @media (max-width: 48rem) {
    .chat-heading {
      min-height: 4rem;
      padding-inline: 0.7rem;
    }

    .chat-heading__identity p,
    .chat-heading__actions > button span {
      display: none;
    }
  }

  @media (max-width: 64rem) {
    .chat-workspace {
      height: calc(100vh - var(--shell-header-height) - 3.75rem);
    }
  }
</style>
