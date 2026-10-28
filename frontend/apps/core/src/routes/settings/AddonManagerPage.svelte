<script lang="ts">
  import { onMount, untrack } from "svelte";
  import {
    shouldUseClientNavigation,
    type FrontendAddonActionResponse,
    type FrontendAddonCatalogPackage,
    type FrontendAddonCatalogResponse,
    type FrontendAddonDoctorReport,
    type FrontendConnectionRecord,
    type FrontendAddonDependencyNotice,
    type FrontendAddonManagerItem,
    type FrontendAddonManagerSummary,
    type FrontendAddonOperation,
    type FrontendCapabilityCandidate,
    type FrontendCapabilityResolutionReport,
    type FrontendApiClient,
    type FrontendBootstrap,
  } from "@carlina/sdk";
  import { Button, EmptyState, Icon } from "@carlina/ui";

  import {
    addonOperationActive,
    collectAddonFacets,
    collectAddonKinds,
    filterAddonDirectory,
    readableAddonLabel,
    summarizeAddonDirectory,
    type AddonDirectoryStatus,
  } from "../../lib/addon-manager.ts";
  import {
    addonApiErrorPayload,
    addonApiErrorStatus,
    createAddonManagerApi,
    dependencyReviewNoticesFromError,
  } from "../../lib/addon-manager-api.ts";
  import {
    createAddonJobAction,
    createAddonLifecycleAction,
    createAddonWorkerAction,
    createServerRestartAction,
    type AddonPendingAction,
  } from "../../lib/addon-manager-actions.ts";
  import { announceRestartRequired } from "../../lib/restart-required.ts";
  import { createAddonManagerResources } from "../../lib/addon-manager-resources.ts";
  import AddonSettingsDialog from "./AddonSettingsDialog.svelte";
  import AddonManagerOverview from "./addon-manager/AddonManagerOverview.svelte";
  import AddonOperationsPanel from "./addon-manager/AddonOperationsPanel.svelte";
  import AddonDirectoryList from "./addon-manager/AddonDirectoryList.svelte";
  import AddonDirectoryToolbar from "./addon-manager/AddonDirectoryToolbar.svelte";
  import AddonDetailPanel from "./addon-manager/AddonDetailPanel.svelte";
  import AddonActionDialog from "./addon-manager/AddonActionDialog.svelte";
  import AddonLocalInstallDialog from "./addon-manager/AddonLocalInstallDialog.svelte";
  import AddonCatalogDialog from "./addon-manager/AddonCatalogDialog.svelte";
  import "./addon-manager.css";

  interface Props {
    api: FrontendApiClient;
    bootstrap: FrontendBootstrap;
    route: string;
    navigate: (
      href: string,
      clientRoute?: boolean,
      options?: { replace?: boolean },
    ) => "client" | "document";
    refreshBootstrap: () => Promise<FrontendBootstrap>;
  }

  const DIRECTORY_BATCH_SIZE = 25;

  let {
    api,
    bootstrap,
    route,
    navigate,
    refreshBootstrap,
  }: Props = $props();

  const addonApi = untrack(() => createAddonManagerApi(api));
  const resources = createAddonManagerResources(addonApi);
  const inventoryResource = resources.inventory;
  const detailResource = resources.detail;
  const connectionResource = resources.connections;
  const catalogResource = resources.catalog;
  const doctorResource = resources.doctor;
  const capabilityResource = resources.capability;
  const operationsResource = resources.operations;

  let addons = $state<FrontendAddonManagerSummary[]>([]);
  let refreshing = $state(false);
  let error = $state("");
  let notice = $state("");
  let query = $state("");
  let statusFilter = $state<AddonDirectoryStatus>("all");
  let kindFilter = $state("all");
  let roleFilter = $state("all");
  let categoryFilter = $state("all");
  let selectedId = $state("");
  let selectedDetail = $state<FrontendAddonManagerItem | null>(null);
  let selectedDetailError = $state("");
  let requestedDetailAddonId = $state("");
  let connectionsLoaded = $state(false);
  let pendingAction = $state<AddonPendingAction | null>(null);
  let confirmationText = $state("");
  let actionPending = $state(false);
  let installOpen = $state(false);
  let installFile = $state<File | null>(null);
  let installTrustApproved = $state(false);
  let installDependencyNotices = $state<FrontendAddonDependencyNotice[]>([]);
  let installDependencyReviewApproved = $state(false);
  let installPending = $state(false);
  let catalog = $state<FrontendAddonCatalogResponse | null>(null);
  let catalogError = $state("");
  let catalogOpen = $state(false);
  let selectedCatalogId = $state("");
  let catalogTrustApproved = $state(false);
  let catalogDependencyApproved = $state(false);
  let catalogInstallPending = $state(false);
  let restartRequired = $state(false);
  let settingsOpen = $state(false);
  let connections = $state<FrontendConnectionRecord[]>([]);
  let capabilityResolution = $state<FrontendCapabilityResolutionReport | null>(null);
  let capabilityBindingPending = $state(false);
  let requestedCapabilityAddonId = $state("");
  let doctor = $state<FrontendAddonDoctorReport | null>(null);
  let operations = $state<FrontendAddonOperation[]>([]);
  let operationsLoading = $state(false);
  let operationsError = $state("");
  let operationActionPending = $state("");
  let operationInitialTimer: number | null = null;
  let operationPollTimer: number | null = null;
  let visibleAddonLimit = $state(DIRECTORY_BATCH_SIZE);

  const isAdmin = $derived(bootstrap.user.is_admin);
  const loading = $derived(
    $inventoryResource.loading ||
      (!$inventoryResource.loaded && !$inventoryResource.error),
  );
  const selectedDetailLoading = $derived($detailResource.loading);
  const catalogLoading = $derived($catalogResource.loading);
  const doctorLoading = $derived($doctorResource.loading);
  const capabilityLoading = $derived($capabilityResource.loading);
  const summary = $derived(summarizeAddonDirectory(addons));
  const roles = $derived(collectAddonFacets(addons, "roles"));
  const categories = $derived(collectAddonFacets(addons, "categories"));
  const kinds = $derived(collectAddonKinds(addons));
  const filteredAddons = $derived(
    filterAddonDirectory(addons, {
      query,
      status: statusFilter,
      kind: kindFilter,
      role: roleFilter,
      category: categoryFilter,
    }),
  );
  const visibleAddons = $derived(
    filteredAddons.slice(0, visibleAddonLimit),
  );
  const selectedSummary = $derived(
    filteredAddons.find((addon) => addon.id === selectedId) ?? null,
  );
  const selectedAddon = $derived(
    selectedDetail?.id === selectedSummary?.id ? selectedDetail : null,
  );
  const selectedDoctorChecks = $derived(
    (doctor?.checks ?? []).filter(
      (check) => check.addon_id === selectedAddon?.id,
    ),
  );
  const selectedCatalogPackage = $derived(
    catalog?.packages.find((addon) => addon.id === selectedCatalogId) ??
      catalog?.packages[0] ??
      null,
  );
  const catalogDependencyNotices = $derived(
    (selectedCatalogPackage?.target?.contract.dependency_notices ?? []).filter(
      (dependencyNotice) => dependencyNotice.review_required !== false,
    ),
  );
  const catalogHasHostCode = $derived(
    (selectedCatalogPackage?.target?.contract.runtime_units ?? []).some(
      (runtimeUnit) => runtimeUnit.mode === "in_process",
    ) || Boolean(selectedCatalogPackage?.target?.contract.database_migration),
  );

  $effect(() => {
    query;
    statusFilter;
    kindFilter;
    roleFilter;
    categoryFilter;
    visibleAddonLimit = DIRECTORY_BATCH_SIZE;
  });

  $effect(() => {
    const addonId = selectedSummary?.id ?? "";
    if (!addonId) {
      detailResource.reset();
      selectedDetail = null;
      requestedDetailAddonId = "";
      capabilityResource.reset();
      capabilityResolution = null;
      requestedCapabilityAddonId = "";
      return;
    }
    if (addonId && addonId !== requestedDetailAddonId) {
      void loadAddonDetail(addonId);
    }
    if (addonId && !connectionsLoaded && !$connectionResource.loading) {
      void loadConnections();
    }
    if (addonId && addonId !== requestedCapabilityAddonId) {
      requestedCapabilityAddonId = addonId;
      void loadCapabilityResolution(addonId);
    }
  });

  onMount(() => {
    const parameters = new URL(route, "https://carlina.local").searchParams;
    const requestedAddon = parameters.get("addon");
    if (requestedAddon) selectedId = requestedAddon;
    settingsOpen = Boolean(
      requestedAddon && parameters.get("settings") === "1",
    );
    void loadAddons();
    operationInitialTimer = window.setTimeout(() => {
      operationInitialTimer = null;
      void loadOperations({ initial: true });
    }, 500);
    return () => {
      resources.abortAll();
      if (operationInitialTimer !== null) {
        window.clearTimeout(operationInitialTimer);
        operationInitialTimer = null;
      }
      if (operationPollTimer !== null) {
        window.clearTimeout(operationPollTimer);
        operationPollTimer = null;
      }
    };
  });

  function scheduleOperationPoll(): void {
    if (operationPollTimer !== null) {
      window.clearTimeout(operationPollTimer);
      operationPollTimer = null;
    }
    if (!isAdmin || !operations.some((operation) => addonOperationActive(operation))) {
      return;
    }
    operationPollTimer = window.setTimeout(() => {
      operationPollTimer = null;
      void loadOperations({ quiet: true });
    }, 1500);
  }

  function upsertOperation(operation: FrontendAddonOperation): void {
    operations = [
      operation,
      ...operations.filter((candidate) => candidate.id !== operation.id),
    ];
    scheduleOperationPoll();
  }

  function trackQueuedOperation(response: FrontendAddonActionResponse): boolean {
    if (!response.operation) return false;
    upsertOperation(response.operation);
    notice = response.message || "Addon lifecycle work was queued.";
    return true;
  }

  function markRestartRequired(): void {
    restartRequired = true;
    announceRestartRequired();
  }

  async function handleOperationTransitions(
    previous: ReadonlyMap<string, FrontendAddonOperation>,
    current: readonly FrontendAddonOperation[],
  ): Promise<void> {
    let refreshState = false;
    let terminalError = "";
    for (const operation of current) {
      const before = previous.get(operation.id);
      if (!before || !addonOperationActive(before) || addonOperationActive(operation)) {
        continue;
      }
      if (operation.status === "completed") {
        const requiresRestart = operation.result?.requires_restart === true;
        if (requiresRestart) markRestartRequired();
        notice = operation.message || `${operation.addon_id ?? "Addon"} is ready.`;
        refreshState = true;
      } else if (operation.status === "failed") {
        terminalError = `${operation.addon_id ?? "Addon"}: ${operation.error || operation.message || "Lifecycle operation failed."}`;
        refreshState = true;
      } else if (operation.status === "cancelled") {
        notice = operation.message || "Addon lifecycle operation cancelled.";
      }
    }
    if (refreshState) {
      await Promise.all([
        loadAddons(),
        loadConnections(),
        refreshCatalogIfLoaded(),
        refreshBootstrap(),
      ]);
    }
    if (terminalError) error = terminalError;
  }

  async function loadOperations(
    options: { quiet?: boolean; initial?: boolean } = {},
  ): Promise<void> {
    if (!isAdmin) {
      operations = [];
      operationsLoading = false;
      return;
    }
    if (!options.quiet) operationsLoading = true;
    const previous = new Map(
      operations.map((operation) => [operation.id, operation] as const),
    );
    try {
      const result = await operationsResource.load(25);
      if (result.status === "success") {
        const payload = result.data;
        const current = Array.isArray(payload.items) ? payload.items : [];
        operations = current;
        operationsError = "";
        if (!options.initial) {
          await handleOperationTransitions(previous, current);
        }
      } else if (result.status === "error") {
        operationsError = result.error;
      }
    } catch (reason) {
      operationsError =
        reason instanceof Error
          ? reason.message
          : "Lifecycle operation state could not be reconciled.";
    } finally {
      if (!options.quiet) operationsLoading = false;
      scheduleOperationPoll();
    }
  }

  async function controlOperation(
    operation: FrontendAddonOperation,
    action: "cancel" | "retry",
  ): Promise<void> {
    const pendingId = `${operation.id}:${action}`;
    operationActionPending = pendingId;
    operationsError = "";
    try {
      const updated = await addonApi.controlOperation(operation, action);
      upsertOperation(updated);
      notice =
        action === "cancel"
          ? "Cancellation requested. The current safe boundary will finish first."
          : "Addon lifecycle operation queued for retry.";
      await loadOperations({ quiet: true });
    } catch (reason) {
      operationsError =
        reason instanceof Error
          ? reason.message
          : `The operation could not be ${action === "cancel" ? "cancelled" : "retried"}.`;
    } finally {
      operationActionPending = "";
    }
  }

  async function loadAddons(): Promise<void> {
    error = "";
    const result = await inventoryResource.load();
    if (result.status === "aborted") return;
    if (result.status === "error") {
      error = result.error;
      return;
    }
    addons = Array.isArray(result.data.items) ? result.data.items : [];
    if (selectedId && !addons.some((addon) => addon.id === selectedId)) {
      selectedId = "";
    }
    detailResource.reset();
    selectedDetail = null;
    selectedDetailError = "";
    requestedDetailAddonId = "";
    capabilityResource.reset();
    capabilityResolution = null;
    requestedCapabilityAddonId = "";
    doctorResource.reset();
    doctor = null;
  }

  async function loadConnections(): Promise<void> {
    const result = await connectionResource.load();
    if (result.status === "aborted") return;
    connections =
      result.status === "success" && Array.isArray(result.data.items)
        ? result.data.items
        : [];
    connectionsLoaded = true;
  }

  async function loadAddonDetail(addonId: string): Promise<void> {
    requestedDetailAddonId = addonId;
    selectedDetailError = "";
    const result = await detailResource.load(addonId);
    if (selectedSummary?.id !== addonId) return;
    if (result.status === "success") {
      selectedDetail = result.data;
    } else if (result.status === "error") {
      selectedDetailError = result.error;
    }
  }

  async function loadCatalog(): Promise<void> {
    catalogError = "";
    const result = await catalogResource.load();
    if (result.status === "aborted") return;
    if (result.status === "error") {
      catalog = null;
      catalogError = result.error;
      return;
    }
    catalog = result.data;
    if (!catalog.packages.some((addon) => addon.id === selectedCatalogId)) {
      selectedCatalogId = catalog.packages[0]?.id ?? "";
    }
  }

  async function refreshCatalogIfLoaded(): Promise<void> {
    if (catalogOpen || catalog !== null) await loadCatalog();
  }

  function openCatalog(): void {
    catalogOpen = true;
    catalogTrustApproved = false;
    catalogDependencyApproved = false;
    void loadCatalog();
  }

  function closeCatalog(): void {
    if (catalogInstallPending) return;
    catalogResource.abort();
    catalogOpen = false;
    catalogTrustApproved = false;
    catalogDependencyApproved = false;
    catalogError = "";
  }

  function selectCatalogPackage(addon: FrontendAddonCatalogPackage): void {
    selectedCatalogId = addon.id;
    catalogTrustApproved = false;
    catalogDependencyApproved = false;
    catalogError = "";
  }

  async function runDoctor(): Promise<void> {
    error = "";
    const result = await doctorResource.load();
    if (result.status === "success") {
      doctor = result.data;
      notice = "Addon diagnostics refreshed.";
    } else if (result.status === "error") {
      error = result.error;
    }
  }

  async function loadCapabilityResolution(addonId: string): Promise<void> {
    const result = await capabilityResource.load(addonId);
    if (requestedCapabilityAddonId !== addonId) return;
    if (result.status === "success") {
      capabilityResolution = result.data;
    } else if (result.status === "error") {
      capabilityResolution = null;
    }
  }

  async function bindCapability(
    requirementId: string,
    candidate: FrontendCapabilityCandidate,
    connectionId?: string,
    selected = true,
  ): Promise<void> {
    if (!selectedAddon || capabilityBindingPending) return;
    capabilityBindingPending = true;
    error = "";
    notice = "";
    try {
      await addonApi.bindCapability(selectedAddon.id, {
        requirementId,
        candidate,
        connectionId,
        selected,
      });
      notice = selected
        ? `Saved ${readableAddonLabel(candidate.capability)} provider.`
        : `Removed ${readableAddonLabel(candidate.capability)} provider.`;
      await loadAddons();
    } catch (reason) {
      error = reason instanceof Error ? reason.message : "The provider choice could not be saved.";
    } finally {
      capabilityBindingPending = false;
    }
  }

  function openProviderSetup(candidate: FrontendCapabilityCandidate): void {
    const setupUrl = candidate.setup?.url?.trim();
    if (!setupUrl) {
      kindFilter = "connector";
      query = candidate.addon_id;
      return;
    }
    if (candidate.setup?.mode === "oauth" || candidate.setup?.mode === "external") {
      window.location.assign(setupUrl);
      return;
    }
    navigate(setupUrl, true);
  }

  async function refreshDirectory(): Promise<void> {
    refreshing = true;
    error = "";
    notice = "";
    try {
      if (isAdmin) {
        const response = await addonApi.refreshDiscovery();
        notice =
          response.message ||
          `Discovery refreshed for ${response.discovered_count ?? 0} addons.`;
      }
      await Promise.all([
        loadAddons(),
        loadConnections(),
        refreshCatalogIfLoaded(),
        refreshBootstrap(),
      ]);
      if (!notice) notice = "Addon state refreshed.";
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "Addon discovery could not be refreshed.";
    } finally {
      refreshing = false;
    }
  }

  function selectAddon(addonId: string): void {
    selectedId = addonId;
    selectedDetailError = "";
    notice = "";
  }

  function requestLifecycleAction(
    kind: "activate" | "deactivate" | "uninstall" | "purge",
    addon: FrontendAddonManagerItem,
  ): void {
    pendingAction = createAddonLifecycleAction(kind, addon);
    confirmationText = "";
  }

  function requestWorkerAction(
    kind: "stop-workers" | "restart-workers",
    addon: FrontendAddonManagerItem,
  ): void {
    pendingAction = createAddonWorkerAction(kind, addon);
    confirmationText = "";
  }

  function requestJobAction(
    kind: "pause-job" | "resume-job" | "run-job",
    addon: FrontendAddonManagerItem,
    jobId: string,
  ): void {
    pendingAction = createAddonJobAction(kind, addon, jobId);
    confirmationText = "";
  }

  function requestServerRestart(): void {
    pendingAction = createServerRestartAction();
    confirmationText = "";
  }

  async function performPendingAction(): Promise<void> {
    if (!pendingAction) return;
    const action = pendingAction;
    if (
      action.requiresIdentifier &&
      confirmationText.trim() !== action.confirmationPhrase
    ) {
      return;
    }

    actionPending = true;
    error = "";
    notice = "";
    try {
      const response = await addonApi.execute(action.command);
      pendingAction = null;
      confirmationText = "";
      notice = response.message || "Addon state updated.";

      if (action.kind === "restart-server") {
        restartRequired = false;
        window.setTimeout(() => window.location.reload(), 2600);
        return;
      }

      if (trackQueuedOperation(response)) return;

      if (
        response.requires_restart ||
        ["activate", "deactivate", "uninstall"].includes(action.kind)
      ) {
        markRestartRequired();
      }
      await loadAddons();
      await refreshBootstrap();
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "The requested addon operation failed.";
    } finally {
      actionPending = false;
    }
  }

  async function installAddon(): Promise<void> {
    if (!installFile) {
      error = "Choose an addon ZIP package first.";
      return;
    }
    if (!installFile.name.toLocaleLowerCase().endsWith(".zip")) {
      error = "Addon packages must use the .zip extension.";
      return;
    }
    if (!installTrustApproved) {
      error =
        "Confirm that host-executed add-on code receives the same access as Carlina.";
      return;
    }
    if (
      installDependencyNotices.length > 0 &&
      !installDependencyReviewApproved
    ) {
      error = "Review and approve the declared dependency licenses first.";
      return;
    }

    installPending = true;
    error = "";
    notice = "";
    try {
      const response = await addonApi.installLocal({
        file: installFile,
        approveNativeCode: installTrustApproved,
        dependencyReview: installDependencyNotices
          .filter((dependencyNotice) => dependencyNotice.review_required !== false)
          .map((dependencyNotice) => dependencyNotice.id),
      });
      installOpen = false;
      installFile = null;
      installTrustApproved = false;
      installDependencyNotices = [];
      installDependencyReviewApproved = false;
      if (trackQueuedOperation(response)) return;
      if (response.requires_restart ?? true) markRestartRequired();
      notice = response.message || "Addon installed.";
      await Promise.all([
        loadAddons(),
        loadConnections(),
        refreshCatalogIfLoaded(),
        refreshBootstrap(),
      ]);
    } catch (reason) {
      const payload = addonApiErrorPayload(reason);
      const notices = dependencyReviewNoticesFromError(reason);
      if (
        addonApiErrorStatus(reason) === 409 &&
        payload?.status === "dependency_review_required" &&
        notices.length > 0
      ) {
        installDependencyNotices = notices;
        installDependencyReviewApproved = false;
        error =
          "The package is staged safely. Review its optional dependency licenses below, then submit it again.";
      } else {
        error =
          reason instanceof Error
            ? reason.message
            : "The addon package could not be installed.";
      }
    } finally {
      installPending = false;
    }
  }

  async function installCatalogAddon(): Promise<void> {
    const selected = selectedCatalogPackage;
    const target = selected?.target;
    if (!selected || !target || !catalog?.source_sha256) {
      catalogError = "Choose an installable catalog release first.";
      return;
    }
    if (!catalogTrustApproved) {
      catalogError =
        "Review the signed publisher, permissions, and runtime access first.";
      return;
    }
    if (catalogDependencyNotices.length > 0 && !catalogDependencyApproved) {
      catalogError = "Review and approve the declared dependency licenses first.";
      return;
    }

    catalogInstallPending = true;
    catalogError = "";
    error = "";
    notice = "";
    try {
      const response = await addonApi.installCatalog({
        addonId: selected.id,
        version: target.version,
        catalogSha256: catalog.source_sha256,
        archiveSha256: target.archive.sha256,
        approveNativeCode: catalogTrustApproved,
        dependencyReview: catalogDependencyNotices.map(
          (dependencyNotice) => dependencyNotice.id,
        ),
      });
      catalogOpen = false;
      catalogTrustApproved = false;
      catalogDependencyApproved = false;
      if (trackQueuedOperation(response)) return;
      if (response.requires_restart ?? true) markRestartRequired();
      notice = response.message || "Addon installed from the catalog.";
      await loadAddons();
      await loadCatalog();
      await refreshBootstrap();
    } catch (reason) {
      const payload = addonApiErrorPayload(reason);
      const status = addonApiErrorStatus(reason);
      if (status === 409 && payload?.status === "catalog_changed") {
        catalogTrustApproved = false;
        catalogDependencyApproved = false;
        await loadCatalog();
        catalogError =
          "The signed catalog changed after review. Review the refreshed release before installing.";
      } else if (
        status === 409 &&
        payload?.status === "dependency_review_required"
      ) {
        catalogDependencyApproved = false;
        catalogError =
          "The package dependency declarations changed. Review the current license notices before continuing.";
        await loadCatalog();
      } else {
        catalogError =
          reason instanceof Error
            ? reason.message
            : "The catalog package could not be installed.";
      }
    } finally {
      catalogInstallPending = false;
    }
  }

  function closeLocalInstall(): void {
    if (installPending) return;
    installOpen = false;
    installFile = null;
    installTrustApproved = false;
    installDependencyNotices = [];
    installDependencyReviewApproved = false;
  }

  function closePendingAction(): void {
    if (actionPending) return;
    pendingAction = null;
    confirmationText = "";
  }

  function activateHref(href: string, event: MouseEvent): void {
    if (!shouldUseClientNavigation(event)) return;
    event.preventDefault();
    navigate(href, true);
  }
