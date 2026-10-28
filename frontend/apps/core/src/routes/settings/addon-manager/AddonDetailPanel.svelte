<script lang="ts">
  import type {
    FrontendAddonDoctorCheck,
    FrontendAddonManagerItem,
    FrontendAddonManagerSummary,
    FrontendCapabilityCandidate,
    FrontendCapabilityResolutionReport,
    FrontendConnectionRecord,
  } from "@carlina/sdk";
  import { Badge, Button, EmptyState, Icon } from "@carlina/ui";

  import {
    addonBuildTone,
    addonCodeAccessLabel,
    addonDateLabel,
    addonEffectiveCodeAccess,
    addonNeedsAttention,
    addonStatusLabel,
    addonStatusTone,
    readableAddonLabel,
  } from "../../../lib/addon-manager.ts";

  type LifecycleAction = "activate" | "deactivate" | "uninstall" | "purge";
  type WorkerAction = "stop-workers" | "restart-workers";
  type JobAction = "pause-job" | "resume-job" | "run-job";

  interface Props {
    summary: FrontendAddonManagerSummary | null;
    addon: FrontendAddonManagerItem | null;
    loading: boolean;
    error: string;
    doctorChecks: readonly FrontendAddonDoctorCheck[];
    isAdmin: boolean;
    actionPending: boolean;
    capabilityLoading: boolean;
    capabilityResolution: FrontendCapabilityResolutionReport | null;
    capabilityBindingPending: boolean;
    connections: readonly FrontendConnectionRecord[];
    onretry: (addonId: string) => void;
    onlifecycle: (kind: LifecycleAction, addon: FrontendAddonManagerItem) => void;
    onsettings: () => void;
    ondependency: (addonId: string) => void;
    onbind: (
      requirementId: string,
      candidate: FrontendCapabilityCandidate,
      connectionId?: string,
      selected?: boolean,
    ) => void;
    onprovidersetup: (candidate: FrontendCapabilityCandidate) => void;
    onworker: (kind: WorkerAction, addon: FrontendAddonManagerItem) => void;
    onjob: (
      kind: JobAction,
      addon: FrontendAddonManagerItem,
      jobId: string,
    ) => void;
    onnavigate: (href: string, event: MouseEvent) => void;
  }

  let {
    summary: selectedSummary,
    addon: selectedAddon,
    loading: selectedDetailLoading,
    error: selectedDetailError,
    doctorChecks: selectedDoctorChecks,
    isAdmin,
    actionPending,
    capabilityLoading,
    capabilityResolution,
    capabilityBindingPending,
    connections,
    onretry,
    onlifecycle,
    onsettings,
    ondependency,
    onbind,
    onprovidersetup,
    onworker,
    onjob,
    onnavigate,
  }: Props = $props();

  const selectedConnectorConnections = $derived(
    connections.filter(
      (connection) => connection.connector_addon_id === selectedAddon?.id,
    ),
  );

  const readableLabel = readableAddonLabel;
  const statusTone = addonStatusTone;
  const buildTone = addonBuildTone;
  const effectiveCodeAccess = addonEffectiveCodeAccess;
  const codeAccessLabel = addonCodeAccessLabel;
  const dateLabel = addonDateLabel;

  function requestLifecycleAction(
    kind: LifecycleAction,
    addon: FrontendAddonManagerItem,
  ): void {
    onlifecycle(kind, addon);
  }

  function bindCapability(
    requirementId: string,
    candidate: FrontendCapabilityCandidate,
    connectionId?: string,
    selected?: boolean,
  ): void {
    onbind(requirementId, candidate, connectionId, selected);
  }

  function openProviderSetup(candidate: FrontendCapabilityCandidate): void {
    onprovidersetup(candidate);
  }

  function requestWorkerAction(
    kind: WorkerAction,
    addon: FrontendAddonManagerItem,
  ): void {
    onworker(kind, addon);
  }

  function requestJobAction(
    kind: JobAction,
    addon: FrontendAddonManagerItem,
    jobId: string,
  ): void {
    onjob(kind, addon, jobId);
  }

  function activateHref(href: string, event: MouseEvent): void {
    onnavigate(href, event);
  }
