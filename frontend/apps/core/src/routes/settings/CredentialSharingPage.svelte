<script lang="ts">
  import { onMount } from "svelte";
  import type {
    FrontendApiClient,
    FrontendCredentialDelegation,
    FrontendCredentialSharingDashboard,
    FrontendSettingsMutationResponse,
    FrontendSharingCredential,
    FrontendSharingProvider,
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
    availableSharingCredentials,
    filterDelegations,
    formatSettingsDate,
  } from "../../lib/model-settings.ts";

  interface Props {
    api: FrontendApiClient;
  }

  interface CredentialChoice {
    key: string;
    provider: FrontendSharingProvider;
    credential: FrontendSharingCredential;
  }

  let { api }: Props = $props();

  let dashboard = $state<FrontendCredentialSharingDashboard | null>(null);
  let loading = $state(true);
  let refreshing = $state(false);
  let submitting = $state(false);
  let error = $state("");
  let notice = $state("");
  let query = $state("");
  let shareOpen = $state(false);
  let removeTarget = $state<FrontendCredentialDelegation | null>(null);
  let selectedCredentialKeys = $state<Set<string>>(new Set());
  let selectedRecipientIds = $state<Set<number>>(new Set());

  const configuredProviders = $derived(
    dashboard?.providers.filter((provider) => provider.credentials.length > 0) ??
      [],
  );
  const filteredDelegations = $derived(
    filterDelegations(dashboard?.delegations ?? [], query),
  );
  const credentialCount = $derived(
    availableSharingCredentials(dashboard?.providers ?? []),
  );
  const selectedCredentials = $derived.by(() => {
    const choices: CredentialChoice[] = [];
    for (const provider of dashboard?.providers ?? []) {
      for (const credential of provider.credentials) {
        const key = credentialKey(provider.id, credential.id);
        if (selectedCredentialKeys.has(key)) {
          choices.push({ key, provider, credential });
        }
      }
    }
    return choices;
  });
  const selectedRecipients = $derived(
    (dashboard?.recipients ?? []).filter((recipient) =>
      selectedRecipientIds.has(recipient.id),
    ),
  );

  onMount(() => {
    void loadDashboard();
  });

  function credentialKey(
    providerId: string,
    credentialId: number | null,
  ): string {
    return `${providerId}:${credentialId ?? "settings"}`;
  }

  async function loadDashboard(options: { quiet?: boolean } = {}): Promise<void> {
    if (!options.quiet) loading = true;
    error = "";
    try {
      dashboard =
        await api.request<FrontendCredentialSharingDashboard>(
          "/api/frontend/settings/sharing",
        );
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "Credential sharing could not be loaded.";
    } finally {
      loading = false;
      refreshing = false;
    }
  }

  async function refreshDesk(): Promise<void> {
    refreshing = true;
    notice = "";
    await loadDashboard({ quiet: true });
  }

  function openShareDialog(): void {
    error = "";
    notice = "";
    selectedCredentialKeys = new Set();
    selectedRecipientIds = new Set();
    shareOpen = true;
  }

  function closeShareDialog(): void {
    if (submitting) return;
    shareOpen = false;
  }

  function chooseCredential(
    provider: FrontendSharingProvider,
    credential: FrontendSharingCredential,
  ): void {
    const key = credentialKey(provider.id, credential.id);
    const next = new Set(
      [...selectedCredentialKeys].filter(
        (item) => !item.startsWith(`${provider.id}:`),
      ),
    );
    if (!selectedCredentialKeys.has(key)) next.add(key);
    selectedCredentialKeys = next;
  }

  function toggleRecipient(recipientId: number): void {
    const next = new Set(selectedRecipientIds);
    if (next.has(recipientId)) next.delete(recipientId);
    else next.add(recipientId);
    selectedRecipientIds = next;
  }

  async function createDelegations(): Promise<void> {
    if (
      !dashboard ||
      selectedCredentials.length === 0 ||
      selectedRecipients.length === 0
    ) {
      error = "Choose at least one configured account and one recipient.";
      return;
    }
    submitting = true;
    error = "";
    notice = "";
    try {
      const response = await api.request<FrontendSettingsMutationResponse>(
        dashboard.actions.create_url,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            delegatee_ids: selectedRecipients.map((recipient) => recipient.id),
            credentials: selectedCredentials.map(({ provider, credential }) => ({
              provider_name: provider.id,
              credential_id: credential.id,
            })),
          }),
        },
      );
      shareOpen = false;
      notice = response.message || "Credential access was updated.";
      await loadDashboard({ quiet: true });
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "Credential access could not be updated.";
    } finally {
      submitting = false;
    }
  }

  async function removeDelegation(): Promise<void> {
    if (!dashboard || !removeTarget) return;
    submitting = true;
    error = "";
    notice = "";
    try {
      const response = await api.request<FrontendSettingsMutationResponse>(
        dashboard.actions.remove_url,
        {
          method: "DELETE",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            delegatee_id: removeTarget.delegatee_id,
            provider_name: removeTarget.provider_id,
          }),
        },
      );
      removeTarget = null;
      notice = response.message || "Shared access was removed.";
      await loadDashboard({ quiet: true });
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "Shared access could not be removed.";
    } finally {
      submitting = false;
    }
  }
