<script lang="ts">
  import type { FrontendChatTimelineItem } from "@carlina/sdk";
  import { Icon } from "@carlina/ui";

  import AgentEventCard from "./AgentEventCard.svelte";

  interface Props {
    item: FrontendChatTimelineItem;
    busy?: boolean;
    onedit?: (item: FrontendChatTimelineItem, text: string) => Promise<void>;
    ondelete?: (item: FrontendChatTimelineItem) => Promise<void>;
    onrerun?: (item: FrontendChatTimelineItem) => void;
    onfork?: (item: FrontendChatTimelineItem) => void;
    onlisten?: (item: FrontendChatTimelineItem) => Promise<void>;
    onannounce?: (message: string) => void;
  }

  let {
    item,
    busy = false,
    onedit,
    ondelete,
    onrerun,
    onfork,
    onlisten,
    onannounce,
  }: Props = $props();

  let editing = $state(false);
  let editText = $state("");
  let saving = $state(false);
  let developerOpen = $state(false);
  let listening = $state(false);
  let actionMenuOpen = $state(false);

  const timestamp = $derived.by(() => {
    if (!item.created_at) return "";
    const date = new Date(item.created_at);
    return Number.isNaN(date.valueOf())
      ? ""
      : new Intl.DateTimeFormat(undefined, {
          hour: "numeric",
          minute: "2-digit",
        }).format(date);
  });

  async function copyText() {
    if (item.role !== "user" && item.role !== "assistant") return;
    try {
      await navigator.clipboard.writeText(item.text);
      onannounce?.("Message copied.");
    } catch {
      onannounce?.("The message could not be copied.");
    } finally {
      actionMenuOpen = false;
    }
  }

  function beginEdit() {
    if (item.role !== "user") return;
    editText = item.text;
    editing = true;
    actionMenuOpen = false;
  }

  async function saveEdit() {
    if (!onedit || !editText.trim()) return;
    saving = true;
    try {
      await onedit(item, editText);
      editing = false;
    } catch (reason) {
      onannounce?.(
        reason instanceof Error
          ? `The message could not be saved: ${reason.message}`
          : "The message could not be saved.",
      );
    } finally {
      saving = false;
    }
  }

  async function readAloud() {
    if (item.role !== "assistant") return;
    listening = true;
    try {
      if (onlisten) {
        await onlisten(item);
      } else if ("speechSynthesis" in window) {
        window.speechSynthesis.cancel();
        window.speechSynthesis.speak(new SpeechSynthesisUtterance(item.text));
      }
      onannounce?.("Reading response aloud.");
    } catch (reason) {
      onannounce?.(
        reason instanceof Error
          ? `Read aloud failed: ${reason.message}`
          : "Read aloud failed.",
      );
    } finally {
      listening = false;
      actionMenuOpen = false;
    }
  }
</script>