</script>

<div class="addon-manager-page">
  <AddonManagerOverview
    {summary}
    bind:statusFilter
    bind:kindFilter
    {doctor}
    {doctorLoading}
    {refreshing}
    {restartRequired}
    {isAdmin}
    ondoctor={() => void runDoctor()}
    onrefresh={() => void refreshDirectory()}
    oncatalog={openCatalog}
    oninstall={() => (installOpen = true)}
    onrestart={requestServerRestart}
  />

  <AddonOperationsPanel
    {isAdmin}
    {operations}
    loading={operationsLoading}
    error={operationsError}
    actionPending={operationActionPending}
    onrefresh={() => void loadOperations()}
    oncontrol={(operation, action) => void controlOperation(operation, action)}
    onnavigate={activateHref}
  />

  <AddonDirectoryToolbar
    bind:query
    bind:statusFilter
    bind:kindFilter
    bind:roleFilter
    bind:categoryFilter
    {kinds}
    {roles}
    {categories}
  />

  {#if error}
    <p class="page-feedback page-feedback--error" role="alert">{error}</p>
  {:else if notice}
    <p class="page-feedback" role="status">
      <Icon name="check" size={15} />
      {notice}
    </p>
  {/if}

  {#if loading}
    <section class="directory-loading" aria-label="Loading addon directory" aria-busy="true">
      <span></span><span></span><span></span>
    </section>
  {:else if addons.length === 0}
    <EmptyState
      icon="workspaces"
      title="No addons discovered"
      description="Refresh discovery or install a valid addon package to begin."
    >
      {#snippet actions()}
        {#if isAdmin}
          <Button variant="outline" onclick={openCatalog}>Browse catalog</Button>
          <Button onclick={() => (installOpen = true)}>Install local ZIP</Button>
        {/if}
      {/snippet}
    </EmptyState>
  {:else}
    <div class="manager-layout">
      <AddonDirectoryList
        addons={filteredAddons}
        totalCount={addons.length}
        {visibleAddons}
        selectedId={selectedSummary?.id ?? ""}
        hasFilters={Boolean(
          query ||
            statusFilter !== "all" ||
            kindFilter !== "all" ||
            roleFilter !== "all" ||
            categoryFilter !== "all",
        )}
        batchSize={DIRECTORY_BATCH_SIZE}
        onselect={selectAddon}
        onclear={() => {
          query = "";
          statusFilter = "all";
          kindFilter = "all";
          roleFilter = "all";
          categoryFilter = "all";
        }}
        onshowmore={() => (visibleAddonLimit += DIRECTORY_BATCH_SIZE)}
      />

      <AddonDetailPanel
        summary={selectedSummary}
        addon={selectedAddon}
        loading={selectedDetailLoading}
        error={selectedDetailError}
        doctorChecks={selectedDoctorChecks}
        {isAdmin}
        {actionPending}
        {capabilityLoading}
        {capabilityResolution}
        {capabilityBindingPending}
        {connections}
        onretry={(addonId) => void loadAddonDetail(addonId)}
        onlifecycle={requestLifecycleAction}
        onsettings={() => (settingsOpen = true)}
        ondependency={(addonId) => {
          query = "";
          statusFilter = "all";
          kindFilter = "all";
          roleFilter = "all";
          categoryFilter = "all";
          selectAddon(addonId);
        }}
        onbind={(requirementId, candidate, connectionId, selected) =>
          void bindCapability(requirementId, candidate, connectionId, selected)}
        onprovidersetup={openProviderSetup}
        onworker={requestWorkerAction}
        onjob={requestJobAction}
        onnavigate={activateHref}
      />
    </div>
  {/if}

<AddonSettingsDialog
  {api}
  addon={selectedAddon}
  open={settingsOpen}
  onclose={() => (settingsOpen = false)}
/>

<AddonCatalogDialog
  open={catalogOpen}
  {catalog}
  loading={catalogLoading}
  error={catalogError}
  selectedPackage={selectedCatalogPackage}
  dependencyNotices={catalogDependencyNotices}
  hasHostCode={catalogHasHostCode}
  bind:trustApproved={catalogTrustApproved}
  bind:dependencyApproved={catalogDependencyApproved}
  installPending={catalogInstallPending}
  onclose={closeCatalog}
  onselect={selectCatalogPackage}
  oninstall={() => void installCatalogAddon()}
/>
<AddonLocalInstallDialog
  open={installOpen}
  bind:file={installFile}
  bind:trustApproved={installTrustApproved}
  bind:dependencyNotices={installDependencyNotices}
  bind:dependencyReviewApproved={installDependencyReviewApproved}
  pending={installPending}
  onclose={closeLocalInstall}
  oninstall={() => void installAddon()}
/>
<AddonActionDialog
  action={pendingAction}
  bind:confirmation={confirmationText}
  pending={actionPending}
  onclose={closePendingAction}
  onconfirm={() => void performPendingAction()}
/>
</div>
