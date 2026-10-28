<script lang="ts">
  import { untrack } from "svelte";
  import type {
    FrontendAddonConnection,
    FrontendAddonManagerItem,
    FrontendAddonSettingField,
    FrontendAddonSettingOption,
    FrontendAddonSettingsDashboard,
    FrontendAddonSettingsMutation,
    FrontendApiClient,
    FrontendModelRecord,
  } from "@carlina/sdk";
  import { Badge, Button, Dialog, Icon } from "@carlina/ui";

  import {
    addonSettingOptions,
    buildAddonSettingsDraft,
    changedAddonSettings,
    formatAddonConnectionDate,
    groupAddonSettings,
    type AddonSettingsDraft,
  } from "../../lib/addon-settings.ts";
  import { announceRestartRequired } from "../../lib/restart-required.ts";

  interface Props {
    api: FrontendApiClient;
    addon: FrontendAddonManagerItem | null;
    open: boolean;
    onclose: () => void;
  }

  let { api, addon, open, onclose }: Props = $props();

  let dashboard = $state<FrontendAddonSettingsDashboard | null>(null);
  let draft = $state<AddonSettingsDraft>({});
  let modelOptions = $state<FrontendAddonSettingOption[]>([]);
  let connectionDraft = $state<Record<string, string>>({});
  let loadedId = $state("");
  let loading = $state(false);
  let saving = $state(false);
  let connectionPending = $state(false);
  let pendingDeleteId = $state<string | null>(null);
  let error = $state("");
  let notice = $state("");

  const groups = $derived(groupAddonSettings(dashboard?.fields ?? []));
  const changes = $derived(
    changedAddonSettings(dashboard?.fields ?? [], draft),
  );
  const changedCount = $derived(Object.keys(changes).length);

  $effect(() => {
    const addonId = addon?.id ?? "";
    if (open && addonId && addonId !== loadedId) {
      untrack(() => void load(addonId));
    }
    if (!open) {
      pendingDeleteId = null;
      error = "";
      notice = "";
    }
  });

  async function load(addonId: string): Promise<void> {
    loading = true;
    error = "";
    notice = "";
    try {
      const payload = await api.request<FrontendAddonSettingsDashboard>(
        `/api/frontend/settings/addons/${encodeURIComponent(addonId)}`,
      );
      dashboard = payload;
      draft = buildAddonSettingsDraft(payload.fields);
      connectionDraft = Object.fromEntries(
        (payload.connections?.create_fields ?? []).map((field) => [
          field.id,
          "",
        ]),
      );
      loadedId = addonId;
      if (payload.fields.some((field) => field.options_source === "llm_models")) {
        await loadModelOptions();
      } else {
        modelOptions = [];
      }
    } catch (reason) {
      dashboard = null;
      error =
        reason instanceof Error
          ? reason.message
          : "Addon settings could not be loaded.";
    } finally {
      loading = false;
    }
  }

  async function loadModelOptions(): Promise<void> {
    try {
      const models = await api.request<FrontendModelRecord[]>("/api/llm/models");
      modelOptions = models
        .filter((model) => model.active && model.type === "chat")
        .map((model) => ({
          value: model.id,
          label: `${model.name} · ${model.provider_name}`,
        }));
    } catch {
      modelOptions = [];
    }
  }

  function updateValue(fieldId: string, value: unknown): void {
    draft = { ...draft, [fieldId]: value };
  }

  function mappingRows(field: FrontendAddonSettingField): Array<{
    key: string;
    value: string;
  }> {
    const value = draft[field.id];
    return Array.isArray(value)
      ? value.map((row) => ({
          key: String((row as Record<string, unknown>).key ?? ""),
          value: String((row as Record<string, unknown>).value ?? ""),
        }))
      : [];
  }

  function updateMapping(
    field: FrontendAddonSettingField,
    index: number,
    part: "key" | "value",
    value: string,
  ): void {
    const rows = mappingRows(field);
    rows[index] = { ...rows[index], [part]: value };
    updateValue(field.id, rows);
  }

  function addMappingRow(field: FrontendAddonSettingField): void {
    updateValue(field.id, [...mappingRows(field), { key: "", value: "" }]);
  }

  function removeMappingRow(
    field: FrontendAddonSettingField,
    index: number,
  ): void {
    updateValue(
      field.id,
      mappingRows(field).filter((_, rowIndex) => rowIndex !== index),
    );
  }

  async function save(): Promise<void> {
    if (!dashboard || !changedCount) return;
    saving = true;
    error = "";
    notice = "";
    try {
      const response = await api.request<FrontendAddonSettingsMutation>(
        dashboard.actions.update_url,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ values: changes }),
        },
      );
      dashboard = response.settings;
      draft = buildAddonSettingsDraft(response.settings.fields);
      notice = response.message;
      if (response.restart_required) announceRestartRequired();
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "Addon settings could not be saved.";
    } finally {
      saving = false;
    }
  }

  async function createConnection(): Promise<void> {
    const connections = dashboard?.connections;
    if (!connections?.create_url) return;
    connectionPending = true;
    error = "";
    notice = "";
    try {
      await api.request<{ tested: boolean }>(connections.create_url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(connectionDraft),
      });
      await load(dashboard!.addon.id);
      notice = connections.tested_on_create
        ? "Connection tested and stored."
        : "Connection stored. Its health will be checked on first use.";
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "The connection could not be stored.";
    } finally {
      connectionPending = false;
    }
  }

  async function activateConnection(
    connection: FrontendAddonConnection,
  ): Promise<void> {
    if (!connection.activate_url) return;
    connectionPending = true;
    error = "";
    notice = "";
    try {
      await api.request(connection.activate_url, { method: "POST" });
      await load(dashboard!.addon.id);
      notice = `${connection.name} is now the active connection.`;
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "The active connection could not be changed.";
    } finally {
      connectionPending = false;
    }
  }

  async function deleteConnection(
    connection: FrontendAddonConnection,
  ): Promise<void> {
    if (!connection.delete_url) return;
    connectionPending = true;
    error = "";
    notice = "";
    try {
      await api.request(connection.delete_url, {
        method: connection.delete_method,
        headers: connection.delete_body
          ? { "Content-Type": "application/json" }
          : undefined,
        body: connection.delete_body
          ? JSON.stringify(connection.delete_body)
          : undefined,
      });
      pendingDeleteId = null;
      await load(dashboard!.addon.id);
      notice = `${connection.name} was disconnected.`;
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "The connection could not be removed.";
    } finally {
      connectionPending = false;
    }
  }
