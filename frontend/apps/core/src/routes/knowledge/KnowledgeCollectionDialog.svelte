<script lang="ts">
  import {
    createUiComponentService,
    type FrontendBootstrap,
    type FrontendKnowledgeCollection,
    type FrontendKnowledgeDriveItem,
    type FrontendKnowledgeDrivePickerResult,
    type FrontendKnowledgeOptions,
  } from "@carlina/sdk";
  import { Button, Dialog, Icon } from "@carlina/ui";

  export interface KnowledgeCollectionPayload {
    name: string;
    source_type: string;
    collection_mode: string;
    source_metadata: Readonly<Record<string, unknown>>;
  }

  interface Props {
    open: boolean;
    collection: FrontendKnowledgeCollection | null;
    options: FrontendKnowledgeOptions;
    bootstrap: FrontendBootstrap;
    onclose: () => void;
    onsave: (payload: KnowledgeCollectionPayload) => Promise<void>;
  }

  let {
    open,
    collection,
    options,
    bootstrap,
    onclose,
    onsave,
  }: Props = $props();

  let initializedKey = $state("");
  let sourceKind = $state("document_upload");
  let mode = $state("vector");
  let name = $state("");
  let engine = $state("auto");
  let emailAccountId = $state("");
  let emailScope = $state("threads");
  let emailSender = $state("");
  let emailSenderRole = $state("participant");
  let emailIncludeOthers = $state(true);
  let emailRecipient = $state("");
  let emailSubject = $state("");
  let emailBody = $state("");
  let emailAfter = $state("");
  let emailBefore = $state("");
  let driveAccountId = $state("");
  let driveItems = $state<FrontendKnowledgeDriveItem[]>([]);
  let pickerPending = $state(false);
  let saving = $state(false);
  let error = $state("");

  const editing = $derived(Boolean(collection));
  const title = $derived(
    collection ? `Edit ${collection.name}` : "Create a knowledge collection",
  );

  $effect(() => {
    const key = open ? String(collection?.id ?? "new") : "";
    if (!key) {
      initializedKey = "";
      return;
    }
    if (key === initializedKey) return;
    initializedKey = key;
    resetDraft(collection);
  });

  function metadataRecord(
    value: unknown,
  ): Readonly<Record<string, unknown>> {
    return typeof value === "object" &&
      value !== null &&
      !Array.isArray(value)
      ? (value as Readonly<Record<string, unknown>>)
      : {};
  }

  function stringValue(value: unknown): string {
    return typeof value === "string" || typeof value === "number"
      ? String(value)
      : "";
  }

  function resetDraft(value: FrontendKnowledgeCollection | null): void {
    const metadata = metadataRecord(value?.source_metadata);
    sourceKind = value?.source_type || "document_upload";
    mode = value?.collection_mode || "vector";
    name = value?.name || "";
    engine = stringValue(metadata.engine) || "auto";
    emailAccountId = stringValue(metadata.account_id);
    emailScope = stringValue(metadata.scope) || "threads";
    emailSender = stringValue(metadata.sender);
    emailSenderRole = stringValue(metadata.sender_role) || "participant";
    emailIncludeOthers = metadata.include_others !== false;
    emailRecipient = stringValue(metadata.recipient);
    emailSubject = stringValue(metadata.subject);
    emailBody = stringValue(metadata.body);
    emailAfter = stringValue(metadata.after_date);
    emailBefore = stringValue(metadata.before_date);
    driveAccountId = stringValue(metadata.account_id);
    driveItems = Array.isArray(metadata.items)
      ? metadata.items
          .map((item) => metadataRecord(item))
          .filter((item) => stringValue(item.id) && stringValue(item.name))
          .map((item) => ({
            id: stringValue(item.id),
            name: stringValue(item.name),
            isFolder: Boolean(item.isFolder),
            mimeType: stringValue(item.mimeType),
            ...(stringValue(item.webViewLink)
              ? { webViewLink: stringValue(item.webViewLink) }
              : {}),
          }))
      : [];
    error = "";
  }

  function chooseSource(nextSource: string, nextMode = mode): void {
    if (editing) return;
    sourceKind = nextSource;
    mode = nextMode;
    error = "";
  }

  async function chooseDriveItems(): Promise<void> {
    if (!driveAccountId) {
      error = "Choose a Google account before selecting Drive sources.";
      return;
    }
    pickerPending = true;
    error = "";
    try {
      const service = createUiComponentService(
        bootstrap,
        window.location.origin,
      );
      const result = await service.open<
        {
          title: string;
          mode: "any";
          multiple: boolean;
          allowFolders: boolean;
          accountId: number;
        },
        FrontendKnowledgeDrivePickerResult
      >("googledrive.picker", {
        title: "Select knowledge sources",
        mode: "any",
        multiple: true,
        allowFolders: true,
        accountId: Number(driveAccountId),
      });
      if (!result) return;
      driveItems =
        Array.isArray(result.items) && result.items.length
          ? result.items
          : [result];
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "The Google Drive picker could not be opened.";
    } finally {
      pickerPending = false;
    }
  }

  function accountLabel(
    accountId: string,
    accounts: FrontendKnowledgeOptions["email_accounts"],
  ): string {
    return (
      accounts.find((account) => String(account.id) === accountId)?.label ?? ""
    );
  }

  function compactRecord(
    entries: Array<[string, unknown]>,
  ): Readonly<Record<string, unknown>> {
    return Object.fromEntries(
      entries.filter(([, value]) => {
        if (typeof value === "string") return Boolean(value.trim());
        return value !== null && value !== undefined;
      }),
    );
  }

  async function submit(): Promise<void> {
    error = "";
    if (!name.trim()) {
      error = "Give the collection a concise, recognizable name.";
      return;
    }

    let sourceMetadata: Readonly<Record<string, unknown>>;
    if (sourceKind === "document_upload") {
      sourceMetadata = {
        type: "file_upload",
        engine: engine || "auto",
      };
    } else if (sourceKind === "email") {
      if (!emailAccountId) {
        error = "Choose the email account this collection should read.";
        return;
      }
      const entries: Array<[string, unknown]> = [
        ["account_id", Number(emailAccountId)],
        [
          "account_label",
          accountLabel(emailAccountId, options.email_accounts),
        ],
        ["scope", emailScope],
        ["sender", emailSender.trim()],
        ["recipient", emailRecipient.trim()],
        ["subject", emailSubject.trim()],
        ["body", emailBody.trim()],
        ["after_date", emailAfter],
        ["before_date", emailBefore],
      ];
      if (emailSender.trim() && emailScope === "threads") {
        entries.push(["sender_role", emailSenderRole]);
        entries.push(["include_others", emailIncludeOthers]);
      }
      sourceMetadata = compactRecord(entries);
    } else {
      if (!driveAccountId) {
        error = "Choose the Google account that owns these sources.";
        return;
      }
      if (!driveItems.length) {
        error = "Select at least one Drive file or folder.";
        return;
      }
      sourceMetadata = {
        account_id: Number(driveAccountId),
        account_label: accountLabel(
          driveAccountId,
          options.drive_accounts,
        ),
        items: driveItems.map((item) => ({
          id: item.id,
          name: item.name,
          isFolder: item.isFolder,
          mimeType: item.mimeType,
        })),
      };
    }

    saving = true;
    try {
      await onsave({
        name: name.trim(),
        source_type: sourceKind,
        collection_mode: mode,
        source_metadata: sourceMetadata,
      });
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "The collection could not be saved.";
    } finally {
      saving = false;
    }
  }
