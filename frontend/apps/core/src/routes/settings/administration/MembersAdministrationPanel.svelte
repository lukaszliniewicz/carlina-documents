<script lang="ts">
  import { untrack } from "svelte";
  import type {
    FrontendAdministrationMutationResponse,
    FrontendApiClient,
    FrontendMemberDirectory,
    FrontendMemberRecord,
  } from "@carlina/sdk";
  import { Badge, Button, Dialog, Icon } from "@carlina/ui";

  import { filterMembers } from "../../../lib/administration.ts";

  interface Props {
    api: FrontendApiClient;
    value: FrontendMemberDirectory;
    onrefresh: () => Promise<void>;
  }

  let { api, value, onrefresh }: Props = $props();

  let directory = $state(untrack(() => value));
  let query = $state("");
  let editorOpen = $state(false);
  let editing = $state<FrontendMemberRecord | null>(null);
  let editorEmail = $state("");
  let editorPassword = $state("");
  let editorAdmin = $state(false);
  let deleteTarget = $state<FrontendMemberRecord | null>(null);
  let deleteConfirmation = $state("");
  let working = $state(false);
  let error = $state("");
  let notice = $state("");

  const filtered = $derived(
    filterMembers(directory.members, query),
  );
  const editorValid = $derived(
    editorEmail.trim().includes("@") &&
      (editing !== null ||
        (editorPassword.length >=
          directory.limits.password_characters.minimum &&
          editorPassword.length <=
            directory.limits.password_characters.maximum)),
  );

  function openCreate(): void {
    editing = null;
    editorEmail = "";
    editorPassword = "";
    editorAdmin = false;
    editorOpen = true;
  }

  function openEdit(member: FrontendMemberRecord): void {
    editing = member;
    editorEmail = member.email;
    editorPassword = "";
    editorAdmin = member.is_admin;
    editorOpen = true;
  }

  function openDelete(member: FrontendMemberRecord): void {
    deleteTarget = member;
    deleteConfirmation = "";
  }

  async function saveMember(): Promise<void> {
    if (!editorValid) return;
    working = true;
    error = "";
    notice = "";
    const endpoint = editing
      ? `${directory.actions.collection_url}/${editing.id}`
      : directory.actions.collection_url;
    try {
      const response =
        await api.request<FrontendAdministrationMutationResponse>(
          endpoint,
          {
            method: editing ? "PUT" : "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(
              editing
                ? {
                    email: editorEmail,
                    is_admin: editorAdmin,
                  }
                : {
                    email: editorEmail,
                    password: editorPassword,
                    is_admin: editorAdmin,
                  },
            ),
          },
        );
      if (response.directory) directory = response.directory;
      editorOpen = false;
      notice = response.message || "Member directory updated.";
      await onrefresh();
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "The member directory was not updated.";
    } finally {
      working = false;
    }
  }

  async function deleteMember(): Promise<void> {
    if (
      !deleteTarget ||
      deleteConfirmation.trim() !== deleteTarget.email
    ) {
      return;
    }
    working = true;
    error = "";
    notice = "";
    try {
      const response =
        await api.request<FrontendAdministrationMutationResponse>(
          `${directory.actions.collection_url}/${deleteTarget.id}`,
          {
            method: "DELETE",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              confirmation: deleteConfirmation,
            }),
          },
        );
      if (response.directory) directory = response.directory;
      notice = response.message || "Member removed.";
      deleteTarget = null;
      deleteConfirmation = "";
      await onrefresh();
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "The member was not removed.";
    } finally {
      working = false;
    }
  }
</script>

