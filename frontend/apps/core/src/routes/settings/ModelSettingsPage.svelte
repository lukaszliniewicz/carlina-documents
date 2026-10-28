<script lang="ts">
  import { onMount } from "svelte";
  import type {
    FrontendApiClient,
    FrontendBootstrap,
    FrontendIntelligenceSettingsDashboard,
    FrontendModelProvider,
    FrontendModelRecord,
    FrontendProviderConnection,
    FrontendProviderSettingsDetail,
    FrontendSettingsMutationResponse,
  } from "@carlina/sdk";
  import {
    Badge,
    Button,
    Dialog,
    EmptyState,
    Icon,
    IconButton,
    PageHeader,
    SearchField,
  } from "@carlina/ui";

  import {
    connectionTone,
    filterModelProviders,
    filterModels,
    formatSettingsDate,
    imageDetailOptions,
    modelInputCapacityLabel,
    modelLimit,
    optionalNumber,
    optionalInteger,
    providerTone,
    type ModelStateFilter,
    type ModelTypeFilter,
  } from "../../lib/model-settings.ts";

  interface Props {
    api: FrontendApiClient;
    bootstrap: FrontendBootstrap;
    route: string;
  }

  type DeskView = "connections" | "models";

  interface PendingAction {
    title: string;
    description: string;
    confirmLabel: string;
    endpoint: string;
    method?: "POST" | "PUT" | "DELETE";
    body?: Readonly<Record<string, unknown>>;
    danger?: boolean;
    confirmationTarget?: string;
    refresh?: "all" | "provider";
  }

  let { api, bootstrap, route }: Props = $props();

  let dashboard = $state<FrontendIntelligenceSettingsDashboard | null>(null);
  let detail = $state<FrontendProviderSettingsDetail | null>(null);
  let loading = $state(true);
  let detailLoading = $state(false);
  let refreshing = $state(false);
  let error = $state("");
  let notice = $state("");
  let providerQuery = $state("");
  let modelQuery = $state("");
  let modelType = $state<ModelTypeFilter>("all");
  let modelState = $state<ModelStateFilter>("all");
  let selectedProviderId = $state(0);
  let view = $state<DeskView>("connections");
  let workspaceTabs = $state<HTMLElement>();

  let endpointOpen = $state(false);
  let endpointName = $state("");
  let endpointBase = $state("");
  let endpointKey = $state("");
  let endpointVersion = $state("");
  let endpointGlobal = $state(false);

  let connectionOpen = $state(false);
  let editingConnection = $state<FrontendProviderConnection | null>(null);
  let connectionName = $state("");
  let connectionSecret = $state("");
  let connectionBase = $state("");
  let connectionVersion = $state("");
  let connectionGlobal = $state(false);
  let vertexProject = $state("");
  let vertexLocation = $state("us-central1");

  let modelOpen = $state(false);
  let editingModel = $state<FrontendModelRecord | null>(null);
  let modelString = $state("");
  let modelName = $state("");
  let modelKind = $state<"chat" | "embedding">("chat");
  let modelInputCost = $state("");
  let modelCachedInputCost = $state("");
  let modelOutputCost = $state("");
  let modelInputLimitOverride = $state("");
  let modelOutputLimitOverride = $state("");
  let modelReasoning = $state(false);
  let modelVision = $state(false);
  let modelPdf = $state(false);
  let modelAudio = $state(false);
  let reasoningLevels = $state<string[]>([]);
  let embeddingDimensions = $state("");
  let embeddingMrl = $state(false);
  let embeddingPrefixStyle = $state("none");
  let customQueryPrefix = $state("");
  let customPassagePrefix = $state("");

  let imageConfigs = $state<Record<string, string>>({});
  let imageDetailsDirty = $state(false);
  let imageDetailsPending = $state(false);
  let formPending = $state(false);

  let pendingAction = $state<PendingAction | null>(null);
  let confirmationText = $state("");
  let actionPending = $state(false);

  const isAdmin = $derived(bootstrap.user.is_admin);
  const providers = $derived(dashboard?.providers ?? []);
  const filteredProviders = $derived(
    filterModelProviders(providers, providerQuery),
  );
  const selectedProvider = $derived(
    providers.find((provider) => provider.id === selectedProviderId) ?? null,
  );
  const filteredModels = $derived(
    filterModels(detail?.models ?? [], {
      query: modelQuery,
      type: modelType,
      state: modelState,
    }),
  );
  const routableVisionModelIds = $derived(
    new Set(
      (dashboard?.routing.models ?? [])
        .filter((model) => model.capabilities.vision)
        .map((model) => model.id),
    ),
  );
  const isVertexProvider = $derived(selectedProvider?.key === "vertex_ai");

  onMount(() => {
    void loadDashboard();
  });

  function revealActiveWorkspaceTab(): void {
    workspaceTabs
      ?.querySelector<HTMLButtonElement>("button.active")
      ?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  function scrollWorkspaceTabs(event: KeyboardEvent): void {
    if (
      !workspaceTabs ||
      !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)
    ) {
      return;
    }
    event.preventDefault();
    if (event.key === "Home" || event.key === "End") {
      workspaceTabs.scrollTo({
        left: event.key === "Home" ? 0 : workspaceTabs.scrollWidth,
        behavior: "smooth",
      });
      return;
    }
    workspaceTabs.scrollBy({
      left:
        (event.key === "ArrowLeft" ? -1 : 1) * workspaceTabs.clientWidth * 0.7,
      behavior: "smooth",
    });
  }

  $effect(() => {
    view;
    if (typeof window === "undefined") return;
    const frame = window.requestAnimationFrame(revealActiveWorkspaceTab);
    return () => window.cancelAnimationFrame(frame);
  });

  function requestedProviderId(): number {
    const value = Number(
      new URL(route, "https://iarf.local").searchParams.get("provider"),
    );
    return Number.isInteger(value) && value > 0 ? value : 0;
  }

  async function loadDashboard(
    options: { preserveSelection?: boolean } = {},
  ): Promise<void> {
    if (!dashboard) loading = true;
    error = "";
    try {
      const payload = await api.request<FrontendIntelligenceSettingsDashboard>(
        "/api/frontend/settings/intelligence",
      );
      dashboard = payload;
      imageConfigs = { ...payload.image_detail.configs };
      imageDetailsDirty = false;

      const requested = requestedProviderId();
      const retained = options.preserveSelection ? selectedProviderId : 0;
      const candidate = requested || retained;
      if (payload.providers.some((provider) => provider.id === candidate)) {
        selectedProviderId = candidate;
      } else {
        selectedProviderId = payload.providers[0]?.id ?? 0;
      }
      if (selectedProviderId) await loadProviderDetail(selectedProviderId);
      else detail = null;
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "Model settings could not be loaded.";
    } finally {
      loading = false;
    }
  }

  async function loadProviderDetail(providerId: number): Promise<void> {
    detailLoading = true;
    error = "";
    try {
      detail = await api.request<FrontendProviderSettingsDetail>(
        `/api/frontend/settings/intelligence/providers/${providerId}`,
      );
    } catch (reason) {
      detail = null;
      error =
        reason instanceof Error
          ? reason.message
          : "Provider details could not be loaded.";
    } finally {
      detailLoading = false;
    }
  }

  async function refreshDesk(): Promise<void> {
    refreshing = true;
    notice = "";
    try {
      await loadDashboard({ preserveSelection: true });
      notice = "Model and connection state refreshed.";
    } finally {
      refreshing = false;
    }
  }

  function selectProvider(provider: FrontendModelProvider): void {
    if (provider.id === selectedProviderId) return;
    selectedProviderId = provider.id;
    notice = "";
    void loadProviderDetail(provider.id);
  }

  function openEndpointDialog(): void {
    endpointName = "";
    endpointBase = "";
    endpointKey = "";
    endpointVersion = "";
    endpointGlobal = false;
    endpointOpen = true;
  }

  async function createEndpoint(): Promise<void> {
    if (!endpointName.trim() || !endpointBase.trim()) {
      error = "Endpoint name and base URL are required.";
      return;
    }
    formPending = true;
    error = "";
    notice = "";
    try {
      await api.request(dashboard?.links.create_endpoint ?? "", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          display_name: endpointName.trim(),
          api_base: endpointBase.trim(),
          api_key: endpointKey.trim(),
          api_version: endpointVersion.trim(),
          is_global: endpointGlobal,
        }),
      });
      endpointOpen = false;
      notice = `${endpointName.trim()} was added.`;
      await loadDashboard({ preserveSelection: false });
    } catch (reason) {
      error =
        reason instanceof Error ? reason.message : "The endpoint was not added.";
    } finally {
      formPending = false;
    }
  }

  function openConnectionDialog(
    connection: FrontendProviderConnection | null = null,
  ): void {
    editingConnection = connection;
    connectionName = connection?.name ?? "";
    connectionSecret = "";
    connectionBase = "";
    connectionVersion = connection?.api_version ?? "";
    connectionGlobal = connection?.scope === "global";
    vertexProject = "";
    vertexLocation = "us-central1";
    connectionOpen = true;
  }

  async function saveConnection(): Promise<void> {
    if (!selectedProvider || !connectionName.trim()) {
      error = "A connection name is required.";
      return;
    }
    if (!editingConnection && !connectionSecret.trim()) {
      connectionSecret = "not-required";
    }
    if (
      selectedProvider.requires_endpoint &&
      !editingConnection &&
      !connectionBase.trim() &&
      !isVertexProvider
    ) {
      error = "This provider requires an API base URL.";
      return;
    }
    if (
      isVertexProvider &&
      !editingConnection &&
      (!vertexProject.trim() || !vertexLocation.trim())
    ) {
      error = "Vertex project and location are required.";
      return;
    }

    const body: Record<string, unknown> = {
      name: connectionName.trim(),
      is_global: connectionGlobal,
    };
    if (editingConnection) {
      if (connectionSecret.trim()) body.api_key = connectionSecret.trim();
      if (connectionBase.trim()) body.api_base = connectionBase.trim();
      body.api_version = connectionVersion.trim();
    } else {
      body.provider_id = selectedProvider.id;
      body.api_key = connectionSecret.trim();
      body.api_base = isVertexProvider
        ? `${vertexProject.trim()},${vertexLocation.trim()}`
        : connectionBase.trim();
      body.api_version = connectionVersion.trim();
    }

    formPending = true;
    error = "";
    notice = "";
    try {
      await api.request(
        editingConnection
          ? editingConnection.actions.update_url
          : dashboard?.links.create_connection ?? "",
        {
          method: editingConnection ? "PUT" : "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      connectionOpen = false;
      notice = editingConnection
        ? "Connection details updated."
        : "Connection added.";
      await loadDashboard({ preserveSelection: true });
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "The connection could not be saved.";
    } finally {
      formPending = false;
    }
  }

  function plainModelId(model: FrontendModelRecord): string {
    const prefix = `${selectedProvider?.key ?? ""}/`;
    return model.id.startsWith(prefix) ? model.id.slice(prefix.length) : model.id;
  }

  function openModelDialog(model: FrontendModelRecord | null = null): void {
    editingModel = model;
    modelString = model ? plainModelId(model) : "";
    modelName = model?.name ?? "";
    modelKind = model?.type === "embedding" ? "embedding" : "chat";
    modelInputCost = model?.costs.input?.toString() ?? "";
    modelCachedInputCost = model?.costs.cached_input?.toString() ?? "";
    modelOutputCost = model?.costs.output?.toString() ?? "";
    modelInputLimitOverride =
      (model ? modelLimit(model, "input").override_tokens : null)?.toString() ?? "";
    modelOutputLimitOverride =
      (model ? modelLimit(model, "output").override_tokens : null)?.toString() ?? "";
    modelReasoning = model?.capabilities.reasoning ?? false;
    modelVision = model?.capabilities.vision ?? false;
    modelPdf = model?.capabilities.pdf ?? false;
    modelAudio = model?.capabilities.audio ?? false;
    reasoningLevels = [...(model?.capabilities.reasoning_levels ?? [])];
    embeddingDimensions =
      model?.embedding.default_dimensions?.toString() ?? "";
    embeddingMrl = model?.embedding.supports_mrl ?? false;
    embeddingPrefixStyle = model?.embedding.prefix_style ?? "none";
    customQueryPrefix = "";
    customPassagePrefix = "";
    modelOpen = true;
  }

  async function saveModel(): Promise<void> {
    if (!selectedProvider || !modelString.trim() || !modelName.trim()) {
      error = "Model identifier and display name are required.";
      return;
    }
    const inputLimitOverride = optionalInteger(modelInputLimitOverride);
    const outputLimitOverride = optionalInteger(modelOutputLimitOverride);
    if (modelInputLimitOverride.trim() && inputLimitOverride === null) {
      error = "The input token limit must be a positive whole number.";
      return;
    }
    if (modelOutputLimitOverride.trim() && outputLimitOverride === null) {
      error = "The output token limit must be a positive whole number.";
      return;
    }
    const body: Record<string, unknown> = {
      provider_id: selectedProvider.id,
      display_name: modelName.trim(),
      model_type: modelKind,
      max_input_tokens_override: inputLimitOverride,
      max_output_tokens_override: outputLimitOverride,
    };
    // Discovered/catalogue models are editable for labels, capabilities, and
    // limits, but their provider model identifier is not a manual string.
    if (!editingModel || editingModel.manual) {
      body.model_string = modelString.trim();
    }
    if (modelKind === "embedding") {
      body.default_dimensions = optionalNumber(embeddingDimensions);
      body.supports_mrl = embeddingMrl;
      body.prefix_style = embeddingPrefixStyle;
      if (customQueryPrefix.trim()) {
        body.custom_query_prefix = customQueryPrefix.trim();
      }
      if (customPassagePrefix.trim()) {
        body.custom_passage_prefix = customPassagePrefix.trim();
      }
      body.input_cost = null;
      body.input_cached_cost = null;
      body.output_cost = null;
      body.supports_vision = false;
      body.supports_pdf = false;
      body.supports_reasoning = false;
      body.supports_audio_in = false;
      body.supported_reasoning_levels = null;
    } else {
      body.input_cost = optionalNumber(modelInputCost);
      body.input_cached_cost = optionalNumber(modelCachedInputCost);
      body.output_cost = optionalNumber(modelOutputCost);
      body.supports_vision = modelVision;
      body.supports_pdf = modelPdf;
      body.supports_reasoning = modelReasoning;
      body.supports_audio_in = modelAudio;
      body.supported_reasoning_levels = modelReasoning
        ? reasoningLevels.join(",")
        : null;
      body.default_dimensions = null;
      body.supports_mrl = false;
      body.prefix_style = "none";
    }

    formPending = true;
    error = "";
    notice = "";
    try {
      await api.request(
        editingModel
          ? editingModel.actions.update_url
          : dashboard?.links.create_model ?? "",
        {
          method: editingModel ? "PUT" : "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      modelOpen = false;
      notice = editingModel ? "Model updated." : "Custom model added.";
      await loadDashboard({ preserveSelection: true });
    } catch (reason) {
      error =
        reason instanceof Error ? reason.message : "The model was not saved.";
    } finally {
      formPending = false;
    }
  }

  function requestAction(action: PendingAction): void {
    pendingAction = action;
    confirmationText = "";
  }

  async function performPendingAction(): Promise<void> {
    if (!pendingAction) return;
    const action = pendingAction;
    if (
      action.confirmationTarget &&
      confirmationText.trim() !== action.confirmationTarget
    ) {
      return;
    }
    actionPending = true;
    error = "";
    notice = "";
    try {
      const response = await api.request<FrontendSettingsMutationResponse>(
        action.endpoint,
        {
          method: action.method ?? "POST",
          headers: action.body
            ? { "Content-Type": "application/json" }
            : undefined,
          body: action.body ? JSON.stringify(action.body) : undefined,
        },
      );
      pendingAction = null;
      confirmationText = "";
      notice = response.message || "Model settings updated.";
      if (action.refresh === "provider" && selectedProviderId) {
        await loadProviderDetail(selectedProviderId);
      } else {
        await loadDashboard({ preserveSelection: true });
      }
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "The requested change could not be applied.";
    } finally {
      actionPending = false;
    }
  }

  function requestConnectionActivation(
    connection: FrontendProviderConnection,
  ): void {
    requestAction({
      title: `Use “${connection.name}” for new requests?`,
      description:
        "This connection becomes active within its personal or shared scope. Other connections for the same provider and scope become inactive.",
      confirmLabel: "Make active",
      endpoint: connection.actions.activate_url,
      refresh: "all",
    });
  }

  function requestConnectionDeletion(
    connection: FrontendProviderConnection,
  ): void {
    requestAction({
      title: `Delete “${connection.name}”?`,
      description:
        "The encrypted key material and endpoint configuration for this connection will be removed. Models remain recorded but may become unavailable.",
      confirmLabel: "Delete connection",
      endpoint: connection.actions.delete_url,
      method: "DELETE",
      danger: true,
      confirmationTarget: connection.name,
      refresh: "all",
    });
  }

  function requestProviderDeletion(provider: FrontendModelProvider): void {
    requestAction({
      title: `Delete ${provider.name}?`,
      description:
        "This removes the personal endpoint, its connections, and its recorded models. It does not stop or modify the external model server.",
      confirmLabel: "Delete endpoint",
      endpoint: provider.actions.delete_url,
      method: "DELETE",
      danger: true,
      confirmationTarget: provider.key,
      refresh: "all",
    });
  }

  function requestModelAction(
    model: FrontendModelRecord,
    kind: "active" | "vision" | "pdf" | "reasoning" | "audio" | "delete",
  ): void {
    const definitions = {
      active: {
        title: `${model.active ? "Deactivate" : "Activate"} ${model.name}?`,
        description: model.active
          ? "The model will no longer appear in new model selections or routing choices."
          : "The model will become available for new selections within its scope.",
        confirmLabel: model.active ? "Deactivate model" : "Activate model",
        endpoint: model.actions.toggle_active_url,
      },
      vision: {
        title: `${model.capabilities.vision ? "Remove" : "Declare"} vision support?`,
        description:
          "Capability declarations control whether image inputs may be routed to this model. They must match the provider’s real behavior.",
        confirmLabel: model.capabilities.vision
          ? "Remove vision"
          : "Declare vision",
        endpoint: model.actions.toggle_vision_url,
      },
      pdf: {
        title: `${model.capabilities.pdf ? "Remove" : "Declare"} PDF support?`,
        description:
          "Capability declarations control whether PDF inputs may be routed to this model. They must match the provider’s real behavior.",
        confirmLabel: model.capabilities.pdf ? "Remove PDF" : "Declare PDF",
        endpoint: model.actions.toggle_pdf_url,
      },
      reasoning: {
        title: `${model.capabilities.reasoning ? "Remove" : "Declare"} reasoning support?`,
        description:
          "Reasoning controls and levels will follow this declaration in model selectors.",
        confirmLabel: model.capabilities.reasoning
          ? "Remove reasoning"
          : "Declare reasoning",
        endpoint: model.actions.toggle_reasoning_url,
      },
      audio: {
        title: `${model.capabilities.audio ? "Remove" : "Declare"} audio support?`,
        description:
          "Audio input controls will follow this declaration in model selectors.",
        confirmLabel: model.capabilities.audio
          ? "Remove audio"
          : "Declare audio",
        endpoint: model.actions.toggle_audio_url,
      },
      delete: {
        title: `Delete ${model.name}?`,
        description:
          "The custom model record and its routing metadata will be permanently removed. The provider itself is not changed.",
        confirmLabel: "Delete model",
        endpoint: model.actions.delete_url,
      },
    } as const;
    const definition = definitions[kind];
    requestAction({
      ...definition,
      method: kind === "delete" ? "DELETE" : "POST",
      danger: kind === "delete" || (kind === "active" && model.active),
      confirmationTarget: kind === "delete" ? model.id : undefined,
      refresh: "all",
    });
  }

  function requestSync(provider: FrontendModelProvider): void {
    requestAction({
      title: `Refresh ${provider.name} models?`,
      description:
        "The server will contact the configured provider and reconcile its model catalogue. No model is selected or used by this check.",
      confirmLabel: "Refresh catalogue",
      endpoint: provider.actions.sync_url,
      body: { provider_id: provider.id },
      refresh: "all",
    });
  }

  async function saveImageDetails(): Promise<void> {
    if (!dashboard) return;
    imageDetailsPending = true;
    error = "";
    notice = "";
    const stored = Object.fromEntries(
      Object.entries(imageConfigs).filter(([, value]) => value !== "preserve"),
    );
    try {
      const response = await api.request<FrontendSettingsMutationResponse>(
        dashboard.links.preferences,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ image_detail_configs: stored }),
        },
      );
      notice = response.message || "Image handling preferences saved.";
      await loadDashboard({ preserveSelection: true });
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "Image handling preferences were not saved.";
    } finally {
      imageDetailsPending = false;
    }
  }

  function canConfigureImageDetails(model: FrontendModelRecord): boolean {
    return model.capabilities.vision && routableVisionModelIds.has(model.id);
  }

  function updateImageDetail(modelId: string, value: string): void {
    imageConfigs = { ...imageConfigs, [modelId]: value };
    imageDetailsDirty = true;
  }

  function toggleReasoningLevel(level: string): void {
    reasoningLevels = reasoningLevels.includes(level)
      ? reasoningLevels.filter((item) => item !== level)
      : [...reasoningLevels, level];
  }
</script>

<div class="model-settings-page">
  <PageHeader
    eyebrow="Settings / Model control"
    title="Models & intelligence"
    description="Connect model providers, curate the models people can choose, and keep shared routing legible. Secret values stay write-only; this desk shows configuration state rather than credential material."
  >
    {#snippet actions()}
      <Button
        variant="outline"
        onclick={() => void refreshDesk()}
        disabled={refreshing}
      >
        <Icon name="refresh" size={15} />
        {refreshing ? "Refreshing…" : "Refresh"}
      </Button>
      <Button onclick={openEndpointDialog}>
        <Icon name="plus" size={15} />
        Add endpoint
      </Button>
    {/snippet}
  </PageHeader>

  {#if error}
    <p class="desk-message desk-message--error" role="alert">{error}</p>
  {:else if notice}
    <p class="desk-message" role="status">
      <Icon name="check" size={14} />
      {notice}
    </p>
  {/if}

  {#if loading}
    <section class="loading-state" aria-label="Loading model settings" aria-busy="true">
      <span></span><span></span><span></span>
    </section>
  {:else if dashboard}
    <section class="summary-strip" aria-label="Model settings summary">
      <article>
        <p>Providers</p>
        <strong>{dashboard.summary.providers}</strong>
        <span>{dashboard.summary.connected_providers} ready</span>
      </article>
      <article>
        <p>Connections</p>
        <strong>{dashboard.summary.connections}</strong>
        <span>secret values remain sealed</span>
      </article>
      <article>
        <p>Active models</p>
        <strong>{dashboard.summary.active_models}</strong>
        <span>{dashboard.summary.routing_models} routing choices</span>
      </article>
      <article>
        <p>Shared access</p>
        <strong>{dashboard.summary.delegations}</strong>
        <span>credential delegations</span>
      </article>
    </section>

    <div class="desk-layout">
      <aside class="provider-directory">
        <div class="directory-heading">
          <div>
            <p>Provider index</p>
            <h2>Connections</h2>
          </div>
          <span>{filteredProviders.length}</span>
        </div>
        <SearchField
          value={providerQuery}
          placeholder="Find a provider…"
          label="Search providers"
          oninput={(value) => (providerQuery = value)}
        />
        <div class="provider-list">
          {#each filteredProviders as provider (provider.id)}
            <button
              type="button"
              class:selected={provider.id === selectedProviderId}
              aria-pressed={provider.id === selectedProviderId}
              onclick={() => selectProvider(provider)}
            >
              <span class="provider-index">
                {String(providers.indexOf(provider) + 1).padStart(2, "0")}
              </span>
              <span class="provider-copy">
                <strong>{provider.name}</strong>
                <small>
                  {provider.active_model_count} active models ·
                  {provider.active_connection_count} live connections
                </small>
              </span>
              <span class:ready={provider.status === "ready"} class="provider-mark"></span>
            </button>
          {/each}
          {#if filteredProviders.length === 0}
            <p class="directory-empty">No providers match this search.</p>
          {/if}
        </div>
      </aside>

      <section class="provider-workspace">
        {#if selectedProvider}
          <header class="provider-header">
            <div>
              <div class="provider-kicker">
                <Badge tone={providerTone(selectedProvider)}>
                  {selectedProvider.status_label}
                </Badge>
                <span>{selectedProvider.scope_label}</span>
              </div>
              <h2>{selectedProvider.name}</h2>
              <p>
                {selectedProvider.kind_label} ·
                {selectedProvider.discovery_label} catalogue
              </p>
            </div>
            <div class="provider-header-actions">
              {#if selectedProvider.permissions.sync_models}
                <Button
                  variant="quiet"
                  size="sm"
                  onclick={() => requestSync(selectedProvider)}
                >
                  Refresh catalogue
                </Button>
              {/if}
              {#if selectedProvider.permissions.delete_provider}
                <Button
                  variant="danger"
                  size="sm"
                  onclick={() => requestProviderDeletion(selectedProvider)}
                >
                  Delete endpoint
                </Button>
              {/if}
            </div>
          </header>

          <nav
            class="workspace-tabs"
            aria-label="Model settings views"
            bind:this={workspaceTabs}
          >
            {#each [
              ["connections", "Connections", selectedProvider.connection_count],
              ["models", "Models", selectedProvider.model_count],
            ] as tab}
              <button
                type="button"
                class:active={view === tab[0]}
                aria-current={view === tab[0] ? "page" : undefined}
                onclick={() => (view = tab[0] as DeskView)}
                onkeydown={scrollWorkspaceTabs}
              >
                {tab[1]}
                <span>{tab[2]}</span>
              </button>
            {/each}
          </nav>

          {#if detailLoading}
            <div class="detail-loading" aria-busy="true">
              <span></span><span></span>
            </div>
          {:else if view === "connections" && detail}
            <section class="workspace-section">
              <div class="section-heading">
                <div>
                  <p>Encrypted connection register</p>
                  <h3>How requests reach {selectedProvider.name}</h3>
                  <span>
                    API keys and service-account documents are never returned to
                    this page. Replace a secret by entering a new one.
                  </span>
                </div>
                <Button size="sm" onclick={() => openConnectionDialog()}>
                  <Icon name="plus" size={14} />
                  Add connection
                </Button>
              </div>

              {#if detail.connections.length}
                <div class="connection-grid">
                  {#each detail.connections as connection (connection.id)}
                    <article class="connection-card">
                      <div class="connection-card__top">
                        <div>
                          <p>{connection.scope_label}</p>
                          <h4>{connection.name}</h4>
                        </div>
                        <Badge tone={connectionTone(connection)}>
                          {connection.active ? "Active" : "Standby"}
                        </Badge>
                      </div>
                      <dl>
                        <div>
                          <dt>Endpoint</dt>
                          <dd>{connection.endpoint_label ?? "Provider default"}</dd>
                        </div>
                        <div>
                          <dt>API version</dt>
                          <dd>{connection.api_version ?? "Provider default"}</dd>
                        </div>
                        <div>
                          <dt>Added</dt>
                          <dd>{formatSettingsDate(connection.created_at)}</dd>
                        </div>
                      </dl>
                      <footer>
                        {#if connection.permissions.activate && !connection.active}
                          <Button
                            variant="quiet"
                            size="sm"
                            onclick={() => requestConnectionActivation(connection)}
                          >
                            Make active
                          </Button>
                        {/if}
                        {#if connection.permissions.update}
                          <Button
                            variant="outline"
                            size="sm"
                            onclick={() => openConnectionDialog(connection)}
                          >
                            Edit label
                          </Button>
                        {/if}
                        {#if connection.permissions.delete}
                          <Button
                            variant="danger"
                            size="sm"
                            onclick={() => requestConnectionDeletion(connection)}
                          >
                            Delete
                          </Button>
                        {/if}
                      </footer>
                    </article>
                  {/each}
                </div>
              {:else}
                <EmptyState
                  title="No connection configured"
                  description="Add a personal connection, or—if you are an administrator—a shared connection for this provider."
                  icon="shield"
                >
                  {#snippet actions()}
                    <Button onclick={() => openConnectionDialog()}>
                      Add connection
                    </Button>
                  {/snippet}
                </EmptyState>
              {/if}
            </section>
          {:else if view === "models" && detail}
            <section class="workspace-section">
              <div class="section-heading">
                <div>
                  <p>Curated model catalogue</p>
                  <h3>{selectedProvider.name} models</h3>
                  <span>
                    Capability labels guide routing. They should describe what
                    the provider actually accepts.
                  </span>
                </div>
                {#if selectedProvider.permissions.add_model}
                  <Button size="sm" onclick={() => openModelDialog()}>
                    <Icon name="plus" size={14} />
                    Add model
                  </Button>
                {/if}
              </div>
              <div class="model-toolbar">
                <SearchField
                  value={modelQuery}
                  placeholder="Search model or capability…"
                  label="Search models"
                  oninput={(value) => (modelQuery = value)}
                />
                <label>
                  Type
                  <select bind:value={modelType}>
                    <option value="all">All types</option>
                    <option value="chat">Chat</option>
                    <option value="embedding">Embedding</option>
                  </select>
                </label>
                <label>
                  State
                  <select bind:value={modelState}>
                    <option value="all">All states</option>
                    <option value="active">Active</option>
                    <option value="inactive">Inactive</option>
                  </select>
                </label>
              </div>

              {#if filteredModels.length}
                <div class="model-list">
                  {#each filteredModels as model (model.db_id)}
                    <article class="model-row">
                      <div class="model-identity">
                        <div>
                          <Badge tone={model.active ? "success" : "neutral"}>
                            {model.active ? "Active" : "Inactive"}
                          </Badge>
                          <Badge tone="neutral">{model.scope_label}</Badge>
                          <Badge tone="neutral">{model.type}</Badge>
                        </div>
                        <h4>{model.name}</h4>
                        <code>{model.id}</code>
                        <span
                          class:unknown={!modelLimit(model, "input").known}
                          class="model-capacity"
                          title={`${modelLimit(model, "input").source_label} · ${modelLimit(model, "input").confidence} confidence`}
                        >
                          {modelInputCapacityLabel(model)}
                        </span>
                      </div>
                      <div class="model-capabilities">
                        <div class="capability-list" aria-label="Capabilities">
                          {#if model.type === "embedding"}
                            <span class:enabled={model.embedding.supports_mrl}>MRL</span>
                            <span>
                              {model.embedding.default_dimensions
                                ? `${model.embedding.default_dimensions} dim`
                                : "Flexible dimensions"}
                            </span>
                          {:else}
                            <span class:enabled={model.capabilities.vision}>Vision</span>
                            <span class:enabled={model.capabilities.pdf}>PDF</span>
                            <span class:enabled={model.capabilities.reasoning}>Reasoning</span>
                            <span class:enabled={model.capabilities.audio}>Audio</span>
                          {/if}
                        </div>
                        {#if model.capabilities.vision}
                          {#if canConfigureImageDetails(model)}
                            <label class="image-detail-policy">
                              <span>Image detail</span>
                              <select
                                value={imageConfigs[model.id] ?? "preserve"}
                                aria-label={`Image detail policy for ${model.name}`}
                                onchange={(event) =>
                                  updateImageDetail(model.id, event.currentTarget.value)}
                              >
                                {#each imageDetailOptions(model) as option (option.value)}
                                  <option value={option.value}>{option.label}</option>
                                {/each}
                              </select>
                            </label>
                          {:else}
                            <span
                              class="image-detail-unavailable"
                              title="Image detail can be set when this model is active and available to you."
                            >
                              Image detail available when active
                            </span>
                          {/if}
                        {/if}
                      </div>
                      <div class="model-actions">
                        {#if model.permissions.toggle_active}
                          <IconButton
                            icon={model.active ? "pause" : "play"}
                            label={`${model.active ? "Deactivate" : "Activate"} ${model.name}`}
                            onclick={() => requestModelAction(model, "active")}
                          />
                        {/if}
                        {#if model.permissions.edit}
                          <IconButton
                            icon="edit"
                            label={`Edit ${model.name}`}
                            onclick={() => openModelDialog(model)}
                          />
                        {/if}
                        {#if model.permissions.toggle_capabilities && model.type === "chat"}
                          <details class="capability-actions">
                            <summary title={`Configure capabilities for ${model.name}`}>
                              <Icon name="settings" size={14} />
                              Configure capabilities
                            </summary>
                            <div>
                              <button type="button" onclick={() => requestModelAction(model, "vision")}>
                                {model.capabilities.vision ? "Remove vision" : "Add vision"}
                              </button>
                              <button type="button" onclick={() => requestModelAction(model, "pdf")}>
                                {model.capabilities.pdf ? "Remove PDF" : "Add PDF"}
                              </button>
                              <button type="button" onclick={() => requestModelAction(model, "reasoning")}>
                                {model.capabilities.reasoning ? "Remove reasoning" : "Add reasoning"}
                              </button>
                              <button type="button" onclick={() => requestModelAction(model, "audio")}>
                                {model.capabilities.audio ? "Remove audio" : "Add audio"}
                              </button>
                            </div>
                          </details>
                        {/if}
                        {#if model.permissions.delete}
                          <IconButton
                            class="model-action--danger"
                            icon="trash"
                            label={`Delete ${model.name}`}
                            onclick={() => requestModelAction(model, "delete")}
                          />
                        {/if}
                      </div>
                    </article>
                  {/each}
                </div>
                {#if detail.model_total > detail.models.length}
                  <p class="bounded-note">
                    Showing {detail.models.length} of {detail.model_total} recorded
                    models for this provider.
                  </p>
                {/if}
              {:else}
                <EmptyState
                  title="No models match"
                  description="Adjust the filters, refresh provider discovery, or add a custom model when the provider supports it."
                  icon="brain"
                />
              {/if}
              {#if imageDetailsDirty}
                <div class="section-footer image-settings-save">
                  <p>
                    Review the image-detail choices above, then save them for
                    future requests. Other model preferences are retained.
                  </p>
                  <Button
                    onclick={() => void saveImageDetails()}
                    disabled={imageDetailsPending}
                  >
                    {imageDetailsPending ? "Saving…" : "Save image policies"}
                  </Button>
                </div>
              {/if}
            </section>
          {/if}
        {:else}
          <EmptyState
            title="No model providers"
            description="Add an OpenAI-compatible endpoint or use Advanced settings to configure a built-in provider."
            icon="brain"
          >
            {#snippet actions()}
              <Button onclick={openEndpointDialog}>Add endpoint</Button>
            {/snippet}
          </EmptyState>
        {/if}
      </section>
    </div>
  {/if}
</div>

<Dialog
  open={endpointOpen}
  title="Add OpenAI-compatible endpoint"
  description="Create a personal endpoint by default. Administrators may make it shared."
  onclose={() => {
    if (!formPending) endpointOpen = false;
  }}
  size="lg"
>
  <form
    class="dialog-form"
    onsubmit={(event) => {
      event.preventDefault();
      void createEndpoint();
    }}
  >
    <label>
      Display name
      <input bind:value={endpointName} required placeholder="Editorial inference server" />
    </label>
    <label>
      API base URL
      <input bind:value={endpointBase} type="url" required placeholder="http://localhost:8000/v1" />
    </label>
    <label>
      API key
      <input bind:value={endpointKey} type="password" autocomplete="new-password" placeholder="Optional when the endpoint does not require one" />
    </label>
    <label>
      API version
      <input bind:value={endpointVersion} placeholder="Optional gateway version" />
    </label>
    {#if isAdmin}
      <label class="check-field">
        <input type="checkbox" bind:checked={endpointGlobal} />
        <span>
          <strong>Shared endpoint</strong>
          <small>Make this provider and connection available across the installation.</small>
        </span>
      </label>
    {/if}
    <p class="secret-note">
      <Icon name="shield" size={17} />
      The secret is encrypted at rest and will not be shown again.
    </p>
  </form>
  {#snippet footer()}
    <Button variant="quiet" onclick={() => (endpointOpen = false)} disabled={formPending}>
      Cancel
    </Button>
    <Button onclick={() => void createEndpoint()} disabled={formPending}>
      {formPending ? "Adding…" : "Add endpoint"}
    </Button>
  {/snippet}
</Dialog>

<Dialog
  open={connectionOpen}
  title={editingConnection ? "Update connection" : `Connect ${selectedProvider?.name ?? "provider"}`}
  description={editingConnection
    ? "Secret and endpoint fields are blank by design. Enter a replacement only when it should change."
    : "Add encrypted provider credentials within a personal or shared scope."}
  onclose={() => {
    if (!formPending) connectionOpen = false;
  }}
  size="lg"
>
  <form
    class="dialog-form"
    onsubmit={(event) => {
      event.preventDefault();
      void saveConnection();
    }}
  >
    <label>
      Connection name
      <input bind:value={connectionName} required placeholder="Primary" />
    </label>
    {#if isVertexProvider}
      <div class="field-pair">
        <label>
          Google Cloud project
          <input bind:value={vertexProject} required={!editingConnection} placeholder="project-id" />
        </label>
        <label>
          Location
          <input bind:value={vertexLocation} required={!editingConnection} placeholder="us-central1" />
        </label>
      </div>
      <label>
        Service-account JSON
        <textarea bind:value={connectionSecret} rows="6" placeholder={editingConnection ? "Leave blank to keep the current credentials" : "Paste the service-account JSON"}></textarea>
      </label>
    {:else}
      <label>
        API key
        <input
          bind:value={connectionSecret}
          type="password"
          autocomplete="new-password"
          placeholder={editingConnection ? "Leave blank to keep the current key" : "Enter key or use not-required"}
        />
      </label>
      {#if selectedProvider?.requires_endpoint}
        <label>
          API base URL
          <input
            bind:value={connectionBase}
            type="url"
            placeholder={editingConnection
              ? "Leave blank to keep the configured endpoint"
              : "https://provider.example/v1"}
          />
        </label>
      {/if}
      <label>
        API version
        <input bind:value={connectionVersion} placeholder="Optional" />
      </label>
    {/if}
    {#if isAdmin && selectedProvider?.scope === "global"}
      <label class="check-field">
        <input type="checkbox" bind:checked={connectionGlobal} />
        <span>
          <strong>Shared connection</strong>
          <small>Allow this connection to serve all members.</small>
        </span>
      </label>
    {/if}
    <p class="secret-note">
      <Icon name="shield" size={17} />
      Existing secret material is never sent back to the browser.
    </p>
  </form>
  {#snippet footer()}
    <Button variant="quiet" onclick={() => (connectionOpen = false)} disabled={formPending}>
      Cancel
    </Button>
    <Button onclick={() => void saveConnection()} disabled={formPending}>
      {formPending ? "Saving…" : editingConnection ? "Save changes" : "Add connection"}
    </Button>
  {/snippet}
</Dialog>

<Dialog
  open={modelOpen}
  title={editingModel ? "Edit model" : `Add a ${selectedProvider?.name ?? ""} model`}
  description="Keep the identifier provider-accurate and declare only capabilities the endpoint actually supports. Blank limits use detected catalogue metadata or the labelled provisional fallback."
  onclose={() => {
    if (!formPending) modelOpen = false;
  }}
  size="lg"
>
  <form
    class="dialog-form"
    onsubmit={(event) => {
      event.preventDefault();
      void saveModel();
    }}
  >
    <div class="field-pair">
      <label>
        Model identifier
        <input bind:value={modelString} required disabled={Boolean(editingModel && !editingModel.manual)} placeholder="qwen2.5:latest" />
      </label>
      <label>
        Display name
        <input bind:value={modelName} required placeholder="Qwen 2.5" />
      </label>
    </div>
    <label>
      Model type
      <select bind:value={modelKind}>
        <option value="chat">Chat model</option>
        <option value="embedding">Embedding model</option>
      </select>
    </label>
    <div class="field-pair">
      <label>
        Input token limit override
        <input
          bind:value={modelInputLimitOverride}
          type="number"
          min="1"
          step="1"
          placeholder="Use detected or provisional fallback"
        />
        <small>Blank uses detected/catalogue input capacity; unknown models use a provisional fallback.</small>
      </label>
      <label>
        Output token limit override
        <input
          bind:value={modelOutputLimitOverride}
          type="number"
          min="1"
          step="1"
          placeholder="Use detected or provider default"
        />
        <small>Blank uses detected/catalogue output capacity.</small>
      </label>
    </div>
    {#if modelKind === "chat"}
      <div class="field-trio">
        <label>
          Input / million
          <input bind:value={modelInputCost} type="number" min="0" step="any" placeholder="Optional" />
        </label>
        <label>
          Cached input / million
          <input bind:value={modelCachedInputCost} type="number" min="0" step="any" placeholder="Optional" />
        </label>
        <label>
          Output / million
          <input bind:value={modelOutputCost} type="number" min="0" step="any" placeholder="Optional" />
        </label>
      </div>
      <label class="check-field">
        <input type="checkbox" bind:checked={modelVision} />
        <span>
          <strong>Vision input</strong>
          <small>The model accepts image inputs in provider requests.</small>
        </span>
      </label>
      <label class="check-field">
        <input type="checkbox" bind:checked={modelPdf} />
        <span>
          <strong>Native PDF input</strong>
          <small>The model accepts PDF documents without image conversion.</small>
        </span>
      </label>
      <label class="check-field">
        <input type="checkbox" bind:checked={modelReasoning} />
        <span>
          <strong>Reasoning controls</strong>
          <small>Expose supported reasoning levels in compatible workspaces.</small>
        </span>
      </label>
      {#if modelReasoning}
        <fieldset>
          <legend>Supported levels</legend>
          {#each ["off", "minimal", "low", "medium", "high", "xhigh"] as level}
            <label class="small-check">
              <input
                type="checkbox"
                checked={reasoningLevels.includes(level)}
                onchange={() => toggleReasoningLevel(level)}
              />
              {level}
            </label>
          {/each}
        </fieldset>
      {/if}
      <label class="check-field">
        <input type="checkbox" bind:checked={modelAudio} />
        <span>
          <strong>Audio input</strong>
          <small>The model accepts audio in provider requests.</small>
        </span>
      </label>
    {:else}
      <div class="field-pair">
        <label>
          Default dimensions
          <input bind:value={embeddingDimensions} type="number" min="1" step="1" placeholder="1536" />
        </label>
        <label>
          Prefix style
          <select bind:value={embeddingPrefixStyle}>
            <option value="none">No prefix</option>
            <option value="query_passage">Query / passage</option>
            <option value="instruct">Instruction</option>
            <option value="custom">Custom</option>
          </select>
        </label>
      </div>
      <label class="check-field">
        <input type="checkbox" bind:checked={embeddingMrl} />
        <span>
          <strong>Matryoshka dimensions</strong>
          <small>The model supports safe dimensionality reduction.</small>
        </span>
      </label>
      {#if embeddingPrefixStyle === "custom"}
        <label>
          Custom query prefix
          <textarea bind:value={customQueryPrefix} rows="3" placeholder={editingModel ? "Leave blank to preserve" : "Query prefix"}></textarea>
        </label>
        <label>
          Custom passage prefix
          <textarea bind:value={customPassagePrefix} rows="3" placeholder={editingModel ? "Leave blank to preserve" : "Passage prefix"}></textarea>
        </label>
      {/if}
    {/if}
  </form>
  {#snippet footer()}
    <Button variant="quiet" onclick={() => (modelOpen = false)} disabled={formPending}>
      Cancel
    </Button>
    <Button onclick={() => void saveModel()} disabled={formPending}>
      {formPending ? "Saving…" : editingModel ? "Save model" : "Add model"}
    </Button>
  {/snippet}
</Dialog>

<Dialog
  open={Boolean(pendingAction)}
  title={pendingAction?.title ?? "Review change"}
  description={pendingAction?.description}
  onclose={() => {
    if (!actionPending) {
      pendingAction = null;
      confirmationText = "";
    }
  }}
>
  {#if pendingAction?.confirmationTarget}
    <label class="typed-confirmation">
      Type <code>{pendingAction.confirmationTarget}</code> to confirm
      <input
        bind:value={confirmationText}
        autocomplete="off"
        spellcheck="false"
      />
    </label>
  {:else}
    <p class="review-note">
      Review the effect above. The change is applied only after you continue.
    </p>
  {/if}
  {#snippet footer()}
    <Button
      variant="quiet"
      onclick={() => {
        pendingAction = null;
        confirmationText = "";
      }}
      disabled={actionPending}
    >
      Keep current settings
    </Button>
    <Button
      variant={pendingAction?.danger ? "danger" : "primary"}
      onclick={() => void performPendingAction()}
      disabled={actionPending ||
        Boolean(
          pendingAction?.confirmationTarget &&
            confirmationText.trim() !== pendingAction.confirmationTarget,
        )}
    >
      {actionPending ? "Applying…" : pendingAction?.confirmLabel ?? "Continue"}
    </Button>
  {/snippet}
</Dialog>

<style>
  .model-settings-page {
    width: min(100%, 100rem);
    margin: 0 auto;
    padding: clamp(1.4rem, 4vw, 3.5rem);
  }

  .desk-message {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    border: 1px solid var(--color-rule);
    border-left: 3px solid var(--color-moss);
    margin: 1rem 0 0;
    padding: 0.7rem 0.85rem;
    background: var(--color-surface);
    color: var(--color-ink-soft);
    font-size: 0.74rem;
  }

  .desk-message--error {
    border-left-color: var(--color-danger);
    color: var(--color-danger);
  }

  .loading-state {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 1px;
    border: 1px solid var(--color-rule);
    margin-top: 1.5rem;
    background: var(--color-rule);
  }

  .loading-state span,
  .detail-loading span {
    min-height: 9rem;
    background: linear-gradient(
      90deg,
      var(--color-surface-muted),
      var(--color-surface),
      var(--color-surface-muted)
    );
    background-size: 200% 100%;
    animation: sweep 1.5s ease-in-out infinite;
  }

  .summary-strip {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    border: 1px solid var(--color-rule-strong);
    margin-top: 1.5rem;
    background: var(--color-surface);
  }

  .summary-strip article {
    min-width: 0;
    border-right: 1px solid var(--color-rule);
    padding: 1rem 1.15rem;
  }

  .summary-strip article:last-child {
    border-right: 0;
  }

  .summary-strip p,
  .directory-heading p,
  .provider-kicker,
  .section-heading p,
  .connection-card__top p {
    margin: 0;
    color: var(--color-clay);
    font-size: 0.57rem;
    font-weight: 800;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  .summary-strip strong {
    display: block;
    margin: 0.35rem 0 0.1rem;
    font-family: var(--font-editorial);
    font-size: 2rem;
    font-weight: 500;
  }

  .summary-strip span {
    color: var(--color-ink-muted);
    font-size: 0.65rem;
  }

  .desk-layout {
    display: grid;
    grid-template-columns: minmax(16rem, 0.27fr) minmax(0, 1fr);
    border: 1px solid var(--color-rule-strong);
    border-top: 0;
    background: var(--color-surface);
  }

  .provider-directory {
    min-width: 0;
    border-right: 1px solid var(--color-rule-strong);
    background: var(--color-paper);
  }

  .directory-heading {
    display: flex;
    align-items: end;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 1.15rem;
  }

  .directory-heading h2 {
    margin: 0.2rem 0 0;
    font-family: var(--font-editorial);
    font-size: 1.35rem;
    font-weight: 520;
  }

  .directory-heading > span {
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.68rem;
    color: var(--color-ink-muted);
  }

  .provider-directory :global(.search-field) {
    margin: 0.9rem;
  }

  .provider-list {
    max-height: 62rem;
    overflow: auto;
    border-top: 1px solid var(--color-rule);
  }

  .provider-list > button {
    width: 100%;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: start;
    gap: 0.7rem;
    border: 0;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.9rem;
    background: transparent;
    color: var(--color-ink);
    text-align: left;
    cursor: pointer;
  }

  .provider-list > button:hover,
  .provider-list > button.selected {
    background: var(--color-surface);
  }

  .provider-list > button.selected {
    box-shadow: inset 3px 0 0 var(--color-clay);
  }

  .provider-index {
    color: var(--color-rule-strong);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.57rem;
  }

  .provider-copy {
    min-width: 0;
  }

  .provider-copy strong {
    display: block;
    overflow: hidden;
    font-family: var(--font-editorial);
    font-size: 1rem;
    font-weight: 560;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .provider-copy small {
    display: block;
    margin-top: 0.25rem;
    color: var(--color-ink-muted);
    font-size: 0.6rem;
    line-height: 1.45;
  }

  .provider-mark {
    width: 0.55rem;
    height: 0.55rem;
    margin-top: 0.22rem;
    border: 1px solid var(--color-clay);
    background: var(--color-clay-muted);
  }

  .provider-mark.ready {
    border-color: var(--color-moss);
    background: var(--color-moss);
  }

  .directory-empty,
  .bounded-note {
    margin: 0;
    padding: 1rem;
    color: var(--color-ink-muted);
    font-size: 0.7rem;
  }

  .provider-workspace {
    min-width: 0;
  }

  .provider-header {
    min-height: 8.7rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule-strong);
    padding: 1.25rem clamp(1.1rem, 3vw, 2rem);
    background:
      linear-gradient(120deg, var(--color-surface), var(--color-clay-muted));
  }

  .provider-kicker {
    display: flex;
    align-items: center;
    gap: 0.65rem;
  }

  .provider-header h2 {
    margin: 0.55rem 0 0.15rem;
    font-family: var(--font-editorial);
    font-size: clamp(1.8rem, 4vw, 2.8rem);
    font-weight: 500;
  }

  .provider-header p {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.7rem;
  }

  .provider-header-actions {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 0.5rem;
  }

  .workspace-tabs {
    min-width: 0;
    display: flex;
    overflow-x: auto;
    overscroll-behavior-inline: contain;
    border-bottom: 1px solid var(--color-rule-strong);
    background: var(--color-paper);
    scrollbar-width: none;
    touch-action: pan-x;
  }

  .workspace-tabs::-webkit-scrollbar {
    display: none;
  }

  .workspace-tabs button {
    flex: 1 0 auto;
    min-width: 8rem;
    border: 0;
    border-right: 1px solid var(--color-rule);
    padding: 0.8rem 1rem;
    background: transparent;
    color: var(--color-ink-muted);
    font-size: 0.68rem;
    font-weight: 720;
    cursor: pointer;
  }

  .workspace-tabs button.active {
    box-shadow: inset 0 -3px 0 var(--color-clay);
    background: var(--color-surface);
    color: var(--color-ink);
  }

  .workspace-tabs button:focus-visible {
    outline: 2px solid var(--color-focus);
    outline-offset: -2px;
  }

  .workspace-tabs span {
    margin-left: 0.35rem;
    color: var(--color-rule-strong);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.58rem;
  }

  @media (max-width: 44rem) {
    .workspace-tabs button {
      min-width: 6.2rem;
      display: flex;
      flex: 0 0 auto;
      flex-direction: column;
      gap: 0.15rem;
      padding: 0.55rem 0.7rem;
    }

    .workspace-tabs span {
      margin-left: 0;
    }
  }

  .detail-loading {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1px;
    background: var(--color-rule);
  }

  .workspace-section {
    min-height: 32rem;
  }

  .section-heading {
    display: flex;
    align-items: end;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 1.3rem clamp(1rem, 3vw, 1.8rem);
  }

  .section-heading h3 {
    margin: 0.25rem 0 0;
    font-family: var(--font-editorial);
    font-size: 1.65rem;
    font-weight: 510;
  }

  .section-heading > div > span {
    display: block;
    max-width: 48rem;
    margin-top: 0.35rem;
    color: var(--color-ink-muted);
    font-size: 0.69rem;
    line-height: 1.55;
  }

  .connection-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 1px;
    background: var(--color-rule);
  }

  .connection-card {
    min-width: 0;
    background: var(--color-surface);
  }

  .connection-card__top {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 1rem;
  }

  .connection-card h4,
  .model-row h4 {
    margin: 0.25rem 0 0;
    font-family: var(--font-editorial);
    font-size: 1.2rem;
    font-weight: 540;
  }

  dl {
    margin: 0;
    padding: 0.4rem 1rem;
  }

  dl > div {
    display: grid;
    grid-template-columns: 6rem minmax(0, 1fr);
    gap: 0.7rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.55rem 0;
  }

  dl > div:last-child {
    border-bottom: 0;
  }

  dt {
    color: var(--color-ink-muted);
    font-size: 0.6rem;
    font-weight: 700;
    text-transform: uppercase;
  }

  dd {
    margin: 0;
    overflow-wrap: anywhere;
    color: var(--color-ink-soft);
    font-size: 0.7rem;
  }

  .connection-card footer,
  .section-footer {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 0.45rem;
    border-top: 1px solid var(--color-rule);
    padding: 0.75rem 1rem;
    background: var(--color-paper);
  }

  .model-toolbar {
    display: grid;
    grid-template-columns: minmax(13rem, 1fr) auto auto;
    gap: 0.75rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.85rem 1rem;
    background: var(--color-paper);
  }

  .model-toolbar > label {
    display: grid;
    gap: 0.35rem;
    color: var(--color-ink-muted);
    font-size: 0.6rem;
    font-weight: 700;
    text-transform: uppercase;
  }

  select,
  input,
  textarea {
    width: 100%;
    border: 1px solid var(--color-rule);
    border-radius: 0;
    padding: 0.65rem 0.7rem;
    background: var(--color-paper);
    color: var(--color-ink);
    font: inherit;
  }

  select:focus,
  input:focus,
  textarea:focus {
    border-color: var(--color-focus);
    outline: 2px solid var(--color-focus);
    outline-offset: 2px;
  }

  .model-list {
    display: grid;
    gap: 1px;
    background: var(--color-rule);
  }

  .model-row {
    display: grid;
    grid-template-columns: minmax(12rem, 1.15fr) minmax(12rem, 0.9fr) auto;
    align-items: center;
    gap: 1rem;
    padding: 1rem;
    background: var(--color-surface);
  }

  .model-identity > div {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
  }

  .model-identity code,
  .typed-confirmation code {
    display: inline-block;
    margin-top: 0.3rem;
    color: var(--color-ink-muted);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.58rem;
    overflow-wrap: anywhere;
  }

  .model-capacity {
    display: block;
    margin-top: 0.35rem;
    color: var(--color-ink-soft);
    font-size: 0.62rem;
    font-variant-numeric: tabular-nums;
  }

  .model-capacity.unknown {
    color: var(--color-ink-muted);
    font-style: italic;
  }

  .capability-list {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
  }

  .capability-list span {
    border: 1px solid var(--color-rule);
    padding: 0.28rem 0.42rem;
    color: var(--color-ink-muted);
    font-size: 0.57rem;
    font-weight: 700;
    text-transform: uppercase;
  }

  .capability-list span.enabled {
    border-color: var(--color-moss);
    background: var(--color-moss-muted);
    color: var(--color-ink-soft);
  }

  .model-capabilities {
    min-width: 0;
    display: grid;
    gap: 0.55rem;
  }

  .image-detail-policy {
    display: grid;
    grid-template-columns: auto minmax(8.5rem, 1fr);
    align-items: center;
    gap: 0.5rem;
    color: var(--color-ink-muted);
    font-size: 0.58rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .image-detail-policy select {
    min-width: 0;
    padding: 0.45rem 0.5rem;
    font-size: 0.65rem;
    letter-spacing: normal;
  }

  .image-detail-unavailable {
    color: var(--color-ink-muted);
    font-size: 0.62rem;
    line-height: 1.45;
  }

  .model-actions {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    flex-wrap: wrap;
    gap: 0.35rem;
  }

  :global(.model-action--danger) {
    color: var(--color-danger);
  }

  :global(.model-action--danger:hover) {
    border-color: var(--color-danger);
    background: var(--color-danger-muted);
  }

  .capability-actions {
    position: relative;
  }

  .capability-actions summary {
    min-height: 2.5rem;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    border: 1px solid var(--color-rule);
    padding: 0.45rem 0.6rem;
    color: var(--color-ink-soft);
    font-size: 0.62rem;
    font-weight: 700;
    cursor: pointer;
    list-style: none;
  }

  .capability-actions > div {
    position: absolute;
    z-index: 20;
    right: 0;
    width: 11rem;
    border: 1px solid var(--color-rule-strong);
    background: var(--color-surface);
    box-shadow: 6px 7px 0 color-mix(in srgb, var(--color-ink) 10%, transparent);
  }

  .capability-actions button {
    width: 100%;
    display: block;
    border: 0;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.65rem 0.75rem;
    background: transparent;
    color: var(--color-ink-soft);
    font-size: 0.62rem;
    text-align: left;
    cursor: pointer;
  }

  .capability-actions button:hover {
    background: var(--color-paper);
  }

  .section-footer {
    justify-content: space-between;
    padding: 1rem 1.15rem;
  }

  .section-footer p {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.66rem;
  }

  .dialog-form {
    display: grid;
    gap: 0.9rem;
  }

  .dialog-form > label,
  .field-pair label,
  .field-trio label,
  .typed-confirmation {
    display: grid;
    gap: 0.38rem;
    color: var(--color-ink-soft);
    font-size: 0.65rem;
    font-weight: 720;
  }

  .field-pair,
  .field-trio {
    display: grid;
    gap: 0.75rem;
  }

  .field-pair {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .field-trio {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .check-field {
    grid-template-columns: auto minmax(0, 1fr);
    align-items: start;
    gap: 0.65rem !important;
    border: 1px solid var(--color-rule);
    padding: 0.75rem;
    background: var(--color-paper);
  }

  .check-field input,
  .small-check input {
    width: auto;
    margin-top: 0.15rem;
  }

  .check-field strong,
  .check-field small {
    display: block;
  }

  .check-field small {
    margin-top: 0.2rem;
    color: var(--color-ink-muted);
    font-weight: 400;
    line-height: 1.45;
  }

  .secret-note,
  .review-note {
    display: flex;
    gap: 0.55rem;
    margin: 0;
    border-left: 2px solid var(--color-moss);
    padding: 0.65rem 0.75rem;
    background: var(--color-moss-muted);
    color: var(--color-ink-soft);
    font-size: 0.67rem;
    line-height: 1.5;
  }

  fieldset {
    display: flex;
    flex-wrap: wrap;
    gap: 0.65rem;
    border: 1px solid var(--color-rule);
    padding: 0.75rem;
  }

  legend {
    padding: 0 0.35rem;
    color: var(--color-ink-muted);
    font-size: 0.6rem;
    font-weight: 700;
    text-transform: uppercase;
  }

  .small-check {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    color: var(--color-ink-soft);
    font-size: 0.65rem;
    text-transform: capitalize;
  }

  .typed-confirmation input {
    margin-top: 0.5rem;
    font-family: ui-monospace, "Cascadia Code", monospace;
  }

  @keyframes sweep {
    from { background-position: 200% 0; }
    to { background-position: -200% 0; }
  }

  @media (max-width: 78rem) {
    .desk-layout {
      grid-template-columns: 15rem minmax(0, 1fr);
    }

    .model-row {
      grid-template-columns: minmax(0, 1fr) auto;
    }

    .model-capabilities {
      grid-column: 1 / -1;
      grid-row: 2;
    }
  }

  @media (max-width: 62rem) {
    .summary-strip {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .summary-strip article:nth-child(2) {
      border-right: 0;
    }

    .summary-strip article:nth-child(-n + 2) {
      border-bottom: 1px solid var(--color-rule);
    }

    .desk-layout {
      display: block;
    }

    .provider-directory {
      border-right: 0;
      border-bottom: 1px solid var(--color-rule-strong);
    }

    .provider-list {
      max-height: 18rem;
    }

    .connection-grid {
      grid-template-columns: 1fr;
    }

    .model-toolbar {
      grid-template-columns: 1fr 1fr;
    }

    .model-toolbar :global(.search-field) {
      grid-column: 1 / -1;
    }
  }

  @media (max-width: 44rem) {
    .model-settings-page {
      padding: 1rem;
    }

    .summary-strip {
      grid-template-columns: 1fr;
    }

    .summary-strip article,
    .summary-strip article:nth-child(2) {
      border-right: 0;
      border-bottom: 1px solid var(--color-rule);
    }

    .summary-strip article:last-child {
      border-bottom: 0;
    }

    .provider-header,
    .section-heading,
    .section-footer {
      align-items: stretch;
      flex-direction: column;
    }

    .provider-header-actions {
      justify-content: flex-start;
    }

    .model-toolbar,
    .model-row,
    .field-pair,
    .field-trio {
      grid-template-columns: 1fr;
    }

    .model-actions {
      justify-content: flex-start;
    }

    .image-detail-policy {
      grid-template-columns: 1fr;
    }
  }
</style>
