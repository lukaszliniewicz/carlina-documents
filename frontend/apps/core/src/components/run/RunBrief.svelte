<script lang="ts">
  import type {
    FrontendRunApproval,
    FrontendRunDocument,
    FrontendRunInteraction,
  } from "@carlina/sdk";
  import { Button, Icon } from "@carlina/ui";

  import StructuredData from "../context/StructuredData.svelte";

  interface Props {
    documents: {
      understanding: FrontendRunDocument;
      plan: FrontendRunDocument;
      state: FrontendRunDocument;
    };
    approvals: FrontendRunApproval[];
    interactions: FrontendRunInteraction[];
    open?: boolean;
    busyId: string | null;
    onapproval: (approval: FrontendRunApproval, action: "approved" | "rejected") => void;
    oninteraction: (
      interaction: FrontendRunInteraction,
      action: "approved" | "rejected",
    ) => void;
  }

  let {
    documents,
    approvals,
    interactions,
    open = false,
    busyId,
    onapproval,
    oninteraction,
  }: Props = $props();

  const pendingApprovals = $derived(
    approvals.filter((approval) => approval.status === "pending"),
  );
  const pendingInteractions = $derived(
    interactions.filter((interaction) => interaction.status === "pending"),
  );
  const documentColumns = $derived<
    Array<{ title: string; document: FrontendRunDocument }>
  >([
    { title: "Understanding", document: documents.understanding },
    { title: "Plan", document: documents.plan },
    { title: "Live state", document: documents.state },
  ]);
</script>