<section class="members-panel" aria-labelledby="members-heading">
  <header class="section-intro">
    <div>
      <p>Account directory</p>
      <h2 id="members-heading">Members and stewards</h2>
    </div>
    <span>
      Add accounts, maintain email addresses and administrative roles, and
      remove access through exact identity review.
    </span>
  </header>

  {#if error}
    <p class="panel-message panel-message--error" role="alert">{error}</p>
  {:else if notice}
    <p class="panel-message" role="status">
      <Icon name="check" size={14} />
      {notice}
    </p>
  {/if}

  <section class="member-reading" aria-label="Member summary">
    <article>
      <p>Accounts</p>
      <strong>{directory.summary.total}</strong>
      <span>registered people</span>
    </article>
    <article>
      <p>Administrators</p>
      <strong>{directory.summary.administrators}</strong>
      <span>platform stewards</span>
    </article>
    <article>
      <p>Members</p>
      <strong>{directory.summary.members}</strong>
      <span>standard access</span>
    </article>
  </section>

  {#if directory.summary.truncated}
    <p class="directory-warning" role="status">
      This desk shows the first {directory.limits.members} accounts. Refine the
      directory through a maintenance workflow before adding more.
    </p>
  {/if}

  <div class="directory-toolbar">
    <label for="member-search">
      <Icon name="search" size={15} />
      <input
        id="member-search"
        type="search"
        bind:value={query}
        placeholder="Search email or role"
      />
      {#if query}
        <button
          type="button"
          aria-label="Clear member search"
          onclick={() => (query = "")}
        >
          <Icon name="close" size={12} />
        </button>
      {/if}
    </label>
    <span>{filtered.length} visible</span>
    <Button onclick={openCreate}>
      <Icon name="plus" size={14} />
      Add member
    </Button>
  </div>

  {#if filtered.length}
    <div class="member-list">
      {#each filtered as member (member.id)}
        <article>
          <span class="member-mark" aria-hidden="true">
            {member.email.slice(0, 1).toUpperCase()}
          </span>
          <div class="member-copy">
            <p>{member.is_admin ? "Platform steward" : "Member account"}</p>
            <h3>{member.email}</h3>
            <div>
              <Badge tone={member.is_admin ? "warning" : "neutral"}>
                {member.is_admin ? "Administrator" : "Member"}
              </Badge>
              {#if member.is_current}
                <Badge tone="success">You</Badge>
              {/if}
            </div>
          </div>
          <div class="member-actions">
            <button
              class="member-action"
              type="button"
              aria-label={`Edit ${member.email}`}
              title="Edit member"
              onclick={() => openEdit(member)}
            >
              <Icon name="edit" size={13} />
              <span>Edit</span>
            </button>
            {#if member.can_delete}
              <button
                class="member-action member-action--remove"
                type="button"
                aria-label={`Remove ${member.email}`}
                title="Remove member"
                onclick={() => openDelete(member)}
              >
                <Icon name="trash" size={13} />
                <span>Remove</span>
              </button>
            {/if}
          </div>
        </article>
      {/each}
    </div>
  {:else}
    <div class="empty-directory">
      <Icon name="user" size={24} />
      <h3>No matching members</h3>
      <p>Try a shorter email fragment or role.</p>
    </div>
  {/if}

  <aside class="password-boundary">
    <Icon name="shield" size={18} />
    <div>
      <strong>Password resets are deliberately not exposed here.</strong>
      <p>
        Existing accounts may own password-derived encrypted data. A safe
        password-change workflow must unlock and re-encrypt that material with
        the member present; a blind administrative reset could make it
        unreadable.
      </p>
    </div>
  </aside>
</section>

<Dialog
  open={editorOpen}
  title={editing ? "Edit member account" : "Add a member"}
  description={
    editing
      ? "Update identity and role without exposing password-derived data."
      : "Create a named account with an initial strong password."
  }
  onclose={() => {
    if (!working) editorOpen = false;
  }}
>
  <form
    class="member-form"
    onsubmit={(event) => {
      event.preventDefault();
      void saveMember();
    }}
  >
    <label for="member-email">
      Email address
      <input
        id="member-email"
        type="email"
        bind:value={editorEmail}
        maxlength={directory.limits.email_characters}
        autocomplete="email"
        required
      />
    </label>
    {#if !editing}
      <label for="member-password">
        Initial password
        <input
          id="member-password"
          type="password"
          bind:value={editorPassword}
          minlength={directory.limits.password_characters.minimum}
          maxlength={directory.limits.password_characters.maximum}
          autocomplete="new-password"
          required
        />
        <small>
          {directory.limits.password_characters.minimum}–
          {directory.limits.password_characters.maximum} characters.
        </small>
      </label>
    {/if}
    <label class="role-toggle" for="member-administrator">
      <input
        id="member-administrator"
        type="checkbox"
        bind:checked={editorAdmin}
        disabled={Boolean(
          editing?.is_admin && !editing.can_change_role,
        )}
      />
      <span aria-hidden="true"></span>
      <span>
        <strong>Administrator</strong>
        <small>
          Can manage providers, members, retrieval, backups, and system
          operations.
        </small>
      </span>
    </label>
  </form>
  {#snippet footer()}
    <Button
      variant="quiet"
      disabled={working}
      onclick={() => (editorOpen = false)}
    >
      Cancel
    </Button>
    <Button
      disabled={!editorValid || working}
      onclick={() => void saveMember()}
    >
      {working
        ? "Saving…"
        : editing
          ? "Save member"
          : "Create member"}
    </Button>
  {/snippet}
</Dialog>

<Dialog
  open={Boolean(deleteTarget)}
  title="Remove this member and their data?"
  description="This account and records owned through database relationships will be deleted. This cannot be undone from the member desk."
  onclose={() => {
    if (!working) deleteTarget = null;
  }}
>
  {#if deleteTarget}
    <div class="delete-reading">
      <p>Member to remove</p>
      <strong>{deleteTarget.email}</strong>
    </div>
    <label class="delete-confirmation" for="member-delete-confirmation">
      Type the exact email address to continue
      <input
        id="member-delete-confirmation"
        bind:value={deleteConfirmation}
        autocomplete="off"
        spellcheck="false"
      />
    </label>
  {/if}
  {#snippet footer()}
    <Button
      variant="quiet"
      disabled={working}
      onclick={() => (deleteTarget = null)}
    >
      Keep member
    </Button>
    <Button
      variant="danger"
      disabled={
        !deleteTarget ||
        working ||
        deleteConfirmation.trim() !== deleteTarget.email
      }
      onclick={() => void deleteMember()}
    >
      {working ? "Removing…" : "Remove member"}
    </Button>
  {/snippet}
</Dialog>

<style>
  .members-panel {
    border-inline: 1px solid var(--color-rule-strong);
    border-bottom: 1px solid var(--color-rule-strong);
    background: var(--color-paper);
  }

  .section-intro {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(16rem, 0.6fr);
    gap: 2rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 1.4rem;
  }

  .section-intro p,
  .section-intro h2,
  .section-intro > span {
    margin: 0;
  }

  .section-intro p,
  .member-copy p {
    color: var(--color-clay);
    font-size: 0.58rem;
    font-weight: 800;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  .section-intro h2 {
    margin-top: 0.25rem;
    font-family: var(--font-editorial);
    font-size: clamp(1.4rem, 2.7vw, 2rem);
    font-weight: 520;
  }

  .section-intro > span {
    align-self: end;
    color: var(--color-ink-muted);
    font-size: 0.73rem;
    line-height: 1.6;
  }

  .panel-message,
  .directory-warning {
    margin: 0;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.8rem 1.4rem;
    font-size: 0.7rem;
  }

  .panel-message {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    color: var(--color-lichen);
  }

  .panel-message--error {
    color: var(--color-danger);
  }

  .directory-warning {
    background: var(--color-clay-muted);
    color: var(--color-ink-soft);
  }

  .member-reading {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    border-bottom: 1px solid var(--color-rule-strong);
    background: var(--color-surface);
  }

  .member-reading article {
    display: grid;
    gap: 0.2rem;
    border-right: 1px solid var(--color-rule);
    padding: 0.9rem 1rem;
  }

  .member-reading article:last-child {
    border-right: 0;
  }

  .member-reading p,
  .member-reading span {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.63rem;
  }

  .member-reading strong {
    font-family: var(--font-editorial);
    font-size: 1.4rem;
    font-weight: 540;
  }

  .directory-toolbar {
    display: grid;
    grid-template-columns: minmax(15rem, 1fr) auto auto;
    align-items: center;
    gap: 0.75rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.8rem 1rem;
  }

  .directory-toolbar label {
    min-width: 0;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    border: 1px solid var(--color-rule);
    padding-inline: 0.7rem;
    background: var(--color-surface);
  }

  .directory-toolbar input {
    min-width: 0;
    border: 0;
    padding: 0.65rem;
    background: transparent;
    color: var(--color-ink);
    font: inherit;
    font-size: 0.72rem;
    outline: 0;
  }

  .directory-toolbar label:focus-within {
    outline: 2px solid var(--color-focus);
    outline-offset: 2px;
  }

  .directory-toolbar button {
    border: 0;
    padding: 0.3rem;
    cursor: pointer;
    background: transparent;
    color: var(--color-ink-muted);
  }

  .directory-toolbar > span {
    color: var(--color-ink-muted);
    font-size: 0.63rem;
  }

  .member-list {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .member-list article {
    min-width: 0;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.8rem;
    border-right: 1px solid var(--color-rule);
    border-bottom: 1px solid var(--color-rule);
    padding: 1rem;
  }

  .member-list article:nth-child(2n) {
    border-right: 0;
  }

  .member-mark {
    width: 2.7rem;
    height: 2.7rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-clay);
    color: var(--color-clay);
    font-family: var(--font-editorial);
    font-size: 1.1rem;
  }

  .member-copy {
    min-width: 0;
  }

  .member-copy p,
  .member-copy h3 {
    overflow-wrap: anywhere;
    margin: 0;
  }

  .member-copy h3 {
    margin: 0.22rem 0 0.5rem;
    font-family: var(--font-editorial);
    font-size: 1.05rem;
    font-weight: 540;
  }

  .member-copy div {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
  }

  .member-actions {
    display: grid;
    gap: 0.35rem;
  }

  .member-action {
    min-width: 2.75rem;
    min-height: 2.75rem;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.35rem;
    border: 1px solid var(--color-rule-strong);
    padding: 0.45rem 0.55rem;
    background: var(--color-surface);
    color: var(--color-ink-soft);
    cursor: pointer;
    font-size: 0.65rem;
    font-weight: 700;
  }

  .member-action:hover {
    background: var(--color-surface-muted);
    color: var(--color-ink);
  }

  .member-action--remove {
    color: var(--color-danger);
  }

  .member-action span {
    display: none;
  }

  .empty-directory {
    display: grid;
    justify-items: start;
    gap: 0.4rem;
    min-height: 14rem;
    align-content: center;
    padding: 2rem;
  }

  .empty-directory h3,
  .empty-directory p {
    margin: 0;
  }

  .empty-directory h3 {
    font-family: var(--font-editorial);
    font-size: 1.25rem;
    font-weight: 520;
  }

  .empty-directory p {
    color: var(--color-ink-muted);
    font-size: 0.7rem;
  }

  .password-boundary {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 0.75rem;
    padding: 1rem 1.2rem;
    background: var(--color-lichen-muted);
  }

  .password-boundary strong,
  .password-boundary p {
    margin: 0;
    font-size: 0.68rem;
  }

  .password-boundary p {
    margin-top: 0.25rem;
    color: var(--color-ink-muted);
    line-height: 1.5;
  }

  .member-form {
    display: grid;
    gap: 1rem;
  }

  .member-form > label,
  .delete-confirmation {
    display: grid;
    gap: 0.4rem;
    color: var(--color-ink-soft);
    font-size: 0.68rem;
    font-weight: 720;
  }

  .member-form input,
  .delete-confirmation input {
    width: 100%;
    border: 1px solid var(--color-rule);
    padding: 0.72rem;
    background: var(--color-paper);
    color: var(--color-ink);
    font: inherit;
  }

  .member-form label small {
    color: var(--color-ink-muted);
    font-size: 0.62rem;
    font-weight: 450;
  }

  .role-toggle {
    grid-template-columns: auto auto minmax(0, 1fr);
    align-items: center;
    border-block: 1px solid var(--color-rule);
    padding-block: 0.8rem;
    cursor: pointer;
  }

  .role-toggle input {
    position: absolute;
    width: 1px;
    height: 1px;
    opacity: 0;
  }

  .role-toggle > span:nth-of-type(1) {
    width: 1rem;
    height: 1rem;
    border: 1px solid var(--color-rule-strong);
  }

  .role-toggle input:checked + span {
    border: 3px solid var(--color-paper);
    outline: 1px solid var(--color-clay);
    background: var(--color-clay);
  }

  .role-toggle > span:last-child {
    display: grid;
    gap: 0.2rem;
  }

  .delete-reading {
    display: grid;
    gap: 0.25rem;
    border: 1px solid var(--color-rule);
    margin-bottom: 1rem;
    padding: 0.8rem;
    background: var(--color-paper);
  }

  .delete-reading p,
  .delete-reading strong {
    overflow-wrap: anywhere;
    margin: 0;
  }

  .delete-reading p {
    color: var(--color-clay);
    font-size: 0.58rem;
    font-weight: 800;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .delete-reading strong {
    font-family: var(--font-editorial);
    font-weight: 540;
  }

  @media (max-width: 64rem) {
    .member-list {
      grid-template-columns: 1fr;
    }

    .member-list article,
    .member-list article:nth-child(2n) {
      border-right: 0;
    }
  }

  @media (max-width: 46rem) {
    .section-intro,
    .member-reading {
      grid-template-columns: 1fr;
    }

    .section-intro {
      gap: 0.75rem;
    }

    .member-reading article,
    .member-reading article:last-child {
      border-right: 0;
      border-bottom: 1px solid var(--color-rule);
    }

    .directory-toolbar {
      grid-template-columns: 1fr auto;
    }

    .directory-toolbar label {
      grid-column: 1 / -1;
    }

    .member-list article {
      grid-template-columns: auto minmax(0, 1fr);
    }

    .member-actions {
      grid-column: 1 / -1;
      grid-template-columns: repeat(2, auto);
      justify-content: end;
    }

  }
</style>