{#if item.role === "event"}
  <AgentEventCard {item} />
{:else if item.role === "developer"}
  <article class="developer-card">
    <button
      type="button"
      aria-expanded={developerOpen}
      onclick={() => (developerOpen = !developerOpen)}
    >
      <span><Icon name="code" size={15} /> {item.title}</span>
      <span>{developerOpen ? "Hide" : "Inspect"}</span>
    </button>
    {#if developerOpen}
      <pre>{JSON.stringify(item.developer, null, 2)}</pre>
    {/if}
  </article>
{:else}
  <article class:message--user={item.role === "user"} class="message">
    <header>
      <span class="message__meta">
        <span>{item.role === "user" ? "You" : "Carlina"}</span>
        {#if timestamp}<time datetime={item.created_at ?? undefined}>{timestamp}</time>{/if}
      </span>
      {#if !editing}
        <span class="message-menu">
          <button
            class="message-menu__trigger"
            type="button"
            aria-label="Message actions"
            aria-expanded={actionMenuOpen}
            onclick={() => (actionMenuOpen = !actionMenuOpen)}
          >
            <Icon name="more" size={15} />
          </button>
          {#if actionMenuOpen}
            <span class="message-menu__panel" role="menu">
              <button type="button" role="menuitem" onclick={copyText}>
                <Icon name="copy" size={13} /> Copy
              </button>
              {#if item.role === "user"}
                <button type="button" role="menuitem" disabled={busy} onclick={beginEdit}>
                  <Icon name="edit" size={13} /> Edit
                </button>
                <button
                  type="button"
                  role="menuitem"
                  disabled={busy}
                  onclick={() => {
                    actionMenuOpen = false;
                    onrerun?.(item);
                  }}
                >
                  <Icon name="refresh" size={13} /> Rerun from here
                </button>
                <button
                  class="danger"
                  type="button"
                  role="menuitem"
                  disabled={busy}
                  onclick={() => {
                    actionMenuOpen = false;
                    void ondelete?.(item);
                  }}
                >
                  <Icon name="trash" size={13} /> Delete
                </button>
              {:else}
                <button type="button" role="menuitem" disabled={listening} onclick={readAloud}>
                  <Icon name="volume" size={13} />
                  {listening ? "Preparing…" : "Read aloud"}
                </button>
                <button
                  type="button"
                  role="menuitem"
                  onclick={() => {
                    actionMenuOpen = false;
                    onfork?.(item);
                  }}
                >
                  <Icon name="fork" size={13} /> Fork conversation
                </button>
              {/if}
            </span>
          {/if}
        </span>
      {/if}
    </header>

    {#if editing && item.role === "user"}
      <div class="message__edit">
        <textarea bind:value={editText} rows="4" aria-label="Edit message"></textarea>
        <div>
          <button type="button" onclick={() => (editing = false)}>Cancel</button>
          <button
            class="primary"
            type="button"
            disabled={saving || !editText.trim()}
            onclick={saveEdit}
          >
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    {:else}
      {#if item.role === "assistant"}
        <div class="message__content prose">{@html item.html}</div>
      {:else}
        <p class="message__content">{item.text}</p>
      {/if}

      {#if item.role === "user" && item.attachments.length}
        <ul class="attachments" aria-label="Message attachments">
          {#each item.attachments as attachment (attachment.id)}
            <li>
              <Icon name="file" size={14} />
              {#if attachment.url}
                <a href={attachment.url} target="_blank" rel="noopener">
                  {attachment.name}
                </a>
              {:else}
                <span>{attachment.name}</span>
              {/if}
              {#if attachment.strategy}
                <small>{attachment.strategy.replaceAll("_", " ")}</small>
              {/if}
            </li>
          {/each}
        </ul>
      {/if}
    {/if}

  </article>
{/if}

<style>
  .message {
    width: min(50rem, calc(100% - 1.25rem));
    content-visibility: auto;
    contain-intrinsic-size: auto 7rem;
    margin: 0 auto;
    padding: 0.58rem 0.85rem 0.48rem;
    border-block: 1px solid transparent;
  }

  .message--user {
    border-color: var(--color-rule);
    background:
      linear-gradient(
        90deg,
        var(--color-clay-muted),
        color-mix(in srgb, var(--color-clay-muted) 42%, transparent)
      );
  }

  header {
    position: relative;
    display: flex;
    align-items: center;
    justify-content: space-between;
    min-height: 1.25rem;
    margin-bottom: 0.22rem;
    color: var(--color-ink-muted);
    font-size: 0.55rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  time {
    font-weight: 500;
    letter-spacing: 0.04em;
  }

  .message__meta {
    display: inline-flex;
    align-items: baseline;
    gap: 0.45rem;
  }

  .message-menu {
    position: relative;
    display: block;
    line-height: 1;
  }

  .message-menu__trigger {
    width: 1.6rem;
    height: 1.45rem;
    display: grid;
    place-items: center;
    border: 1px solid transparent;
    padding: 0;
    background: transparent;
    color: var(--color-ink-muted);
    cursor: pointer;
  }

  .message-menu__trigger:hover,
  .message-menu__trigger[aria-expanded="true"] {
    border-color: var(--color-rule);
    background: var(--color-surface);
    color: var(--color-ink);
  }

  .message-menu__panel {
    position: absolute;
    z-index: 12;
    top: calc(100% + 0.2rem);
    right: 0;
    width: 10.5rem;
    display: grid;
    border: 1px solid var(--color-rule-strong);
    padding: 0.25rem;
    background: var(--color-surface);
    box-shadow: 0 0.65rem 1.8rem
      color-mix(in srgb, var(--color-ink) 14%, transparent);
  }

  .message-menu__panel button {
    min-height: 2rem;
    display: flex;
    align-items: center;
    gap: 0.45rem;
    border: 0;
    padding: 0 0.5rem;
    background: transparent;
    color: var(--color-ink-soft);
    cursor: pointer;
    font-size: 0.62rem;
    text-align: left;
  }

  .message-menu__panel button:hover {
    background: var(--color-surface-muted);
    color: var(--color-ink);
  }

  .message-menu__panel button.danger {
    border-top: 1px solid var(--color-rule);
    color: var(--color-danger);
  }

  .message-menu__panel button:disabled {
    cursor: not-allowed;
    opacity: 0.45;
  }

  .message__content {
    margin: 0;
    color: var(--color-ink);
    font-family: var(--font-editorial);
    font-size: clamp(0.88rem, 0.84rem + 0.14vw, 0.96rem);
    line-height: 1.42;
    white-space: pre-wrap;
    user-select: text;
    -webkit-user-select: text;
  }

  .message--user .message__content {
    font-family: var(--font-sans);
    font-size: 0.8rem;
    line-height: 1.4;
  }

  :global(.message__content.prose) {
    white-space: normal;
  }

  :global(.prose > :first-child) {
    margin-top: 0;
  }

  :global(.prose > :last-child) {
    margin-bottom: 0;
  }

  :global(.prose p),
  :global(.prose ul),
  :global(.prose ol),
  :global(.prose dl),
  :global(.prose table),
  :global(.prose pre),
  :global(.prose blockquote) {
    margin-block: 0.42em;
  }

  :global(.prose h1),
  :global(.prose h2),
  :global(.prose h3),
  :global(.prose h4),
  :global(.prose h5),
  :global(.prose h6) {
    margin: 0.8em 0 0.3em;
    font-family: var(--font-editorial);
    font-weight: 550;
    line-height: 1.15;
  }

  :global(.prose h1) {
    font-size: 1.25rem;
  }

  :global(.prose h2) {
    font-size: 1.1rem;
  }

  :global(.prose h3) {
    font-size: 0.98rem;
  }

  :global(.prose h4),
  :global(.prose h5),
  :global(.prose h6) {
    font-size: 0.9rem;
  }

  :global(.prose ul),
  :global(.prose ol) {
    padding-inline-start: 1.65rem;
    list-style-position: outside;
  }

  :global(.prose ul) {
    list-style-type: disc;
  }

  :global(.prose ol) {
    list-style-type: decimal;
  }

  :global(.prose li) {
    margin-block: 0.22em;
    padding-inline-start: 0.18rem;
  }

  :global(.prose li > p) {
    margin-block: 0.2em;
  }

  :global(.prose li > ul),
  :global(.prose li > ol) {
    margin-block: 0.3em;
    padding-inline-start: 1.45rem;
  }

  :global(.prose ul ul) {
    list-style-type: circle;
  }

  :global(.prose ul ul ul) {
    list-style-type: square;
  }

  :global(.prose ol ol) {
    list-style-type: lower-alpha;
  }

  :global(.prose ol ol ol) {
    list-style-type: lower-roman;
  }

  :global(.prose li::marker) {
    color: var(--color-clay);
    font-family: var(--font-sans);
    font-size: 0.82em;
    font-weight: 700;
  }

  :global(.prose input[type="checkbox"]) {
    margin: 0 0.35rem 0 -1.1rem;
    accent-color: var(--color-lichen);
  }

  :global(.prose a) {
    color: var(--color-clay);
    text-decoration-thickness: 1px;
    text-underline-offset: 0.22rem;
  }

  :global(.prose code) {
    padding: 0.08em 0.25em;
    background: var(--color-surface-muted);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.82em;
  }

  :global(.prose pre) {
    overflow-x: auto;
    border-left: 2px solid var(--color-lichen);
    padding: 0.55rem 0.7rem;
    background: var(--color-surface-muted);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.68rem;
    line-height: 1.42;
  }

  :global(.prose pre code) {
    padding: 0;
    background: transparent;
    color: inherit;
    font-size: inherit;
  }

  :global(.prose blockquote) {
    border-left: 2px solid var(--color-clay);
    margin-inline: 0;
    padding-left: 1rem;
    color: var(--color-ink-soft);
    font-style: italic;
  }

  :global(.prose blockquote > :first-child) {
    margin-top: 0;
  }

  :global(.prose blockquote > :last-child) {
    margin-bottom: 0;
  }

  :global(.prose table) {
    width: 100%;
    max-width: 100%;
    display: block;
    overflow-x: auto;
    border-collapse: collapse;
    background: var(--color-surface);
    font-family: var(--font-sans);
    font-size: 0.7rem;
    line-height: 1.35;
    white-space: nowrap;
  }

  :global(.prose th),
  :global(.prose td) {
    min-width: 6rem;
    border: 1px solid var(--color-rule);
    padding: 0.38rem 0.5rem;
    text-align: left;
    vertical-align: top;
  }

  :global(.prose th) {
    background: var(--color-surface-muted);
    color: var(--color-ink);
    font-weight: 700;
  }

  :global(.prose tbody tr:nth-child(even)) {
    background: color-mix(in srgb, var(--color-paper) 72%, transparent);
  }

  :global(.prose hr) {
    border: 0;
    border-top: 1px solid var(--color-rule-strong);
    margin: 0.75rem 0;
  }

  :global(.prose img),
  :global(.prose video) {
    max-width: 100%;
    height: auto;
    border: 1px solid var(--color-rule);
  }

  :global(.prose dt) {
    margin-top: 0.35rem;
    font-weight: 700;
  }

  :global(.prose dd) {
    margin-left: 1rem;
    color: var(--color-ink-soft);
  }

  :global(.prose details) {
    border: 1px solid var(--color-rule);
    margin-block: 0.45rem;
    padding: 0.45rem 0.55rem;
    background: var(--color-surface);
  }

  :global(.prose summary) {
    cursor: pointer;
    font-family: var(--font-sans);
    font-size: 0.72rem;
    font-weight: 700;
  }

  :global(.prose kbd) {
    border: 1px solid var(--color-rule-strong);
    border-bottom-width: 2px;
    padding: 0.05rem 0.28rem;
    background: var(--color-surface-muted);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.72em;
  }

  .attachments {
    display: grid;
    gap: 1px;
    margin: 0.5rem 0 0;
    padding: 0;
    background: var(--color-rule);
    list-style: none;
  }

  .attachments li {
    min-height: 2.5rem;
    display: flex;
    align-items: center;
    gap: 0.55rem;
    padding: 0.45rem 0.65rem;
    background: var(--color-surface);
    color: var(--color-ink-soft);
    font-size: 0.7rem;
  }

  .attachments a {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .attachments small {
    margin-left: auto;
    color: var(--color-ink-muted);
    text-transform: capitalize;
  }

  .message__edit {
    display: grid;
    gap: 0.6rem;
  }

  .message__edit textarea {
    width: 100%;
    resize: vertical;
    border: 1px solid var(--color-rule-strong);
    padding: 0.75rem;
    background: var(--color-surface);
    line-height: 1.5;
  }

  .message__edit > div {
    display: flex;
    justify-content: flex-end;
    gap: 0.4rem;
  }

  .message__edit button {
    min-height: 2rem;
    border: 1px solid var(--color-rule);
    padding: 0 0.7rem;
    background: var(--color-surface);
    cursor: pointer;
    font-size: 0.68rem;
  }

  .message__edit button.primary {
    border-color: var(--color-lichen);
    background: var(--color-lichen);
    color: var(--color-surface);
  }

  .developer-card {
    width: min(45rem, calc(100% - 2rem));
    margin: 0.35rem auto;
    border: 1px dashed var(--color-rule-strong);
    background: var(--color-surface-muted);
  }

  .developer-card > button {
    width: 100%;
    min-height: 2.6rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    border: 0;
    padding: 0 0.75rem;
    background: transparent;
    color: var(--color-ink-soft);
    cursor: pointer;
    font-size: 0.68rem;
  }

  .developer-card > button span:first-child {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
  }

  .developer-card pre {
    max-height: 26rem;
    overflow: auto;
    border-top: 1px dashed var(--color-rule-strong);
    margin: 0;
    padding: 0.8rem;
    background: var(--color-surface);
    font-size: 0.67rem;
    line-height: 1.5;
    white-space: pre-wrap;
  }

  @media (max-width: 42rem) {
    .message {
      width: calc(100% - 0.5rem);
      padding-inline: 0.65rem;
    }
  }
</style>