</script>

<Dialog
  {open}
  title={addon ? `${addon.name} settings` : "Addon settings"}
  description="Configuration and sealed accounts owned by this addon."
  {onclose}
  size="lg"
>
  {#if loading}
    <div class="settings-loading" aria-busy="true" aria-label="Loading addon settings">
      <span></span><span></span><span></span>
    </div>
  {:else if error && !dashboard}
    <div class="desk-error" role="alert">
      <Icon name="activity" size={19} />
      <p>{error}</p>
      {#if addon}
        <Button variant="outline" onclick={() => void load(addon!.id)}>
          Try again
        </Button>
      {/if}
    </div>
  {:else if dashboard}
    <div class="settings-desk">
      {#if error}
        <p class="desk-message desk-message--error" role="alert">{error}</p>
      {:else if notice}
        <p class="desk-message" role="status">
          <Icon name="check" size={14} />
          {notice}
        </p>
      {/if}

      {#if dashboard.connections}
        <section class="connection-desk">
          <header class="section-heading">
            <div>
              <p>Sealed accounts</p>
              <h3>Connections</h3>
            </div>
            <Badge tone={dashboard.connections.items.some((item) => item.active) ||
              dashboard.connections.delegated_from
              ? "success"
              : "neutral"}>
              {dashboard.connections.items.length} visible
            </Badge>
          </header>

          {#if dashboard.connections.delegated_from}
            <div class="delegated-note">
              <Icon name="shield" size={18} />
              <div>
                <strong>Shared access is active</strong>
                <p>
                  This addon uses an account delegated by
                  {dashboard.connections.delegated_from}. Secret material remains
                  with its owner.
                </p>
              </div>
            </div>
          {/if}
            {#if dashboard.connections.callback_url}
              <div class="callback-note">
                <span>OAuth callback</span>
                <code>{dashboard.connections.callback_url}</code>
              </div>
            {/if}

            {#if dashboard.connections.items.length}
              <div class="connection-list">
                {#each dashboard.connections.items as connection (connection.id)}
                  <article>
                    <div class="connection-reading">
                      <span class="connection-mark" aria-hidden="true">
                        {connection.name.slice(0, 1).toUpperCase()}
                      </span>
                      <div>
                        <strong>{connection.name}</strong>
                        <small>{formatAddonConnectionDate(connection.created_at)}</small>
                      </div>
                    </div>
                    <Badge tone={connection.active ? "success" : "neutral"}>
                      {connection.active ? "Active" : "Available"}
                    </Badge>
                    <div class="connection-actions">
                      {#if connection.can_manage && !connection.active && connection.activate_url}
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={connectionPending}
                          onclick={() => void activateConnection(connection)}
                        >
                          Make active
                        </Button>
                      {/if}
                      {#if connection.can_manage && pendingDeleteId === connection.id}
                        <Button
                          size="sm"
                          variant="quiet"
                          disabled={connectionPending}
                          onclick={() => (pendingDeleteId = null)}
                        >
                          Keep
                        </Button>
                        <Button
                          size="sm"
                          variant="danger"
                          disabled={connectionPending}
                          onclick={() => void deleteConnection(connection)}
                        >
                          Disconnect
                        </Button>
                      {:else if connection.can_manage}
                        <Button
                          size="sm"
                          variant="quiet"
                          disabled={connectionPending}
                          onclick={() => (pendingDeleteId = connection.id)}
                        >
                          Remove
                        </Button>
                      {/if}
                    </div>
                  </article>
                  {#if pendingDeleteId === connection.id}
                    <p class="disconnect-note">
                      Disconnecting removes the encrypted account from this
                      installation. It does not remove the external account.
                    </p>
                  {/if}
                {/each}
              </div>
            {:else}
              <p class="empty-note">
                No personal connection is stored for this addon.
              </p>
            {/if}

            {#if dashboard.connections.mode !== "credentials" &&
              dashboard.connections.connect_url}
              <a class="oauth-link" href={dashboard.connections.connect_url}>
                <Icon name="arrow-up-right" size={15} />
                {dashboard.connections.mode === "managed"
                  ? "Configure account"
                  : "Connect an account"}
              </a>
            {:else if dashboard.connections.create_url}
              <form
                class="connection-form"
                onsubmit={(event) => {
                  event.preventDefault();
                  void createConnection();
                }}
              >
                <div class="section-heading section-heading--small">
                  <div>
                    <p>New connection</p>
                    <h4>
                      {dashboard.connections.tested_on_create
                        ? "Test and seal credentials"
                        : "Seal credentials"}
                    </h4>
                  </div>
                </div>
                <div class="connection-fields">
                  {#each dashboard.connections.create_fields as field (field.id)}
                    <label>
                      <span>{field.name}</span>
                      <input
                        type={field.kind}
                        placeholder={field.placeholder ?? ""}
                        autocomplete={field.kind === "password" ? "new-password" : "off"}
                        value={connectionDraft[field.id] ?? ""}
                        required={field.required}
                        oninput={(event) => {
                          connectionDraft = {
                            ...connectionDraft,
                            [field.id]: event.currentTarget.value,
                          };
                        }}
                      />
                      {#if field.description}<small>{field.description}</small>{/if}
                    </label>
                  {/each}
                </div>
                <Button type="submit" disabled={connectionPending}>
                  {connectionPending
                    ? dashboard.connections.tested_on_create
                      ? "Testing…"
                      : "Saving…"
                    : dashboard.connections.tested_on_create
                      ? "Test & save"
                      : "Save connection"}
                </Button>
              </form>
            {/if}
        </section>
      {/if}

      {#if groups.length}
        <section class="schema-desk">
          <header class="section-heading">
            <div>
              <p>Schema settings</p>
              <h3>Operating preferences</h3>
            </div>
            <span class="field-count">{dashboard.fields.length} fields</span>
          </header>

          {#each groups as group (group.name)}
            <fieldset>
              <legend>{group.name}</legend>
              <div class="field-grid">
                {#each group.fields as field (field.id)}
                  <div
                    class="setting-field"
                    class:setting-field--wide={field.kind === "textarea" ||
                      field.kind === "mapping"}
                  >
                    <div class="field-label">
                      <label for={`addon-setting-${dashboard.addon.id}-${field.id}`}>
                        {field.name}
                      </label>
                      <span>
                        {field.scope_label}
                        {#if field.requires_restart} · restart{/if}
                      </span>
                    </div>

                    {#if field.kind === "checkbox"}
                      <label class="checkbox-field">
                        <input
                          id={`addon-setting-${dashboard.addon.id}-${field.id}`}
                          type="checkbox"
                          checked={Boolean(draft[field.id])}
                          disabled={!field.editable}
                          onchange={(event) =>
                            updateValue(field.id, event.currentTarget.checked)}
                        />
                        <span aria-hidden="true"></span>
                        <strong>
                          {draft[field.id] ? "Enabled" : "Disabled"}
                        </strong>
                      </label>
                    {:else if field.kind === "textarea"}
                      <textarea
                        id={`addon-setting-${dashboard.addon.id}-${field.id}`}
                        rows={field.rows}
                        placeholder={field.placeholder}
                        disabled={!field.editable}
                        value={String(draft[field.id] ?? "")}
                        oninput={(event) =>
                          updateValue(field.id, event.currentTarget.value)}
                      ></textarea>
                    {:else if field.kind === "select" ||
                      field.kind === "model_select"}
                      <select
                        id={`addon-setting-${dashboard.addon.id}-${field.id}`}
                        disabled={!field.editable}
                        value={String(draft[field.id] ?? "")}
                        onchange={(event) =>
                          updateValue(field.id, event.currentTarget.value)}
                      >
                        <option value="">Select…</option>
                        {#each addonSettingOptions(field, modelOptions) as option (option.value)}
                          <option value={option.value}>{option.label}</option>
                        {/each}
                      </select>
                    {:else if field.kind === "mapping" && field.mapping}
                      <div class="mapping-field">
                        {#each mappingRows(field) as row, index}
                          <div class="mapping-row">
                            <input
                              aria-label={field.mapping.key.name}
                              placeholder={field.mapping.key.placeholder ||
                                field.mapping.key.name}
                              value={row.key}
                              disabled={!field.editable}
                              oninput={(event) =>
                                updateMapping(
                                  field,
                                  index,
                                  "key",
                                  event.currentTarget.value,
                                )}
                            />
                            <input
                              aria-label={field.mapping.value.name}
                              placeholder={field.mapping.value.placeholder ||
                                field.mapping.value.name}
                              value={row.value}
                              disabled={!field.editable}
                              oninput={(event) =>
                                updateMapping(
                                  field,
                                  index,
                                  "value",
                                  event.currentTarget.value,
                                )}
                            />
                            <button
                              type="button"
                              aria-label={`Remove ${field.name} row ${index + 1}`}
                              disabled={!field.editable}
                              onclick={() => removeMappingRow(field, index)}
                            >
                              <Icon name="trash" size={14} />
                            </button>
                          </div>
                        {/each}
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          disabled={!field.editable}
                          onclick={() => addMappingRow(field)}
                        >
                          <Icon name="plus" size={13} />
                          {field.mapping.add_label}
                        </Button>
                      </div>
                    {:else}
                      <input
                        id={`addon-setting-${dashboard.addon.id}-${field.id}`}
                        type={field.kind === "password"
                          ? "password"
                          : field.kind === "number"
                            ? "number"
                            : field.kind === "url"
                              ? "url"
                              : "text"}
                        min={field.minimum ?? undefined}
                        max={field.maximum ?? undefined}
                        step={field.step ?? undefined}
                        placeholder={field.sensitive && field.configured
                          ? "Stored · enter a replacement only"
                          : field.placeholder}
                        autocomplete={field.sensitive ? "new-password" : "off"}
                        disabled={!field.editable}
                        value={String(draft[field.id] ?? "")}
                        oninput={(event) =>
                          updateValue(field.id, event.currentTarget.value)}
                      />
                    {/if}

                    {#if field.description}
                      <p>{field.description}</p>
                    {/if}
                    {#if field.sensitive}
                      <small class="sealed-note">
                        <Icon name="shield" size={12} />
                        {field.configured
                          ? "A sealed value is stored; it is never returned."
                          : "Write-only secret; it will not be shown after save."}
                      </small>
                    {/if}
                  </div>
                {/each}
              </div>
            </fieldset>
          {/each}
        </section>
      {:else if !dashboard.connections}
        <p class="empty-note">This addon has no configurable fields.</p>
      {/if}
    </div>
  {/if}

  {#snippet footer()}
    <Button variant="quiet" disabled={saving} onclick={onclose}>Close</Button>
    {#if dashboard?.fields.length}
      <Button
        disabled={saving || changedCount === 0}
        onclick={() => void save()}
      >
        {saving
          ? "Saving…"
          : changedCount
            ? `Save ${changedCount} ${changedCount === 1 ? "change" : "changes"}`
            : "No changes"}
      </Button>
    {/if}
  {/snippet}
</Dialog>

<style>
  .settings-desk {
    display: grid;
    gap: 1.4rem;
  }

  .settings-loading {
    display: grid;
    gap: 0.65rem;
  }

  .settings-loading span {
    height: 6rem;
    background: linear-gradient(
      90deg,
      var(--color-surface-muted),
      var(--color-surface),
      var(--color-surface-muted)
    );
    background-size: 200% 100%;
    animation: loading 1.5s linear infinite;
  }

  .desk-error,
  .delegated-note {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.75rem;
    border-block: 1px solid var(--color-rule);
    padding: 1rem 0;
  }

  .desk-error {
    color: var(--color-danger);
  }

  .desk-error p,
  .delegated-note p,
  .delegated-note strong {
    margin: 0;
  }

  .delegated-note {
    grid-template-columns: auto minmax(0, 1fr);
    color: var(--color-lichen);
  }

  .delegated-note p {
    margin-top: 0.2rem;
    color: var(--color-ink-muted);
    font-size: 0.7rem;
    line-height: 1.5;
  }

  .desk-message {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    border-left: 2px solid var(--color-lichen);
    margin: 0;
    padding: 0.7rem 0.8rem;
    background: var(--color-lichen-muted);
    color: var(--color-lichen);
    font-size: 0.72rem;
  }

  .desk-message--error {
    border-color: var(--color-danger);
    background: var(--color-danger-muted);
    color: var(--color-danger);
  }

  .connection-desk,
  .schema-desk {
    display: grid;
    gap: 1rem;
  }

  .schema-desk {
    border-top: 1px solid var(--color-rule-strong);
    padding-top: 1.2rem;
  }

  .section-heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }

  .section-heading p,
  .section-heading h3,
  .section-heading h4 {
    margin: 0;
  }

  .section-heading p {
    margin-bottom: 0.2rem;
    color: var(--color-clay);
    font-size: 0.56rem;
    font-weight: 800;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  .section-heading h3,
  .section-heading h4 {
    font-family: var(--font-editorial);
    font-size: 1.25rem;
    font-weight: 540;
  }

  .section-heading h4 {
    font-size: 1rem;
  }

  .field-count {
    color: var(--color-ink-muted);
    font-size: 0.62rem;
  }

  .callback-note {
    display: grid;
    gap: 0.35rem;
    border-left: 2px solid var(--color-clay);
    padding: 0.7rem 0.8rem;
    background: var(--color-clay-muted);
  }

  .callback-note span {
    color: var(--color-clay);
    font-size: 0.56rem;
    font-weight: 800;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .callback-note code {
    overflow-wrap: anywhere;
    color: var(--color-ink-soft);
    font-size: 0.68rem;
  }

  .connection-list {
    display: grid;
    border: 1px solid var(--color-rule);
  }

  .connection-list article {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto auto;
    align-items: center;
    gap: 0.75rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.75rem;
    background: var(--color-paper);
  }

  .connection-list article:last-of-type {
    border-bottom: 0;
  }

  .connection-reading {
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 0.65rem;
  }

  .connection-mark {
    width: 2rem;
    height: 2rem;
    display: grid;
    flex: 0 0 auto;
    place-items: center;
    border: 1px solid var(--color-rule-strong);
    color: var(--color-clay);
    font-family: var(--font-editorial);
  }

  .connection-reading strong,
  .connection-reading small {
    display: block;
  }

  .connection-reading strong {
    overflow: hidden;
    text-overflow: ellipsis;
    font-family: var(--font-editorial);
    font-size: 0.85rem;
    font-weight: 560;
    white-space: nowrap;
  }

  .connection-reading small {
    margin-top: 0.2rem;
    color: var(--color-ink-muted);
    font-size: 0.58rem;
  }

  .connection-actions {
    display: flex;
    gap: 0.35rem;
  }

  .disconnect-note {
    margin: 0;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.55rem 0.75rem;
    background: var(--color-danger-muted);
    color: var(--color-danger);
    font-size: 0.64rem;
  }

  .oauth-link {
    min-height: 2.6rem;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.45rem;
    border: 1px solid var(--color-umber);
    padding: 0.65rem 0.85rem;
    background: var(--color-umber);
    color: var(--color-surface);
    font-size: 0.72rem;
    font-weight: 700;
    text-decoration: none;
  }

  .connection-form {
    display: grid;
    gap: 0.9rem;
    border-top: 1px solid var(--color-rule);
    padding-top: 1rem;
  }

  .connection-fields,
  .field-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.8rem;
  }

  .connection-fields label,
  .setting-field {
    display: grid;
    align-content: start;
    gap: 0.42rem;
  }

  .connection-fields label > span,
  .field-label label {
    color: var(--color-ink-soft);
    font-size: 0.65rem;
    font-weight: 720;
  }

  .connection-fields small {
    color: var(--color-ink-muted);
    font-size: 0.6rem;
    line-height: 1.45;
  }

  fieldset {
    border: 0;
    border-top: 1px solid var(--color-rule);
    margin: 0;
    padding: 1rem 0 0;
  }

  legend {
    padding: 0 0.6rem 0 0;
    color: var(--color-ink);
    font-family: var(--font-editorial);
    font-size: 0.96rem;
    font-weight: 550;
  }

  .setting-field {
    border: 1px solid var(--color-rule);
    padding: 0.75rem;
    background: var(--color-paper);
  }

  .setting-field--wide {
    grid-column: 1 / -1;
  }

  .field-label {
    display: flex;
    align-items: start;
    justify-content: space-between;
    gap: 0.6rem;
  }

  .field-label span {
    color: var(--color-ink-muted);
    font-size: 0.52rem;
    text-align: right;
  }

  input,
  select,
  textarea {
    width: 100%;
    border: 1px solid var(--color-rule);
    border-radius: 0;
    padding: 0.65rem 0.72rem;
    background: var(--color-surface);
    color: var(--color-ink);
    font: inherit;
    font-size: 0.7rem;
  }

  textarea {
    resize: vertical;
    line-height: 1.5;
  }

  input:focus,
  select:focus,
  textarea:focus {
    border-color: var(--color-focus);
    outline: 2px solid var(--color-focus);
    outline-offset: 2px;
  }

  input:disabled,
  select:disabled,
  textarea:disabled {
    cursor: not-allowed;
    opacity: 0.65;
  }

  .setting-field > p,
  .empty-note {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.61rem;
    line-height: 1.5;
  }

  .empty-note {
    border-block: 1px solid var(--color-rule);
    padding: 1rem 0;
  }

  .sealed-note {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    color: var(--color-lichen);
    font-size: 0.56rem;
  }

  .checkbox-field {
    min-height: 2.55rem;
    display: flex;
    align-items: center;
    gap: 0.55rem;
    border: 1px solid var(--color-rule);
    padding: 0.55rem 0.65rem;
    background: var(--color-surface);
    cursor: pointer;
  }

  .checkbox-field input {
    position: absolute;
    width: 1px;
    opacity: 0;
  }

  .checkbox-field > span {
    width: 0.92rem;
    height: 0.92rem;
    border: 1px solid var(--color-rule-strong);
    background: var(--color-paper);
  }

  .checkbox-field input:checked + span {
    border: 3px solid var(--color-surface);
    outline: 1px solid var(--color-lichen);
    background: var(--color-lichen);
  }

  .checkbox-field:has(input:focus-visible) {
    outline: 2px solid var(--color-focus);
    outline-offset: 2px;
  }

  .checkbox-field strong {
    font-size: 0.66rem;
    font-weight: 680;
  }

  .mapping-field {
    display: grid;
    gap: 0.55rem;
  }

  .mapping-row {
    display: grid;
    grid-template-columns: 0.55fr 1fr auto;
    gap: 0.4rem;
  }

  .mapping-row button {
    width: 2.35rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-rule);
    cursor: pointer;
    background: var(--color-surface);
    color: var(--color-danger);
  }

  @keyframes loading {
    to {
      background-position: -200% 0;
    }
  }

  @media (max-width: 42rem) {
    .connection-fields,
    .field-grid {
      grid-template-columns: 1fr;
    }

    .connection-list article {
      grid-template-columns: minmax(0, 1fr) auto;
    }

    .connection-actions {
      grid-column: 1 / -1;
    }

    .mapping-row {
      grid-template-columns: 1fr auto;
    }

    .mapping-row input:nth-child(2) {
      grid-column: 1;
    }

    .mapping-row button {
      grid-column: 2;
      grid-row: 1 / 3;
    }
  }
</style>