</script>

<section class="addon-detail" aria-live="polite">
  {#if selectedSummary && selectedDetailError}
    <EmptyState
      icon="activity"
      title="Addon detail unavailable"
      description={selectedDetailError}
    >
      {#snippet actions()}
        <Button
          variant="outline"
          onclick={() => onretry(selectedSummary.id)}
        >
          Retry detail
        </Button>
      {/snippet}
    </EmptyState>
  {:else if selectedSummary && (selectedDetailLoading || !selectedAddon)}
    <div class="detail-loading" aria-label="Loading addon detail" aria-busy="true">
      <span></span>
      <span></span>
      <span></span>
      <p>Loading {selectedSummary.name}</p>
    </div>
  {:else if selectedAddon}
    <header class="detail-header">
      <div>
        <p class="detail-eyebrow">
          {readableLabel(selectedAddon.kind ?? "toolkit")}
          / {selectedAddon.id}
        </p>
        <h2>{selectedAddon.name}</h2>
        <p class="detail-byline">
          {#if selectedAddon.builtin_reconciliation_required}
            Installed {selectedAddon.builtin_reconciliation_required.installed_version}
            · Package {selectedAddon.builtin_reconciliation_required.package_version}
          {:else}
            Version {selectedAddon.version}
          {/if}
          · {selectedAddon.author || "Unknown author"}
        </p>
      </div>
      <div class="detail-status">
        <Badge tone={statusTone(selectedAddon)}>
          {addonStatusLabel(selectedAddon)}
        </Badge>
        {#if selectedAddon.frontend_build?.declared}
          <Badge tone={buildTone(selectedAddon.frontend_build.status)}>
            UI {readableLabel(selectedAddon.frontend_build.status)}
          </Badge>
        {/if}
      </div>
    </header>

    <p class="detail-description">
      {selectedAddon.description || "This addon does not provide a description."}
    </p>

    {#if selectedAddon.builtin_reconciliation_required}
      <section class="attention-note">
        <Icon name="activity" size={18} />
        <div>
          <strong>
            {selectedAddon.is_active
              ? "Built-in update requires maintenance"
              : "Built-in update deferred"}
          </strong>
          <p>
            {#if selectedAddon.is_active}
              Carlina has left this add-on enabled in persisted state but
              will not import the new package until its schema and upgrade
              hook complete. Stop the managed runtime and run
              <code>carlina init --repair</code>.
            {:else}
              This inactive add-on retains its earlier schema and version.
              Carlina will reconcile it safely when you next activate it.
            {/if}
          </p>
          {#if selectedAddon.builtin_reconciliation_required.error}
            <p>{selectedAddon.builtin_reconciliation_required.error}</p>
          {/if}
        </div>
      </section>
    {/if}

    {#if addonNeedsAttention(selectedAddon) && selectedAddon.status.reasons?.length}
      <section class="attention-note">
        <Icon name="activity" size={18} />
        <div>
          <strong>Package health needs attention</strong>
          <ul>
            {#each selectedAddon.status.reasons as reason}
              <li>{reason}</li>
            {/each}
          </ul>
        </div>
      </section>
    {/if}

    {#if selectedDoctorChecks.length}
      <section class="selected-doctor" aria-label="Selected addon diagnostics">
        <header>
          <Icon name="activity" size={17} />
          <strong>Doctor</strong>
        </header>
        <ul>
          {#each selectedDoctorChecks as check (check.id)}
            <li>
              <Badge tone={check.status === "error" ? "danger" : check.status === "warning" ? "warning" : check.status === "ok" ? "success" : "neutral"}>
                {readableLabel(check.status)}
              </Badge>
              <span><strong>{check.title}</strong>{check.summary}</span>
            </li>
          {/each}
        </ul>
      </section>
    {/if}

    <div class="detail-actions">
      {#if isAdmin}
        {#if selectedAddon.is_active}
          <Button
            variant="outline"
            disabled={actionPending}
            onclick={() => requestLifecycleAction("deactivate", selectedAddon)}
          >
            <Icon name="stop" size={15} />
            Deactivate
          </Button>
        {:else}
          <Button
            disabled={actionPending || selectedAddon.code_available === false}
            onclick={() => requestLifecycleAction("activate", selectedAddon)}
          >
            <Icon name="check" size={15} />
            Activate
          </Button>
        {/if}
      {/if}
      {#if selectedAddon.has_settings}
        <Button
          variant="outline"
          onclick={onsettings}
        >
          <Icon name="settings" size={14} />
          Addon settings
        </Button>
      {/if}
    </div>

    <div class="detail-grid">
      <section class="detail-section">
        <div class="section-number">01</div>
        <p class="section-eyebrow">Capability map</p>
        <h3>Purpose and relationships</h3>
        <dl>
          <div>
            <dt>Package type</dt>
            <dd>{readableLabel(selectedAddon.kind ?? "toolkit")}</dd>
          </div>
          <div>
            <dt>Distribution</dt>
            <dd>
              {readableLabel(selectedAddon.distribution?.channel ?? selectedAddon.source_kind)}
              {#if selectedAddon.distribution?.publisher}
                · {selectedAddon.distribution.publisher}
              {/if}
            </dd>
          </div>
          <div>
            <dt>Roles</dt>
            <dd>
              {#if selectedAddon.roles.length}
                {#each selectedAddon.roles as role}
                  <span>{readableLabel(role)}</span>
                {/each}
              {:else}
                Not declared
              {/if}
            </dd>
          </div>
          <div>
            <dt>Categories</dt>
            <dd>
              {#if selectedAddon.categories.length}
                {#each selectedAddon.categories as category}
                  <span>{readableLabel(category)}</span>
                {/each}
              {:else}
                Not declared
              {/if}
            </dd>
          </div>
          <div>
            <dt>Provider domains</dt>
            <dd>
              {#if selectedAddon.provider_domains.length}
                {#each selectedAddon.provider_domains as domain}
                  <span>{readableLabel(domain)}</span>
                {/each}
              {:else}
                None
              {/if}
            </dd>
          </div>
          <div>
            <dt>Dependencies</dt>
            <dd>
              {#if selectedAddon.dependencies.length}
                {#each selectedAddon.dependencies as dependency}
                  <button
                    class="dependency-link"
                  onclick={() => ondependency(dependency)}
                  >
                    {dependency}
                  </button>
                {/each}
              {:else}
                Self-contained
              {/if}
            </dd>
          </div>
          {#if selectedAddon.inferred_hub.relation !== "standalone"}
            <div>
              <dt>Hub relation</dt>
              <dd>
                {selectedAddon.inferred_hub.relation === "hub"
                  ? `${selectedAddon.inferred_hub.spoke_count} connected providers`
                  : `Connected to ${selectedAddon.inferred_hub.hub_label ?? selectedAddon.inferred_hub.hub_id}`}
              </dd>
            </div>
          {/if}
        </dl>
        <h4>Required capabilities</h4>
        {#if selectedAddon.capability_requirements?.length}
          <ul class="permission-list">
            {#each selectedAddon.capability_requirements as requirement}
              <li>
                <strong>{requirement.capability}</strong>
                <span>
                  {readableLabel(requirement.cardinality)} ·
                  {readableLabel(requirement.activation)} ·
                  {readableLabel(requirement.scope)}
                </span>
                {#if requirement.operations.length}
                  <span>{requirement.operations.join(", ")}</span>
                {/if}
              </li>
            {/each}
          </ul>
        {:else}
          <p class="runtime-empty">No external capabilities are required.</p>
        {/if}
        <h4>Provided capabilities</h4>
        {#if selectedAddon.capability_providers?.length}
          <ul class="permission-list">
            {#each selectedAddon.capability_providers as provider}
              <li>
                <strong>{provider.capability}</strong>
                <span>v{provider.version} · {provider.id}</span>
                {#if provider.operations.length}
                  <span>{provider.operations.join(", ")}</span>
                {/if}
              </li>
            {/each}
          </ul>
        {:else}
          <p class="runtime-empty">No capability providers are declared.</p>
        {/if}
        {#if capabilityLoading}
          <p class="runtime-empty">Resolving installed providers…</p>
        {:else if capabilityResolution?.resolutions.length}
          <h4>Effective provider choices</h4>
          <div class="capability-resolution-list">
            {#each capabilityResolution.resolutions as resolution (resolution.requirement.id)}
              <article class:resolution--blocking={resolution.blocks_activation}>
                <header>
                  <strong>{resolution.requirement.capability}</strong>
                  <Badge tone={resolution.status === "resolved" ? "success" : resolution.blocks_activation ? "warning" : "neutral"}>
                    {readableLabel(resolution.status)}
                  </Badge>
                </header>
                <p>{resolution.message}</p>
                {#each resolution.compatible as candidate (`${candidate.addon_id}:${candidate.provider_id}`)}
                  {@const candidateConnections = connections.filter(
                    (connection) =>
                      connection.connector_addon_id === candidate.addon_id &&
                      (!candidate.connector_definition ||
                        connection.connector_definition_id === candidate.connector_definition),
                  )}
                  {#if candidate.connector_definition}
                    {#each candidateConnections as connection (connection.id)}
                      {@const isSelected = resolution.selected_bindings.some(
                        (binding) =>
                          binding.provider_addon_id === candidate.addon_id &&
                          binding.provider_capability_id === candidate.provider_id &&
                          binding.connection_id === connection.id,
                      )}
                      <button
                        class="provider-choice"
                        class:provider-choice--selected={isSelected}
                        disabled={capabilityBindingPending}
                        aria-pressed={isSelected}
                        onclick={() => bindCapability(resolution.requirement.id, candidate, connection.id, !isSelected)}
                      >
                        <span>
                          <strong>{connection.label}</strong>
                          <small>{isSelected ? "Selected" : candidate.addon_id} · {readableLabel(connection.access)}</small>
                        </span>
                        <Icon name={isSelected ? "check" : "arrow-right"} size={14} />
                      </button>
                    {:else}
                      <button
                        class="provider-choice"
                        onclick={() => openProviderSetup(candidate)}
                      >
                        <span>
                          <strong>{candidate.setup?.label ?? `Configure ${candidate.addon_id}`}</strong>
                          <small>No compatible Connection exists yet.</small>
                        </span>
                        <Icon name="settings" size={14} />
                      </button>
                    {/each}
                  {:else}
                    {@const isSelected = resolution.selected_bindings.some(
                      (binding) =>
                        binding.provider_addon_id === candidate.addon_id &&
                        binding.provider_capability_id === candidate.provider_id &&
                        !binding.connection_id,
                    )}
                    <button
                      class="provider-choice"
                      class:provider-choice--selected={isSelected}
                      disabled={capabilityBindingPending}
                      aria-pressed={isSelected}
                      onclick={() => bindCapability(resolution.requirement.id, candidate, undefined, !isSelected)}
                    >
                      <span>
                        <strong>{candidate.addon_id}</strong>
                        <small>{isSelected ? "Selected" : candidate.provider_id}</small>
                      </span>
                      <Icon name={isSelected ? "check" : "arrow-right"} size={14} />
                    </button>
                  {/if}
                {/each}
                {#if resolution.inactive_compatible.length}
                  <small class="inactive-provider-note">
                    {resolution.inactive_compatible.length} compatible provider{resolution.inactive_compatible.length === 1 ? " is" : "s are"} installed but inactive.
                  </small>
                {/if}
              </article>
            {/each}
          </div>
        {/if}
        {#if selectedConnectorConnections.length}
          <h4>Connections</h4>
          <ul class="permission-list">
            {#each selectedConnectorConnections as connection (connection.id)}
              <li>
                <strong>{connection.label}</strong>
                <span>{readableLabel(connection.state)} · {readableLabel(connection.access)}</span>
                <span>{connection.binding_count} active binding{connection.binding_count === 1 ? "" : "s"}</span>
              </li>
            {/each}
          </ul>
        {/if}
      </section>

      <section class="detail-section">
        <div class="section-number">02</div>
        <p class="section-eyebrow">Trust contract</p>
        <h3>Execution and permissions</h3>
        {#if effectiveCodeAccess(selectedAddon) === "host_process"}
          <p class="trust-warning">
            This is native trusted code with the same effective host
            access as Carlina. Manifest permissions are review metadata,
            not a Python sandbox.
          </p>
        {/if}
        <dl>
          <div>
            <dt>Source</dt>
            <dd>{readableLabel(selectedAddon.source_kind)}</dd>
          </div>
          <div>
            <dt>Distribution</dt>
            <dd>{readableLabel(selectedAddon.distribution?.channel)}</dd>
          </div>
          <div>
            <dt>Effective code access</dt>
            <dd>{codeAccessLabel(effectiveCodeAccess(selectedAddon))}</dd>
          </div>
          <div>
            <dt>Lifecycle</dt>
            <dd>{readableLabel(selectedAddon.lifecycle_state)}</dd>
          </div>
          <div>
            <dt>Signature</dt>
            <dd>{readableLabel(String(selectedAddon.provenance?.signature_status ?? "not recorded"))}</dd>
          </div>
        </dl>
        <h4>Declared permissions</h4>
        {#if selectedAddon.permissions?.length}
          <ul class="permission-list">
            {#each selectedAddon.permissions as permission}
              <li>
                <strong>{permission.id}</strong>
                <span>{permission.reason || "No reason supplied."}</span>
              </li>
            {/each}
          </ul>
        {:else}
          <p class="runtime-empty">No host permissions were declared.</p>
        {/if}
        {#if selectedAddon.contract_sha256}
          <p class="hash-reading">
            Contract SHA-256 <code>{selectedAddon.contract_sha256}</code>
          </p>
        {/if}
        {#if selectedAddon.dependency_notices?.length}
          <h4>Optional dependency license review</h4>
          <ul class="permission-list">
            {#each selectedAddon.dependency_notices as dependencyNotice}
              <li>
                <strong>{dependencyNotice.packages.join(", ")}</strong>
                <span>{dependencyNotice.license}</span>
                <span>{dependencyNotice.purpose}</span>
              </li>
            {/each}
          </ul>
        {/if}
      </section>

      <section class="detail-section">
        <div class="section-number">03</div>
        <p class="section-eyebrow">Interface contract</p>
        <h3>Delivery and build</h3>
        {#if selectedAddon.ui_entries?.length}
          <ul class="permission-list">
            {#each selectedAddon.ui_entries as entry}
              <li>
                <strong>{readableLabel(entry.mode)}</strong>
                <span>{entry.route || entry.module || entry.id}</span>
              </li>
            {/each}
          </ul>
        {/if}
        {#if selectedAddon.frontend_build?.declared}
          <div class="build-reading">
            <strong>{readableLabel(selectedAddon.frontend_build.status)}</strong>
            <span>
              {selectedAddon.frontend_build.validation_mode === "full" &&
              selectedAddon.frontend_build.stamp_current
                ? "Source fingerprint verified"
                : selectedAddon.frontend_build.stamp_current
                  ? "Cached build metadata"
                : selectedAddon.frontend_build.rebuild_needed
                  ? "Rebuild required"
                  : "Build declaration present"}
            </span>
          </div>
          <dl>
            <div>
              <dt>Legacy projection</dt>
              <dd>{readableLabel(selectedAddon.frontend_tier ?? selectedAddon.frontend_build.tier)}</dd>
            </div>
            <div>
              <dt>Declared outputs</dt>
              <dd>{selectedAddon.frontend_build.output_paths?.length ?? 0}</dd>
            </div>
          </dl>
          {#if selectedAddon.frontend_build.reasons?.length || selectedAddon.frontend_build.warnings?.length}
            <ul class="build-notes">
              {#each [
                ...(selectedAddon.frontend_build.reasons ?? []),
                ...(selectedAddon.frontend_build.warnings ?? []),
              ] as reason}
                <li>{reason}</li>
              {/each}
            </ul>
          {/if}
        {:else}
          <div class="muted-reading">
            <Icon name="file" size={18} />
            <p>
              No independent frontend build is declared for this package.
              It may be a service-only addon.
            </p>
          </div>
        {/if}
      </section>

      <section class="detail-section detail-section--wide">
        <div class="section-number">04</div>
        <p class="section-eyebrow">Runtime</p>
        <h3>Declared units, workers, and schedules</h3>
        {#if selectedAddon.runtime_units?.length}
          <div class="runtime-unit-grid">
            {#each selectedAddon.runtime_units as unit}
              <article class="runtime-unit">
                <header>
                  <strong>{unit.id}</strong>
                  <Badge tone={unit.mode === "container" ? "warning" : "neutral"}>
                    {readableLabel(unit.mode)}
                  </Badge>
                </header>
                <p>{readableLabel(unit.lifecycle || "host_managed")}</p>
                {#if Object.keys(unit.requirements ?? {}).length}
                  <small>{Object.keys(unit.requirements ?? {}).join(", ")}</small>
                {/if}
              </article>
            {/each}
          </div>
        {:else}
          <p class="runtime-empty">This legacy package has no explicit runtime units yet.</p>
        {/if}
        {#if selectedAddon.database_migration}
          <article class="runtime-unit">
            <header>
              <strong>Database migrations</strong>
              <Badge tone="warning">Host process</Badge>
            </header>
            <p>
              {selectedAddon.database_migration.revision_count} reviewed revision{selectedAddon.database_migration.revision_count === 1 ? "" : "s"}
              · head {selectedAddon.database_migration.head_revision}
            </p>
            <small>{selectedAddon.database_migration.version_table}</small>
          </article>
        {/if}
        <div class="runtime-columns">
          <div class="runtime-card">
            <header>
              <div>
                <p>Worker processes</p>
                <strong>{selectedAddon.worker_state.running} running</strong>
              </div>
              <Badge tone={selectedAddon.worker_state.running ? "success" : "neutral"}>
                {readableLabel(selectedAddon.worker_state.status)}
              </Badge>
            </header>
            <dl>
              <div>
                <dt>Configured</dt>
                <dd>{selectedAddon.worker_state.configured}</dd>
              </div>
              <div>
                <dt>Desired</dt>
                <dd>{selectedAddon.worker_state.desired ?? "Automatic"}</dd>
              </div>
              <div>
                <dt>Entrypoint</dt>
                <dd>{selectedAddon.worker_state.entrypoint ?? "Not running"}</dd>
              </div>
            </dl>
            {#if selectedAddon.worker_state.pids.length}
              <p class="pid-list">
                Process IDs:
                {selectedAddon.worker_state.pids.join(", ")}
              </p>
            {/if}
            {#if isAdmin && selectedAddon.is_active && selectedAddon.worker_state.configured}
              <footer>
                {#if selectedAddon.worker_state.running}
                  <Button
                    size="sm"
                    variant="outline"
                    onclick={() => requestWorkerAction("stop-workers", selectedAddon)}
                  >
                    Stop workers
                  </Button>
                {/if}
                <Button
                  size="sm"
                  variant="outline"
                  onclick={() => requestWorkerAction("restart-workers", selectedAddon)}
                >
                  Restart workers
                </Button>
              </footer>
            {/if}
          </div>

          <div class="runtime-card">
            <header>
              <div>
                <p>Scheduled jobs</p>
                <strong>{selectedAddon.scheduled_job_state.registered} registered</strong>
              </div>
              <Badge tone={selectedAddon.scheduled_job_state.registered ? "success" : "neutral"}>
                {readableLabel(selectedAddon.scheduled_job_state.status)}
              </Badge>
            </header>
            {#if selectedAddon.scheduled_job_state.jobs.length}
              <div class="job-list">
                {#each selectedAddon.scheduled_job_state.jobs as job (job.id)}
                  <article>
                    <div>
                      <strong>{job.name || job.id}</strong>
                      <small>{dateLabel(job.next_run_at)} · {job.trigger}</small>
                    </div>
                    <Badge tone={job.paused ? "warning" : "success"}>
                      {job.paused ? "Paused" : "Scheduled"}
                    </Badge>
                    {#if isAdmin}
                      <div class="job-actions">
                        <button
                          onclick={() =>
                            requestJobAction(
                              job.paused ? "resume-job" : "pause-job",
                              selectedAddon,
                              job.id,
                            )}
                        >
                          {job.paused ? "Resume" : "Pause"}
                        </button>
                        <button
                          onclick={() =>
                            requestJobAction("run-job", selectedAddon, job.id)}
                        >
                          Run now
                        </button>
                      </div>
                    {/if}
                  </article>
                {/each}
              </div>
            {:else}
              <p class="runtime-empty">
                {selectedAddon.scheduled_job_state.declared
                  ? "Jobs are declared but not registered in this process."
                  : "This addon does not declare scheduled work."}
              </p>
            {/if}
          </div>
        </div>
      </section>

      <section class="detail-section">
        <div class="section-number">05</div>
        <p class="section-eyebrow">Observability</p>
        <h3>Registered logs</h3>
        {#if selectedAddon.log_links.length}
          <div class="log-list">
            {#each selectedAddon.log_links as link (link.id)}
              <a
                href={link.url}
                onclick={(event) => activateHref(link.url, event)}
              >
                <span>
                  <strong>{link.name}</strong>
                  <small>{readableLabel(link.category)}</small>
                </span>
                <Icon name="arrow-right" size={15} />
              </a>
            {/each}
          </div>
        {:else}
          <div class="muted-reading">
            <Icon name="logs" size={18} />
            <p>No addon-specific log sources are currently registered.</p>
          </div>
        {/if}
      </section>

      <section class="detail-section">
        <div class="section-number">06</div>
        <p class="section-eyebrow">Package administration</p>
        <h3>Removal boundary</h3>
        {#if isAdmin}
          {#if selectedAddon.can_remove_code}
            <p class="danger-copy">
              Remove the installed package while preserving its data,
              schema, settings, and lifecycle history.
            </p>
            <Button
              variant="outline"
              disabled={actionPending}
              onclick={() => requestLifecycleAction("uninstall", selectedAddon)}
            >
              <Icon name="trash" size={15} />
              Remove code
            </Button>
          {/if}
          {#if selectedAddon.can_purge_data}
            <p class="danger-copy">
              Purge is the separate destructive action. The add-on must
              remain inactive; Carlina creates a safety backup first.
            </p>
            <Button
              variant="danger"
              disabled={actionPending || selectedAddon.is_active}
              onclick={() => requestLifecycleAction("purge", selectedAddon)}
            >
              <Icon name="trash" size={15} />
              Purge add-on data
            </Button>
          {:else if !selectedAddon.can_remove_code}
            <div class="muted-reading">
              <Icon name="shield" size={18} />
              <p>No removable code or retained data is available.</p>
            </div>
          {/if}
        {:else}
          <div class="muted-reading">
            <Icon name="shield" size={18} />
            <p>Lifecycle administration is restricted to administrators.</p>
          </div>
        {/if}
      </section>
    </div>
  {:else}
    <EmptyState
      icon="search"
      title="No addon selected"
      description="Choose an addon from the directory to inspect its capabilities and runtime."
    />
  {/if}
</section>
