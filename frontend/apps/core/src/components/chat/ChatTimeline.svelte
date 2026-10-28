<script lang="ts">
  import type {
    FrontendAgentEventMessage,
    FrontendChatTimelineItem,
    FrontendDeveloperChatMessage,
  } from "@carlina/sdk";
  import { Icon } from "@carlina/ui";

  import MessageCard from "./MessageCard.svelte";
  import ToolActivityGroup from "./ToolActivityGroup.svelte";

  type ActivityItem =
    | FrontendAgentEventMessage
    | FrontendDeveloperChatMessage;
  type TimelineBlock =
    | {
        kind: "message";
        key: string;
        item: FrontendChatTimelineItem;
      }
    | {
        kind: "activity";
        key: string;
        items: ActivityItem[];
      };

  interface Props {
    items: FrontendChatTimelineItem[];
    loading: boolean;
    loadingOlder: boolean;
    hasMore: boolean;
    busy: boolean;
    error: string;
    status: string;
    followRevision: number;
    onloadolder: () => Promise<void>;
    onedit: (item: FrontendChatTimelineItem, text: string) => Promise<void>;
    ondelete: (item: FrontendChatTimelineItem) => Promise<void>;
    onrerun: (item: FrontendChatTimelineItem) => void;
    onfork: (item: FrontendChatTimelineItem) => void;
    onlisten: (item: FrontendChatTimelineItem) => Promise<void>;
    onannounce: (message: string) => void;
  }

  let {
    items,
    loading,
    loadingOlder,
    hasMore,
    busy,
    error,
    status,
    followRevision,
    onloadolder,
    onedit,
    ondelete,
    onrerun,
    onfork,
    onlisten,
    onannounce,
  }: Props = $props();

  let viewport: HTMLElement;
  const blocks = $derived.by<TimelineBlock[]>(() => {
    const result: TimelineBlock[] = [];
    let activity: ActivityItem[] = [];
    const flushActivity = () => {
      if (!activity.length) return;
      result.push({
        kind: "activity",
        key: `activity:${activity[0]!.id}`,
        items: activity,
      });
      activity = [];
    };
    for (const item of items) {
      if (item.role === "event" || item.role === "developer") {
        activity.push(item);
        continue;
      }
      flushActivity();
      result.push({
        kind: "message",
        key: `message:${item.id}`,
        item,
      });
    }
    flushActivity();
    return result;
  });

  $effect(() => {
    followRevision;
    if (!viewport) return;
    requestAnimationFrame(() => {
      viewport.scrollTo({
        top: viewport.scrollHeight,
        behavior: busy ? "smooth" : "auto",
      });
    });
  });

  async function loadOlder() {
    const previousHeight = viewport.scrollHeight;
    const previousTop = viewport.scrollTop;
    await onloadolder();
    requestAnimationFrame(() => {
      viewport.scrollTop =
        previousTop + Math.max(0, viewport.scrollHeight - previousHeight);
    });
  }
</script>

