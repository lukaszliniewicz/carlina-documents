<script lang="ts">
  import type {
    FrontendAddonCatalogPackage,
    FrontendAddonCatalogResponse,
    FrontendAddonDependencyNotice,
  } from "@carlina/sdk";
  import { Badge, Button, Dialog, Icon } from "@carlina/ui";

  import {
    addonByteLabel,
    addonCatalogRecordLabel,
    addonCatalogStatusLabel,
    addonDateLabel,
    readableAddonLabel,
  } from "../../../lib/addon-manager.ts";

  interface Props {
    open: boolean;
    catalog: FrontendAddonCatalogResponse | null;
    loading: boolean;
    error: string;
    selectedPackage: FrontendAddonCatalogPackage | null;
    dependencyNotices: readonly FrontendAddonDependencyNotice[];
    hasHostCode: boolean;
    trustApproved: boolean;
    dependencyApproved: boolean;
    installPending: boolean;
    onclose: () => void;
    onselect: (addon: FrontendAddonCatalogPackage) => void;
    oninstall: () => void;
  }

  let {
    open: catalogOpen,
    catalog,
    loading: catalogLoading,
    error: catalogError,
    selectedPackage: selectedCatalogPackage,
    dependencyNotices: catalogDependencyNotices,
    hasHostCode: catalogHasHostCode,
    trustApproved: catalogTrustApproved = $bindable(),
    dependencyApproved: catalogDependencyApproved = $bindable(),
    installPending: catalogInstallPending,
    onclose: closeCatalog,
    onselect,
    oninstall,
  }: Props = $props();

  const readableLabel = readableAddonLabel;
  const dateLabel = addonDateLabel;
  const byteLabel = addonByteLabel;
  const catalogStatusLabel = addonCatalogStatusLabel;
  const catalogRecordLabel = addonCatalogRecordLabel;

  function selectCatalogPackage(addon: FrontendAddonCatalogPackage): void {
    onselect(addon);
  }

  function installCatalogAddon(): void {
    oninstall();
  }
</script>

<Dialog
  open={catalogOpen}
  title="First-party addon catalog"
  description="Review the authenticated publisher, exact release digest, permissions, runtimes, and dependency licenses before installing."
  onclose={closeCatalog}
  size="lg"