<details class="run-brief" {open}>
  <summary>
    <span>
      <small>Run brief</small>
      <strong>Understanding, plan, and live state</strong>
    </span>
    <span class="brief-counts">
      {documents.plan.sections.length} plan sections
      {#if pendingApprovals.length + pendingInteractions.length}
        · {pendingApprovals.length + pendingInteractions.length} awaiting input
      {/if}
    </span>
  </summary>

  {#if pendingApprovals.length || pendingInteractions.length}
    <section class="decisions" aria-label="Decisions awaiting attention">
      <header>
        <Icon name="shield" size={18} />
        <div>
          <strong>Decisions awaiting attention</strong>
          <span>Review the declared effects before allowing the run to continue.</span>
        </div>
      </header>

      {#each pendingApprovals as approval (approval.id)}
        <article class="decision">
          <div>
            <small>Approval · {approval.subject_type.replace(/[_-]+/g, " ")}</small>
            <strong>{approval.tool_id || approval.subject_id}</strong>
            {#if approval.effects.length}
              <p>{approval.effects.join(" · ")}</p>
            {/if}
            {#if Object.keys((approval.preview as object) ?? {}).length}
              <StructuredData value={approval.preview} compact maxRows={20} />
            {/if}
          </div>
          {#if approval.can_resolve}
            <nav aria-label="Approval actions">
              <Button
                size="sm"
                onclick={() => onapproval(approval, "approved")}
                disabled={busyId === approval.id}
              >
                Approve
              </Button>
              <Button
                size="sm"
                variant="outline"
                onclick={() => onapproval(approval, "rejected")}
                disabled={busyId === approval.id}
              >
                Reject
              </Button>
            </nav>
          {/if}
        </article>
      {/each}

      {#each pendingInteractions as interaction (interaction.id)}
        <article class="decision">
          <div>
            <small>Interaction · {interaction.type.replace(/[_-]+/g, " ")}</small>
            <strong>{interaction.tool_name || "Run input requested"}</strong>
            <StructuredData value={interaction.request} compact maxRows={20} />
          </div>
          {#if interaction.can_resolve}
            <nav aria-label="Interaction actions">
              <Button
                size="sm"
                onclick={() => oninteraction(interaction, "approved")}
                disabled={busyId === interaction.id}
              >
                Confirm
              </Button>
              <Button
                size="sm"
                variant="outline"
                onclick={() => oninteraction(interaction, "rejected")}
                disabled={busyId === interaction.id}
              >
                Decline
              </Button>
            </nav>
          {/if}
        </article>
      {/each}
    </section>
  {/if}

  <div class="brief-columns">
    {#each documentColumns as { title, document } (title)}
      <section class="brief-document">
        <header>
          <small>{title}</small>
          <p>{document.summary}</p>
        </header>
        {#if document.sections.length}
          <div class="document-sections">
            {#each document.sections as section (section.id)}
              <article>
                <strong>{section.title}</strong>
                {#if section.kind === "text"}
                  <p>{String(section.value)}</p>
                {:else}
                  <p>{section.summary}</p>
                  <StructuredData value={section.value} compact maxRows={24} />
                {/if}
              </article>
            {/each}
          </div>
        {:else}
          <p class="empty">Nothing has been recorded here yet.</p>
        {/if}
      </section>
    {/each}
  </div>
</details>

<style>
  .run-brief {
    border: 1px solid var(--color-rule);
    background: var(--color-surface);
  }

  .run-brief > summary {
    min-height: 3.8rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.75rem 0.9rem;
    cursor: pointer;
    list-style: none;
  }

  .run-brief > summary::-webkit-details-marker {
    display: none;
  }

  .run-brief[open] > summary {
    border-bottom: 1px solid var(--color-rule);
  }

  summary > span:first-child {
    display: grid;
    gap: 0.18rem;
  }

  summary small,
  .brief-document > header small,
  .decision small {
    color: var(--color-clay);
    font-size: 0.59rem;
    font-weight: 780;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  summary strong {
    font-family: var(--font-editorial);
    font-size: 1.02rem;
    font-weight: 560;
  }

  .brief-counts {
    color: var(--color-ink-muted);
    font-size: 0.62rem;
  }

  .decisions {
    display: grid;
    gap: 1px;
    border-bottom: 1px solid var(--color-rule);
    background: var(--color-rule);
  }

  .decisions > header {
    display: flex;
    align-items: center;
    gap: 0.7rem;
    padding: 0.8rem 0.9rem;
    background: var(--color-tobacco-muted);
    color: var(--color-tobacco);
  }

  .decisions > header div {
    display: grid;
    gap: 0.14rem;
  }

  .decisions > header strong {
    font-size: 0.72rem;
  }

  .decisions > header span {
    font-size: 0.62rem;
  }

  .decision {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: start;
    gap: 1rem;
    padding: 0.85rem 0.9rem;
    background: var(--color-surface);
  }

  .decision > div {
    min-width: 0;
    display: grid;
    gap: 0.42rem;
  }

  .decision > div > strong {
    font-family: var(--font-editorial);
    font-size: 0.92rem;
    font-weight: 560;
  }

  .decision p {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.65rem;
  }

  .decision nav {
    display: flex;
    gap: 0.4rem;
  }

  .brief-columns {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 1px;
    background: var(--color-rule);
  }

  .brief-document {
    min-width: 0;
    padding: 0.9rem;
    background: var(--color-surface);
  }

  .brief-document > header {
    display: grid;
    gap: 0.35rem;
    min-height: 5.2rem;
    align-content: start;
    border-bottom: 1px solid var(--color-rule);
    margin-bottom: 0.75rem;
    padding-bottom: 0.75rem;
  }

  .brief-document > header p,
  .document-sections p,
  .empty {
    margin: 0;
    color: var(--color-ink-soft);
    font-size: 0.68rem;
    line-height: 1.55;
  }

  .document-sections {
    display: grid;
    gap: 0.85rem;
  }

  .document-sections article {
    display: grid;
    gap: 0.32rem;
  }

  .document-sections strong {
    font-size: 0.68rem;
  }

  .empty {
    color: var(--color-ink-muted);
  }

  @media (max-width: 72rem) {
    .brief-columns {
      grid-template-columns: 1fr;
    }

    .brief-document > header {
      min-height: auto;
    }
  }

  @media (max-width: 42rem) {
    .run-brief > summary,
    .decision {
      align-items: flex-start;
      grid-template-columns: 1fr;
      flex-direction: column;
    }

    .decision nav {
      width: 100%;
    }
  }
</style>