<section class="timeline" bind:this={viewport} aria-label="Conversation">
  <div class="timeline__inner">
    {#if hasMore}
      <button
        class="load-older"
        type="button"
        disabled={loadingOlder}
        onclick={loadOlder}
      >
        <Icon name="arrow-left" size={14} />
        {loadingOlder ? "Loading earlier messages…" : "Load earlier messages"}
      </button>
    {/if}

    {#if loading && !items.length}
      <div class="timeline__loading" aria-busy="true" aria-label="Loading conversation">
        <span></span><span></span><span></span>
      </div>
    {:else if !items.length}
      <div class="welcome">
        <p class="welcome__kicker">A new conversation</p>
        <h1>What shall we work through?</h1>
        <p>
          Begin with a question, a rough brief, or source material. Controls stay
          close to the writing surface and detail appears only when you ask for it.
        </p>
        <div class="welcome__notes" aria-label="Conversation capabilities">
          <span>Files & media</span>
          <span>Scoped tools</span>
          <span>Durable runs</span>
        </div>
      </div>
    {:else}
      <div class="timeline__messages">
        {#each blocks as block (block.key)}
          {#if block.kind === "activity"}
            <ToolActivityGroup items={block.items} />
          {:else}
            <MessageCard
              item={block.item}
              {busy}
              {onedit}
              {ondelete}
              {onrerun}
              {onfork}
              {onlisten}
              {onannounce}
            />
          {/if}
        {/each}
      </div>
    {/if}

    {#if busy}
      <div class="thinking" role="status">
        <span class="thinking__mark"><Icon name="activity" size={15} /></span>
        <span>{status || "Working…"}</span>
        <i></i><i></i><i></i>
      </div>
    {/if}

    {#if error}
      <div class="timeline__error" role="alert">
        <Icon name="activity" size={16} />
        <span>{error}</span>
      </div>
    {/if}
  </div>
</section>

<style>
  .timeline {
    min-height: 0;
    overflow-y: auto;
    overscroll-behavior: contain;
    scrollbar-gutter: stable;
    scroll-behavior: smooth;
  }

  .timeline__inner {
    min-height: 100%;
    display: flex;
    flex-direction: column;
    padding: 0.7rem 0 0.65rem;
  }

  .timeline__messages {
    display: grid;
    gap: 0.22rem;
  }

  .load-older {
    align-self: center;
    min-height: 2.2rem;
    display: inline-flex;
    align-items: center;
    gap: 0.45rem;
    border: 1px solid var(--color-rule);
    margin: 0 auto 1.75rem;
    padding: 0 0.7rem;
    background: var(--color-surface);
    color: var(--color-ink-muted);
    cursor: pointer;
    font-size: 0.67rem;
  }

  .load-older:hover {
    border-color: var(--color-rule-strong);
    color: var(--color-ink);
  }

  .welcome {
    width: min(45rem, calc(100% - 2rem));
    display: grid;
    align-content: center;
    flex: 1;
    margin: auto;
    padding: clamp(2rem, 7vh, 6rem) 0;
  }

  .welcome__kicker {
    margin: 0 0 0.7rem;
    color: var(--color-clay);
    font-size: 0.66rem;
    font-weight: 750;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .welcome h1 {
    max-width: 36rem;
    margin: 0;
    font-family: var(--font-editorial);
    font-size: clamp(2.4rem, 6vw, 4.8rem);
    font-weight: 450;
    letter-spacing: -0.035em;
    line-height: 0.98;
  }

  .welcome > p:not(.welcome__kicker) {
    max-width: 35rem;
    margin: 1.5rem 0 0;
    color: var(--color-ink-soft);
    font-family: var(--font-editorial);
    font-size: 1.05rem;
    line-height: 1.65;
  }

  .welcome__notes {
    display: flex;
    flex-wrap: wrap;
    gap: 1px;
    margin-top: 2rem;
    background: var(--color-rule);
  }

  .welcome__notes span {
    padding: 0.55rem 0.75rem;
    background: var(--color-surface-muted);
    color: var(--color-ink-muted);
    font-size: 0.62rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .thinking {
    width: min(50rem, calc(100% - 1.25rem));
    min-height: 2.2rem;
    display: flex;
    align-items: center;
    gap: 0.55rem;
    margin: 0.25rem auto 0;
    border-top: 1px solid var(--color-rule);
    color: var(--color-ink-muted);
    font-size: 0.7rem;
  }

  .thinking__mark {
    width: 1.45rem;
    height: 1.45rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-lichen);
    color: var(--color-lichen);
  }

  .thinking i {
    width: 3px;
    height: 3px;
    background: var(--color-clay);
    animation: pulse 1.2s infinite ease-in-out;
  }

  .thinking i:nth-last-child(2) {
    animation-delay: 120ms;
  }

  .thinking i:last-child {
    animation-delay: 240ms;
  }

  .timeline__error {
    width: min(43rem, calc(100% - 2rem));
    display: flex;
    gap: 0.6rem;
    margin: 1rem auto 0;
    border-left: 2px solid var(--color-danger);
    padding: 0.75rem;
    background: var(--color-danger-muted);
    color: var(--color-danger);
    font-size: 0.72rem;
  }

  .timeline__loading {
    width: min(48rem, calc(100% - 2rem));
    display: grid;
    gap: 1rem;
    margin: auto;
  }

  .timeline__loading span {
    height: 8rem;
    background: linear-gradient(
      90deg,
      var(--color-surface-muted),
      var(--color-surface),
      var(--color-surface-muted)
    );
    background-size: 200% 100%;
    animation: loading 1.5s infinite linear;
  }

  .timeline__loading span:nth-child(2) {
    width: 85%;
    justify-self: end;
    height: 5rem;
  }

  @keyframes pulse {
    0%,
    100% {
      opacity: 0.25;
    }
    50% {
      opacity: 1;
    }
  }

  @keyframes loading {
    to {
      background-position: -200% 0;
    }
  }

  @media (max-width: 42rem) {
    .timeline__inner {
      padding-top: 0.4rem;
    }

    .welcome {
      width: calc(100% - 2rem);
    }
  }
</style>