>
  {#if catalogLoading && !catalog}
    <section class="catalog-loading" aria-label="Loading addon catalog" aria-busy="true">
      <span></span><span></span><span></span>
    </section>
  {:else if !catalog?.configured}
    <div class="catalog-empty">
      <Icon name="shield" size={24} />
      <h3>First-party catalog is not configured</h3>
      <p>
        Carlina is operating without a remote addon source. An administrator can
        configure a signed catalog URL and publisher-scoped trust key; local ZIP
        installation remains available separately.
      </p>
    </div>
  {:else if catalog.packages.length === 0}
    <div class="catalog-empty">
      <Icon name="search" size={24} />
      <h3>No compatible releases</h3>
      <p>
        The authenticated {catalog.channel} catalog contains no release compatible
        with this version of the Carlina addon API.
      </p>
    </div>
  {:else}
    <div class="catalog-shell">
      <aside class="catalog-directory" aria-label="Catalog packages">
        <header>
          <span>Authenticated source</span>
          <strong>{catalog.publisher}</strong>
          <small>{catalog.channel} channel · {catalog.packages.length} packages</small>
        </header>
        <div class="catalog-list">
          {#each catalog.packages as addon (addon.id)}
            <button
              class:selected={selectedCatalogPackage?.id === addon.id}
              aria-pressed={selectedCatalogPackage?.id === addon.id}
              onclick={() => selectCatalogPackage(addon)}
            >
              <span>
                <strong>{addon.name}</strong>
                <small>{addon.summary}</small>
              </span>
              <Badge
                tone={addon.status === "update_available"
                  ? "warning"
                  : addon.status === "available"
                    ? "success"
                    : "neutral"}
              >
                {catalogStatusLabel(addon.status)}
              </Badge>
            </button>
          {/each}
        </div>
      </aside>

      <section class="catalog-review" aria-label="Package installation review">
        {#if selectedCatalogPackage}
          {@const addon = selectedCatalogPackage}
          <header class="catalog-review-header">
            <div>
              <span>{readableLabel(addon.kind)} · {addon.license}</span>
              <h3>{addon.name}</h3>
              <p>{addon.summary}</p>
            </div>
            <Badge
              tone={addon.status === "update_available" ? "warning" : "neutral"}
            >
              {addon.installed_version
                ? `${addon.installed_version} → ${addon.target?.version ?? "—"}`
                : addon.target?.version ?? "No compatible release"}
            </Badge>
          </header>

          {#if addon.target}
            {@const target = addon.target}
            <dl class="catalog-metadata">
              <div>
                <dt>Publisher</dt>
                <dd>{catalog.publisher}</dd>
              </div>
              <div>
                <dt>Signed by</dt>
                <dd><code>{catalog.signer_key_id ?? "Local development catalog"}</code></dd>
              </div>
              <div>
                <dt>Published</dt>
                <dd>{dateLabel(target.published_at)}</dd>
              </div>
              <div>
                <dt>Package</dt>
                <dd>{target.archive.filename} · {byteLabel(target.archive.size)}</dd>
              </div>
              <div>
                <dt>Archive SHA-256</dt>
                <dd><code>{target.archive.sha256}</code></dd>
              </div>
              <div>
                <dt>Contract SHA-256</dt>
                <dd><code>{target.contract.contract_sha256}</code></dd>
              </div>
              <div>
                <dt>Source</dt>
                <dd><code>{target.contract.distribution.source_uri ?? addon.distribution.source_uri ?? "Not declared"}</code></dd>
              </div>
              <div>
                <dt>Addon API</dt>
                <dd><code>{target.addon_api_requires}</code></dd>
              </div>
            </dl>

            {#if addon.status === "update_available" && addon.update_diff}
              <section class="catalog-change-review">
                <div class="catalog-section-heading">
                  <div>
                    <span>Update contract</span>
                    <h4>Changes since {addon.installed_version}</h4>
                  </div>
                  <Badge tone={addon.update_diff.requires_review ? "warning" : "success"}>
                    {addon.update_diff.requires_review
                      ? "Review required"
                      : "No expanded access"}
                  </Badge>
                </div>
                <div class="catalog-diff-grid">
                  {#each [
                    { label: "Permissions", diff: addon.update_diff.permissions },
                    { label: "Resources", diff: addon.update_diff.resources },
                    { label: "Runtime units", diff: addon.update_diff.runtime_units },
                    {
                      label: "Database migration",
                      diff: addon.update_diff.database_migration,
                    },
                    {
                      label: "Dependency licenses",
                      diff: addon.update_diff.dependency_notices,
                    },
                  ] as section}
                    <article>
                      <h5>{section.label}</h5>
                      {#if section.diff.added.length === 0 && section.diff.removed.length === 0 && section.diff.changed.length === 0}
                        <p>No changes</p>
                      {:else}
                        <ul>
                          {#each section.diff.added as item}
                            <li class="diff-added">Added: {catalogRecordLabel(item)}</li>
                          {/each}
                          {#each section.diff.removed as item}
                            <li class="diff-removed">Removed: {catalogRecordLabel(item)}</li>
                          {/each}
                          {#each section.diff.changed as item}
                            <li class="diff-changed">Changed: {catalogRecordLabel(item.after)}</li>
                          {/each}
                        </ul>
                      {/if}
                    </article>
                  {/each}
                </div>
                {#if addon.update_diff.distribution.changed}
                  <p class="catalog-warning">
                    The declared distribution identity changed. Confirm the publisher
                    and source repository before continuing.
                  </p>
                {/if}
              </section>
            {/if}

            <div class="catalog-contract-grid">
              <section>
                <div class="catalog-section-heading">
                  <div>
                    <span>Host access</span>
                    <h4>Permissions and resources</h4>
                  </div>
                </div>
                {#if target.contract.permissions.length || target.contract.resources.length}
                  <ul class="catalog-contract-list">
                    {#each target.contract.permissions as permission}
                      <li>
                        <strong>{permission.id}</strong>
                        <span>{permission.reason ?? "No reason declared"}</span>
                      </li>
                    {/each}
                    {#each target.contract.resources as resource}
                      <li>
                        <strong>{resource.id}</strong>
                        <span>{resource.access?.join(", ") || resource.description || "Declared resource"}</span>
                      </li>
                    {/each}
                  </ul>
                {:else}
                  <p class="catalog-muted">No host permissions or resources declared.</p>
                {/if}
              </section>
              <section>
                <div class="catalog-section-heading">
                  <div>
                    <span>Execution</span>
                    <h4>Runtime units</h4>
                  </div>
                </div>
                {#if target.contract.runtime_units.length}
                  <ul class="catalog-contract-list">
                    {#each target.contract.runtime_units as runtimeUnit}
                      <li>
                        <strong>{runtimeUnit.id}</strong>
                        <span>{readableLabel(runtimeUnit.mode)}</span>
                      </li>
                    {/each}
                  </ul>
                {:else}
                  <p class="catalog-muted">No executable runtime units declared.</p>
                {/if}
              </section>
            </div>

            {#if catalogDependencyNotices.length}
              <section class="catalog-dependencies">
                <div class="catalog-section-heading">
                  <div>
                    <span>Third-party software</span>
                    <h4>Dependency license review</h4>
                  </div>
                </div>
                <ul class="catalog-contract-list">
                  {#each catalogDependencyNotices as dependencyNotice}
                    <li>
                      <strong>{dependencyNotice.packages.join(", ")}</strong>
                      <span>{dependencyNotice.license} · {dependencyNotice.purpose}</span>
                    </li>
                  {/each}
                </ul>
                <label class="trust-approval">
                  <input type="checkbox" bind:checked={catalogDependencyApproved} />
                  <span>I reviewed these dependency licenses and approve their installation.</span>
                </label>
              </section>
            {/if}

            {#if catalogHasHostCode}
              <p class="catalog-warning">
                This package declares Python that executes inside Carlina—an
                in-process runtime, database migrations, or both. It receives the
                same files, environment, network, and application-data access as
                the host process.
              </p>
            {/if}
            <label class="trust-approval catalog-final-approval">
              <input type="checkbox" bind:checked={catalogTrustApproved} />
              <span>
                I reviewed the authenticated publisher, exact hashes, permission
                contract, runtime modes, and source identity for this release.
              </span>
            </label>
          {:else}
            <div class="catalog-empty catalog-empty--embedded">
              <Icon name="activity" size={22} />
              <h3>No compatible release</h3>
              <p>This package does not support the current Carlina addon API.</p>
            </div>
          {/if}
        {/if}
      </section>
    </div>
  {/if}

  {#if catalogError}
    <p class="page-feedback page-feedback--error catalog-feedback" role="alert">
      {catalogError}
    </p>
  {/if}

  {#snippet footer()}
    <Button variant="quiet" disabled={catalogInstallPending} onclick={closeCatalog}>
      Close
    </Button>
    <Button
      disabled={catalogInstallPending ||
        !selectedCatalogPackage?.target ||
        !["available", "update_available"].includes(
          selectedCatalogPackage?.status ?? "",
        ) ||
        !catalogTrustApproved ||
        (catalogDependencyNotices.length > 0 && !catalogDependencyApproved)}
      onclick={() => void installCatalogAddon()}
    >
      {catalogInstallPending
        ? "Verifying and installing…"
        : selectedCatalogPackage?.status === "update_available"
          ? `Update to ${selectedCatalogPackage.target?.version ?? "release"}`
          : "Install signed release"}
    </Button>
  {/snippet}
</Dialog>