</script>

<Dialog
  {open}
  {title}
  description={editing
    ? "Update the source definition without exposing provider credentials."
    : "Choose where material comes from and how the assistant should use it."}
  onclose={() => {
    if (!saving && !pickerPending) onclose();
  }}
  size="lg"
>
  {#if !editing}
    <div class="source-tabs" aria-label="Collection source">
      <button
        type="button"
        class:active={sourceKind === "document_upload" && mode === "vector"}
        onclick={() => chooseSource("document_upload", "vector")}
      >
        <span>01</span>
        Documents · search
      </button>
      <button
        type="button"
        class:active={sourceKind === "document_upload" && mode === "regular"}
        onclick={() => chooseSource("document_upload", "regular")}
      >
        <span>02</span>
        Documents · direct
      </button>
      <button
        type="button"
        class:active={sourceKind === "email"}
        disabled={!options.providers.email}
        onclick={() => chooseSource("email", "vector")}
      >
        <span>03</span>
        Email
      </button>
      <button
        type="button"
        class:active={sourceKind === "googledrive"}
        disabled={!options.providers.googledrive}
        onclick={() => chooseSource("googledrive", "vector")}
      >
        <span>04</span>
        Drive
      </button>
    </div>
  {/if}

  <form
    class="collection-form"
    onsubmit={(event) => {
      event.preventDefault();
      void submit();
    }}
  >
    <label class="field field-wide">
      <span>Collection name</span>
      <input
        type="text"
        maxlength="100"
        autocomplete="off"
        placeholder="Research library"
        bind:value={name}
        required
      />
    </label>

    {#if sourceKind === "document_upload"}
      <div class="source-note field-wide">
        <Icon name={mode === "vector" ? "brain" : "file"} size={20} />
        <div>
          <strong>
            {mode === "vector"
              ? "Semantic document collection"
              : "Direct-analysis document collection"}
          </strong>
          <p>
            {mode === "vector"
              ? "Uploaded text is extracted, chunked, and indexed for evidence search."
              : "Uploaded documents remain available to the assistant without vector indexing."}
          </p>
        </div>
      </div>
      <label class="field field-wide">
        <span>Default extractor</span>
        <select bind:value={engine}>
          {#each options.engines as option (option.id)}
            <option value={option.id}>{option.name}</option>
          {/each}
        </select>
        <small>
          Per-upload controls remain available after the collection is created.
        </small>
      </label>
    {:else if sourceKind === "email"}
      <label class="field field-wide">
        <span>Email account</span>
        <select bind:value={emailAccountId} required>
          <option value="">Choose an account</option>
          {#each options.email_accounts as account (account.id)}
            <option value={String(account.id)}>
              {account.label}{account.active ? " · default" : ""}
            </option>
          {/each}
        </select>
      </label>
      <fieldset class="mode-field field-wide">
        <legend>Use mode</legend>
        <label>
          <input type="radio" value="vector" bind:group={mode} disabled={editing} />
          Semantic search
        </label>
        <label>
          <input type="radio" value="regular" bind:group={mode} disabled={editing} />
          Direct analysis
        </label>
      </fieldset>
      <label class="field">
        <span>Scope</span>
        <select bind:value={emailScope}>
          <option value="threads">Entire threads</option>
          <option value="messages">Individual messages</option>
        </select>
      </label>
      <label class="field">
        <span>From</span>
        <input
          type="email"
          placeholder="sender@example.org"
          bind:value={emailSender}
        />
      </label>
      {#if emailSender && emailScope === "threads"}
        <label class="field">
          <span>Sender’s role</span>
          <select bind:value={emailSenderRole}>
            <option value="participant">Participant</option>
            <option value="initiator">Thread initiator</option>
          </select>
        </label>
        <label class="check-field">
          <input type="checkbox" bind:checked={emailIncludeOthers} />
          <span>Include replies from other participants</span>
        </label>
      {/if}
      <label class="field">
        <span>To</span>
        <input
          type="email"
          placeholder="recipient@example.org"
          bind:value={emailRecipient}
        />
      </label>
      <label class="field">
        <span>Subject contains</span>
        <input type="text" placeholder="Project Alpha" bind:value={emailSubject} />
      </label>
      <label class="field field-wide">
        <span>Body contains</span>
        <input
          type="text"
          placeholder="Important words or phrases"
          bind:value={emailBody}
        />
      </label>
      <label class="field">
        <span>After</span>
        <input type="date" bind:value={emailAfter} />
      </label>
      <label class="field">
        <span>Before</span>
        <input type="date" bind:value={emailBefore} />
      </label>
    {:else}
      <label class="field field-wide">
        <span>Google account</span>
        <select bind:value={driveAccountId} required>
          <option value="">Choose an account</option>
          {#each options.drive_accounts as account (account.id)}
            <option value={String(account.id)}>
              {account.label}{account.active ? " · active" : ""}
            </option>
          {/each}
        </select>
      </label>
      <fieldset class="mode-field field-wide">
        <legend>Use mode</legend>
        <label>
          <input type="radio" value="vector" bind:group={mode} disabled={editing} />
          Semantic search
        </label>
        <label>
          <input type="radio" value="regular" bind:group={mode} disabled={editing} />
          Direct analysis
        </label>
      </fieldset>
      <div class="drive-picker field-wide">
        <div>
          <span>Selected sources</span>
          <small>Files and folders are resolved through the active Drive provider.</small>
        </div>
        <Button
          type="button"
          variant="outline"
          disabled={pickerPending || !driveAccountId}
          onclick={() => void chooseDriveItems()}
        >
          <Icon name="workspaces" size={16} />
          {pickerPending ? "Opening…" : "Choose sources"}
        </Button>
      </div>
      <div class="drive-items field-wide" aria-live="polite">
        {#if driveItems.length}
          {#each driveItems as item (item.id)}
            <div>
              <Icon name={item.isFolder ? "workspaces" : "file"} size={16} />
              <span>{item.name}</span>
              <small>{item.isFolder ? "Folder" : item.mimeType}</small>
            </div>
          {/each}
        {:else}
          <p>No Drive sources selected.</p>
        {/if}
      </div>
    {/if}

    {#if error}
      <p class="form-error field-wide" role="alert">{error}</p>
    {/if}

    <button type="submit" class="visually-hidden">Save collection</button>
  </form>

  {#snippet footer()}
    <Button variant="quiet" disabled={saving || pickerPending} onclick={onclose}>
      Cancel
    </Button>
    <Button disabled={saving || pickerPending} onclick={() => void submit()}>
      {saving ? "Saving…" : editing ? "Save changes" : "Create collection"}
    </Button>
  {/snippet}
</Dialog>

<style>
  .source-tabs {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    border: 1px solid var(--color-rule);
    border-right: 0;
    margin-bottom: 1.25rem;
  }

  .source-tabs button {
    min-height: 4.2rem;
    display: grid;
    align-content: center;
    gap: 0.3rem;
    border: 0;
    border-right: 1px solid var(--color-rule);
    padding: 0.65rem;
    background: var(--color-paper);
    cursor: pointer;
    color: var(--color-ink-soft);
    font-size: 0.72rem;
    font-weight: 650;
  }

  .source-tabs button span {
    color: var(--color-clay);
    font-size: 0.58rem;
    font-weight: 800;
    letter-spacing: 0.15em;
  }

  .source-tabs button.active {
    background: var(--color-clay-muted);
    color: var(--color-ink);
    box-shadow: inset 0 -2px 0 var(--color-clay);
  }

  .source-tabs button:disabled {
    cursor: not-allowed;
    opacity: 0.45;
  }

  .collection-form {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 1rem;
  }

  .field {
    display: grid;
    align-content: start;
    gap: 0.42rem;
  }

  .field-wide {
    grid-column: 1 / -1;
  }

  .field > span,
  .drive-picker span,
  .mode-field legend {
    color: var(--color-ink-soft);
    font-size: 0.69rem;
    font-weight: 750;
    letter-spacing: 0.07em;
    text-transform: uppercase;
  }

  input,
  select {
    width: 100%;
    min-height: 2.65rem;
    border: 1px solid var(--color-rule);
    border-radius: 0;
    padding: 0.65rem 0.72rem;
    background: var(--color-surface);
  }

  .field small,
  .drive-picker small {
    color: var(--color-ink-muted);
    font-size: 0.72rem;
    line-height: 1.45;
  }

  .source-note {
    display: flex;
    gap: 0.8rem;
    border: 1px solid var(--color-rule);
    padding: 0.9rem;
    background: var(--color-lichen-muted);
    color: var(--color-lichen);
  }

  .source-note strong {
    display: block;
    font-family: var(--font-editorial);
    font-size: 1rem;
    font-weight: 500;
  }

  .source-note p {
    margin: 0.2rem 0 0;
    font-size: 0.76rem;
    line-height: 1.5;
  }

  .mode-field {
    display: flex;
    flex-wrap: wrap;
    gap: 0.85rem 1.3rem;
    border: 1px solid var(--color-rule);
    padding: 0.8rem 0.9rem 0.9rem;
  }

  .mode-field legend {
    padding: 0 0.3rem;
  }

  .mode-field label,
  .check-field {
    display: flex;
    align-items: center;
    gap: 0.48rem;
    font-size: 0.78rem;
  }

  .mode-field input,
  .check-field input {
    width: 1rem;
    min-height: 1rem;
    accent-color: var(--color-clay);
  }

  .check-field {
    align-self: end;
    min-height: 2.65rem;
    border: 1px solid var(--color-rule);
    padding: 0.65rem 0.72rem;
    background: var(--color-surface);
  }

  .drive-picker {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding-bottom: 0.8rem;
  }

  .drive-picker > div {
    display: grid;
    gap: 0.25rem;
  }

  .drive-items {
    max-height: 12rem;
    overflow: auto;
    border: 1px solid var(--color-rule);
    background: var(--color-paper);
  }

  .drive-items > div {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.65rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.65rem 0.75rem;
  }

  .drive-items > div:last-child {
    border-bottom: 0;
  }

  .drive-items span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 0.8rem;
    font-weight: 650;
  }

  .drive-items small,
  .drive-items p {
    color: var(--color-ink-muted);
    font-size: 0.7rem;
  }

  .drive-items p {
    margin: 0;
    padding: 1rem;
  }

  .form-error {
    margin: 0;
    border-left: 3px solid var(--color-danger);
    padding: 0.7rem 0.8rem;
    background: var(--color-danger-muted);
    color: var(--color-danger);
    font-size: 0.78rem;
    line-height: 1.5;
  }

  .visually-hidden {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
  }

  @media (max-width: 44rem) {
    .source-tabs {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .collection-form {
      grid-template-columns: 1fr;
    }

    .field-wide {
      grid-column: auto;
    }

    .drive-picker {
      align-items: stretch;
      flex-direction: column;
    }
  }
</style>