</script>

<div class="sharing-page">
  <PageHeader
    eyebrow="Settings / Access"
    title="Credential sharing"
    description="Delegate a configured addon account without revealing its secret. Recipients receive permission to use the connection; passwords, tokens, and provider payloads remain sealed."
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
      <Button
        onclick={openShareDialog}
        disabled={loading || credentialCount === 0 || !dashboard?.summary.recipients}
      >
        <Icon name="plus" size={15} />
        Share access
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
    <section
      class="loading-state"
      aria-label="Loading credential sharing"
      aria-busy="true"
    >
      <span></span><span></span><span></span>
    </section>
  {:else if dashboard}
    <section class="summary-strip" aria-label="Credential sharing summary">
      <article>
        <p>Available accounts</p>
        <strong>{dashboard.summary.credentials}</strong>
        <span>across {dashboard.summary.configured_providers} addons</span>
      </article>
      <article>
        <p>Shared permissions</p>
        <strong>{dashboard.summary.delegations}</strong>
        <span>active delegations</span>
      </article>
      <article>
        <p>Recipients</p>
        <strong>{dashboard.summary.recipients}</strong>
        <span>eligible members</span>
      </article>
      <article>
        <p>Security model</p>
        <strong class="summary-word">Sealed</strong>
        <span>secrets never appear here</span>
      </article>
    </section>

    <div class="sharing-layout">
      <aside class="account-index">
        <div class="section-heading">
          <div>
            <p class="kicker">Account index</p>
            <h2>Configured sources</h2>
          </div>
          <span>{configuredProviders.length}</span>
        </div>

        {#if configuredProviders.length}
          <div class="provider-list">
            {#each configuredProviders as provider}
              <article>
                <div class="provider-name">
                  <span class="provider-mark" aria-hidden="true">
                    {provider.name.slice(0, 2).toUpperCase()}
                  </span>
                  <div>
                    <h3>{provider.name}</h3>
                    <p>{provider.credentials.length} configured {provider.credentials.length === 1 ? "account" : "accounts"}</p>
                  </div>
                </div>
                <Badge tone={provider.configured ? "success" : "neutral"}>
                  {provider.configured ? "Available" : "Not configured"}
                </Badge>
                <ul>
                  {#each provider.credentials as credential}
                    <li>
                      <span>{credential.label}</span>
                      <small>{credential.source_label}</small>
                    </li>
                  {/each}
                </ul>
              </article>
            {/each}
          </div>
        {:else}
          <div class="aside-empty">
            <Icon name="shield" size={19} />
            <p>No delegatable addon accounts are configured yet.</p>
          </div>
        {/if}

        <div class="security-note">
          <Icon name="shield" size={18} />
          <p>
            This page receives account labels and status only. Secret material
            stays encrypted on the server.
          </p>
        </div>
      </aside>

      <main class="delegation-desk">
        <div class="desk-heading">
          <div>
            <p class="kicker">Permission ledger</p>
            <h2>Who can use what</h2>
            <p>Each row is a scoped permission, not a copy of the credential.</p>
          </div>
          <SearchField
            id="delegation-search"
            label="Search shared access"
            placeholder="Search member or addon"
            value={query}
            oninput={(value) => (query = value)}
          />
        </div>

        {#if filteredDelegations.length}
          <div class="delegation-table" role="table" aria-label="Shared credential access">
            <div class="delegation-table__head" role="row">
              <span role="columnheader">Recipient</span>
              <span role="columnheader">Addon account</span>
              <span role="columnheader">Permission</span>
              <span role="columnheader"><span class="sr-only">Actions</span></span>
            </div>
            {#each filteredDelegations as delegation}
              <div class="delegation-row" role="row">
                <div role="cell" class="recipient-cell">
                  <span class="recipient-mark" aria-hidden="true">
                    {delegation.delegatee_email.slice(0, 1).toUpperCase()}
                  </span>
                  <div>
                    <strong>{delegation.delegatee_email}</strong>
                    <small>Workspace member</small>
                  </div>
                </div>
                <div role="cell">
                  <strong>{delegation.provider_name}</strong>
                  <small>{delegation.credential_name}</small>
                </div>
                <div role="cell">
                  <Badge tone="success">Can use</Badge>
                  <small>Secret remains hidden</small>
                </div>
                <div role="cell" class="row-actions">
                  <Button
                    variant="quiet"
                    size="sm"
                    onclick={() => (removeTarget = delegation)}
                    aria-label={`Remove ${delegation.provider_name} access from ${delegation.delegatee_email}`}
                  >
                    <Icon name="trash" size={14} />
                    Remove
                  </Button>
                </div>
              </div>
            {/each}
          </div>
        {:else if dashboard.delegations.length && query}
          <EmptyState
            title="No permissions match"
            description="Try a member email, addon name, or account label."
            icon="search"
          />
        {:else}
          <EmptyState
            title="No credentials are shared"
            description="When a teammate needs to use one of your configured addon accounts, grant a scoped permission here. The underlying secret is never copied into their profile."
            icon="shield"
          >
            {#snippet actions()}
              <Button
                onclick={openShareDialog}
                disabled={credentialCount === 0 || !dashboard?.summary.recipients}
              >
                Share an account
              </Button>
            {/snippet}
          </EmptyState>
        {/if}
      </main>
    </div>
  {/if}
</div>

<Dialog
  open={shareOpen}
  title="Share configured access"
  description="Choose one account per addon, then select the members who may use it."
  onclose={closeShareDialog}
  size="lg"
>
  <div class="share-dialog">
    <section>
      <div class="dialog-section-heading">
        <span>01</span>
        <div>
          <h3>Accounts</h3>
          <p>Selecting another account for the same addon replaces the first choice.</p>
        </div>
      </div>
      <div class="choice-list">
        {#each configuredProviders as provider}
          <fieldset>
            <legend>{provider.name}</legend>
            {#each provider.credentials as credential}
              {@const key = credentialKey(provider.id, credential.id)}
              <label class:selected={selectedCredentialKeys.has(key)}>
                <input
                  type="checkbox"
                  checked={selectedCredentialKeys.has(key)}
                  onchange={() => chooseCredential(provider, credential)}
                />
                <span>
                  <strong>{credential.label}</strong>
                  <small>
                    {credential.source_label}
                    {credential.created_at
                      ? ` · added ${formatSettingsDate(credential.created_at)}`
                      : ""}
                  </small>
                </span>
                <Badge tone={credential.active ? "success" : "neutral"}>
                  {credential.active ? "Ready" : "Inactive"}
                </Badge>
              </label>
            {/each}
          </fieldset>
        {/each}
      </div>
    </section>

    <section>
      <div class="dialog-section-heading">
        <span>02</span>
        <div>
          <h3>Recipients</h3>
          <p>Existing access for the same addon and member will be updated.</p>
        </div>
      </div>
      <div class="recipient-list">
        {#each dashboard?.recipients ?? [] as recipient}
          <label class:selected={selectedRecipientIds.has(recipient.id)}>
            <input
              type="checkbox"
              checked={selectedRecipientIds.has(recipient.id)}
              onchange={() => toggleRecipient(recipient.id)}
            />
            <span class="recipient-mark" aria-hidden="true">
              {recipient.email.slice(0, 1).toUpperCase()}
            </span>
            <strong>{recipient.email}</strong>
          </label>
        {/each}
      </div>
    </section>

    <section class="review-box">
      <div class="dialog-section-heading">
        <span>03</span>
        <div>
          <h3>Review</h3>
          <p>
            {selectedCredentials.length} {selectedCredentials.length === 1 ? "account" : "accounts"}
            × {selectedRecipients.length} {selectedRecipients.length === 1 ? "recipient" : "recipients"}
          </p>
        </div>
      </div>
      {#if selectedCredentials.length && selectedRecipients.length}
        <p>
          This grants permission to use the selected connections. It does not
          reveal, export, or duplicate their secrets.
        </p>
      {:else}
        <p>Complete the account and recipient selections before granting access.</p>
      {/if}
    </section>
  </div>

  {#snippet footer()}
    <Button variant="quiet" onclick={closeShareDialog} disabled={submitting}>
      Cancel
    </Button>
    <Button
      onclick={() => void createDelegations()}
      disabled={submitting || !selectedCredentials.length || !selectedRecipients.length}
    >
      {submitting ? "Granting…" : "Grant access"}
    </Button>
  {/snippet}
</Dialog>

<Dialog
  open={Boolean(removeTarget)}
  title="Remove shared access?"
  description="The member will no longer be able to use this addon through your configured account."
  onclose={() => {
    if (!submitting) removeTarget = null;
  }}
  size="sm"
>
  {#if removeTarget}
    <div class="remove-review">
      <p class="kicker">Permission to remove</p>
      <strong>{removeTarget.delegatee_email}</strong>
      <span>{removeTarget.provider_name} · {removeTarget.credential_name}</span>
      <p>The underlying account and its encrypted credential will not be changed.</p>
    </div>
  {/if}

  {#snippet footer()}
    <Button
      variant="quiet"
      onclick={() => (removeTarget = null)}
      disabled={submitting}
    >
      Keep access
    </Button>
    <Button
      variant="danger"
      onclick={() => void removeDelegation()}
      disabled={submitting}
    >
      {submitting ? "Removing…" : "Remove access"}
    </Button>
  {/snippet}
</Dialog>

<style>
  .sharing-page {
    width: min(100%, 100rem);
    margin: 0 auto;
    padding: clamp(1.3rem, 4vw, 3.5rem);
  }

  .desk-message {
    min-height: 2.8rem;
    display: flex;
    align-items: center;
    gap: 0.55rem;
    border-block: 1px solid var(--color-rule);
    margin: 0;
    padding: 0.7rem 1rem;
    background: color-mix(in srgb, var(--color-success) 7%, var(--color-paper));
    color: var(--color-ink-soft);
    font-size: 0.8rem;
  }

  .desk-message--error {
    background: color-mix(in srgb, var(--color-danger) 7%, var(--color-paper));
    color: var(--color-danger);
  }

  .loading-state {
    min-height: 21rem;
    display: grid;
    align-content: center;
    gap: 0.7rem;
    border-block: 1px solid var(--color-rule);
  }

  .loading-state span {
    height: 3.7rem;
    background: linear-gradient(
      90deg,
      var(--color-surface-muted),
      var(--color-surface),
      var(--color-surface-muted)
    );
    background-size: 220% 100%;
    animation: loading 1.4s ease infinite;
  }

  @keyframes loading {
    to { background-position: -220% 0; }
  }

  .summary-strip {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    border: 1px solid var(--color-rule-strong);
    margin-top: 1.4rem;
    background: var(--color-paper);
  }

  .summary-strip article {
    min-height: 7.2rem;
    display: grid;
    align-content: center;
    border-right: 1px solid var(--color-rule);
    padding: 1rem 1.2rem;
  }

  .summary-strip article:last-child {
    border-right: 0;
  }

  .summary-strip p,
  .kicker {
    margin: 0;
    color: var(--color-clay);
    font-size: 0.61rem;
    font-weight: 800;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  .summary-strip strong {
    margin-block: 0.15rem;
    font-family: var(--font-editorial);
    font-size: 2rem;
    font-weight: 500;
  }

  .summary-strip .summary-word {
    font-size: 1.55rem;
  }

  .summary-strip span {
    color: var(--color-ink-muted);
    font-size: 0.72rem;
  }

  .sharing-layout {
    display: grid;
    grid-template-columns: minmax(17rem, 0.32fr) minmax(0, 1fr);
    border: 1px solid var(--color-rule-strong);
    border-top: 0;
  }

  .account-index {
    border-right: 1px solid var(--color-rule-strong);
    background: color-mix(in srgb, var(--color-surface-muted) 55%, var(--color-paper));
  }

  .section-heading,
  .desk-heading {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 1.2rem;
  }

  .section-heading h2,
  .desk-heading h2,
  .dialog-section-heading h3 {
    margin: 0.2rem 0 0;
    font-family: var(--font-editorial);
    font-size: 1.25rem;
    font-weight: 500;
  }

  .section-heading > span {
    color: var(--color-ink-muted);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.72rem;
  }

  .provider-list article {
    border-bottom: 1px solid var(--color-rule);
    padding: 1rem 1.2rem;
  }

  .provider-name {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin-bottom: 0.7rem;
  }

  .provider-mark,
  .recipient-mark {
    width: 2rem;
    height: 2rem;
    flex: 0 0 auto;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-rule-strong);
    background: var(--color-surface);
    color: var(--color-clay);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.62rem;
    font-weight: 800;
  }

  .provider-name h3 {
    margin: 0;
    font-size: 0.85rem;
  }

  .provider-name p,
  .desk-heading p,
  .dialog-section-heading p {
    margin: 0.15rem 0 0;
    color: var(--color-ink-muted);
    font-size: 0.73rem;
    line-height: 1.45;
  }

  .provider-list ul {
    display: grid;
    gap: 0.35rem;
    margin: 0.75rem 0 0;
    padding: 0;
    list-style: none;
  }

  .provider-list li {
    display: flex;
    justify-content: space-between;
    gap: 0.6rem;
    color: var(--color-ink-soft);
    font-size: 0.73rem;
  }

  .provider-list small,
  .delegation-row small {
    display: block;
    color: var(--color-ink-muted);
    font-size: 0.67rem;
  }

  .aside-empty,
  .security-note {
    display: flex;
    align-items: flex-start;
    gap: 0.7rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 1rem 1.2rem;
    color: var(--color-ink-muted);
  }

  .aside-empty p,
  .security-note p {
    margin: 0;
    font-size: 0.75rem;
    line-height: 1.55;
  }

  .security-note {
    border-top: 1px solid var(--color-rule);
    border-bottom: 0;
    background: color-mix(in srgb, var(--color-moss) 6%, transparent);
  }

  .delegation-desk {
    min-width: 0;
    background: var(--color-surface);
  }

  .desk-heading {
    align-items: center;
  }

  .desk-heading > div:first-child {
    max-width: 32rem;
  }

  .desk-heading :global(label) {
    width: min(19rem, 100%);
  }

  .delegation-table__head,
  .delegation-row {
    display: grid;
    grid-template-columns: minmax(13rem, 1.2fr) minmax(11rem, 1fr) minmax(9rem, 0.75fr) auto;
    align-items: center;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.8rem 1rem;
  }

  .delegation-table__head {
    background: var(--color-paper);
    color: var(--color-ink-muted);
    font-size: 0.61rem;
    font-weight: 800;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .delegation-row {
    min-height: 5.2rem;
  }

  .delegation-row strong {
    display: block;
    overflow: hidden;
    font-size: 0.78rem;
    text-overflow: ellipsis;
  }

  .recipient-cell {
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 0.7rem;
  }

  .recipient-cell > div {
    min-width: 0;
  }

  .row-actions {
    justify-self: end;
  }

  .share-dialog {
    display: grid;
    gap: 1.5rem;
  }

  .share-dialog section {
    border: 1px solid var(--color-rule);
  }

  .dialog-section-heading {
    display: flex;
    align-items: flex-start;
    gap: 0.85rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.85rem 1rem;
    background: var(--color-paper);
  }

  .dialog-section-heading > span {
    color: var(--color-clay);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.65rem;
    font-weight: 800;
  }

  .dialog-section-heading h3 {
    margin-top: 0;
    font-size: 1.05rem;
  }

  .choice-list,
  .recipient-list {
    display: grid;
  }

  fieldset {
    display: grid;
    border: 0;
    border-bottom: 1px solid var(--color-rule);
    margin: 0;
    padding: 0;
  }

  fieldset:last-child {
    border-bottom: 0;
  }

  legend {
    width: 100%;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.55rem 1rem;
    color: var(--color-ink-muted);
    font-size: 0.65rem;
    font-weight: 750;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .choice-list label,
  .recipient-list label {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.75rem 1rem;
    cursor: pointer;
  }

  .choice-list label:last-child,
  .recipient-list label:last-child {
    border-bottom: 0;
  }

  .choice-list label:hover,
  .recipient-list label:hover,
  .choice-list label.selected,
  .recipient-list label.selected {
    background: color-mix(in srgb, var(--color-clay) 7%, var(--color-surface));
  }

  .choice-list input,
  .recipient-list input {
    width: 1rem;
    height: 1rem;
    accent-color: var(--color-clay);
  }

  .choice-list label > span:nth-of-type(1) {
    min-width: 0;
    flex: 1;
  }

  .choice-list strong,
  .recipient-list strong {
    display: block;
    font-size: 0.78rem;
  }

  .choice-list small {
    display: block;
    margin-top: 0.15rem;
    color: var(--color-ink-muted);
    font-size: 0.68rem;
  }

  .review-box > p {
    margin: 0;
    padding: 0.9rem 1rem;
    color: var(--color-ink-muted);
    font-size: 0.76rem;
    line-height: 1.55;
  }

  .remove-review {
    display: grid;
    gap: 0.3rem;
  }

  .remove-review strong {
    font-family: var(--font-editorial);
    font-size: 1.15rem;
    font-weight: 500;
  }

  .remove-review span,
  .remove-review p:last-child {
    color: var(--color-ink-muted);
    font-size: 0.75rem;
  }

  .remove-review p:last-child {
    border-top: 1px solid var(--color-rule);
    margin: 0.8rem 0 0;
    padding-top: 0.8rem;
    line-height: 1.5;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  @media (max-width: 72rem) {
    .summary-strip {
      grid-template-columns: repeat(2, 1fr);
    }

    .summary-strip article:nth-child(2) {
      border-right: 0;
    }

    .summary-strip article:nth-child(-n + 2) {
      border-bottom: 1px solid var(--color-rule);
    }

    .sharing-layout {
      grid-template-columns: 1fr;
    }

    .account-index {
      border-right: 0;
      border-bottom: 1px solid var(--color-rule-strong);
    }

    .provider-list {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
    }

    .provider-list article:nth-child(odd) {
      border-right: 1px solid var(--color-rule);
    }
  }

  @media (max-width: 50rem) {
    .sharing-page {
      padding: 1rem;
    }

    .desk-heading {
      align-items: stretch;
      flex-direction: column;
    }

    .delegation-table__head {
      display: none;
    }

    .delegation-row {
      grid-template-columns: 1fr 1fr;
    }

    .row-actions {
      justify-self: start;
    }
  }

  @media (max-width: 36rem) {
    .summary-strip,
    .provider-list,
    .delegation-row {
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

    .provider-list article:nth-child(odd) {
      border-right: 0;
    }
  }
</style>
