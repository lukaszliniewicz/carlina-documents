<script lang="ts">
  import { onMount } from "svelte";
  import {
    shouldUseClientNavigation,
    type FrontendAdministrationDashboard,
    type FrontendAdministrationView,
    type FrontendApiClient,
  } from "@carlina/sdk";
  import { Button, Icon, PageHeader } from "@carlina/ui";

  import {
    administrationViewFromRoute,
    administrationViewHref,
  } from "../../lib/administration.ts";
  import ContinuityAdministrationPanel from "./administration/ContinuityAdministrationPanel.svelte";
  import GeneralAdministrationPanel from "./administration/GeneralAdministrationPanel.svelte";
  import MembersAdministrationPanel from "./administration/MembersAdministrationPanel.svelte";
  import RetrievalAdministrationPanel from "./administration/RetrievalAdministrationPanel.svelte";
  import UsageAdministrationPanel from "./administration/UsageAdministrationPanel.svelte";

  interface Props {
    api: FrontendApiClient;
    route: string;
    navigate: (
      href: string,
      clientRoute?: boolean,
      options?: { replace?: boolean },
    ) => "client" | "document";
  }

  let { api, route, navigate }: Props = $props();

  let dashboard = $state<FrontendAdministrationDashboard | null>(null);
  let loading = $state(true);
  let refreshing = $state(false);
  let error = $state("");

  const view = $derived(administrationViewFromRoute(route));
  const views: Array<{
    id: FrontendAdministrationView;
    label: string;
    description: string;
  }> = [
    {
      id: "overview",
      label: "Publishing",
      description: "Public address, policy, and session posture",
    },
    {
      id: "retrieval",
      label: "Retrieval",
      description: "Embedding structure and index maintenance",
    },
    {
      id: "continuity",
      label: "Continuity",
      description: "Verified backups and deliberate recovery",
    },
    {
      id: "members",
      label: "Members",
      description: "Accounts and administrative roles",
    },
    {
      id: "usage",
      label: "Usage",
      description: "Model activity and cost ledger",
    },
  ];

  onMount(() => {
    void loadDashboard();
  });

  async function loadDashboard(background = false): Promise<void> {
    if (background) refreshing = true;
    else loading = true;
    error = "";
    try {
      dashboard =
        await api.request<FrontendAdministrationDashboard>(
          "/api/frontend/settings/administration",
        );
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "Administration settings could not be loaded.";
    } finally {
      loading = false;
      refreshing = false;
    }
  }

  function activateView(
    target: FrontendAdministrationView,
    event: MouseEvent,
  ): void {
    if (!shouldUseClientNavigation(event)) return;
    event.preventDefault();
    navigate(administrationViewHref(target), true);
  }
</script>

<div class="administration-page">
  <PageHeader
    eyebrow="Settings / Administration"
    title="The operating room"
    description="Maintain the application’s public footing, retrieval structure, continuity, membership, and model-use ledger through bounded, human-readable controls."
  >
    {#snippet actions()}
      <Button
        variant="outline"
        disabled={loading || refreshing}
        onclick={() => void loadDashboard(true)}
      >
        <Icon name="refresh" size={14} />
        {refreshing ? "Refreshing…" : "Refresh readings"}
      </Button>
    {/snippet}
  </PageHeader>

  {#if error}
    <section class="administration-error" role="alert">
      <Icon name="activity" size={20} />
      <div>
        <strong>Administration unavailable</strong>
        <p>{error}</p>
      </div>
      <Button variant="outline" onclick={() => void loadDashboard()}>
        Try again
      </Button>
    </section>
  {:else if loading}
    <section
      class="administration-loading"
      aria-label="Loading administration"
      aria-busy="true"
    >
      <span></span><span></span><span></span>
    </section>
  {:else if dashboard}
    <section class="reading-strip" aria-label="System summary">
      <article>
        <p>Members</p>
        <strong>{dashboard.summary.members}</strong>
        <span>{dashboard.summary.administrators} administrators</span>
      </article>
      <article>
        <p>Verified backups</p>
        <strong>{dashboard.summary.backups}</strong>
        <span>available recovery points</span>
      </article>
      <article>
        <p>Retrieval library</p>
        <strong>{dashboard.summary.rag_documents.toLocaleString()}</strong>
        <span>
          {dashboard.summary.rag_needs_review
            ? "configuration needs review"
            : "configuration in bounds"}
        </span>
      </article>
    </section>

    <nav class="administration-nav" aria-label="Administration sections">
      {#each views as item, index (item.id)}
        <a
          href={administrationViewHref(item.id)}
          class:active={view === item.id}
          aria-current={view === item.id ? "page" : undefined}
          onclick={(event) => activateView(item.id, event)}
        >
          <span>{String(index + 1).padStart(2, "0")}</span>
          <strong>{item.label}</strong>
          <small>{item.description}</small>
        </a>
      {/each}
    </nav>

    {#if view === "retrieval"}
      <RetrievalAdministrationPanel
        {api}
        value={dashboard.retrieval}
        onrefresh={() => loadDashboard(true)}
      />
    {:else if view === "continuity"}
      <ContinuityAdministrationPanel
        {api}
        value={dashboard.continuity}
        onrefresh={() => loadDashboard(true)}
      />
    {:else if view === "members"}
      <MembersAdministrationPanel
        {api}
        value={dashboard.members}
        onrefresh={() => loadDashboard(true)}
      />
    {:else if view === "usage"}
      <UsageAdministrationPanel
        {api}
        usageUrl={dashboard.actions.usage_url}
      />
    {:else}
      <GeneralAdministrationPanel
        {api}
        value={dashboard.general}
        updateUrl={dashboard.actions.general_update_url}
        onrefresh={() => loadDashboard(true)}
      />
    {/if}
  {/if}
</div>

<style>
  .administration-page {
    width: min(100%, 100rem);
    margin: 0 auto;
    padding: clamp(1.4rem, 4vw, 3.5rem);
  }

  .reading-strip {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    border: 1px solid var(--color-rule-strong);
    margin-top: 1.5rem;
    background: var(--color-paper);
  }

  .reading-strip article {
    min-width: 0;
    display: grid;
    gap: 0.25rem;
    border-right: 1px solid var(--color-rule);
    padding: 1rem 1.15rem;
  }

  .reading-strip article:last-child {
    border-right: 0;
  }

  .reading-strip p,
  .reading-strip span {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.65rem;
  }

  .reading-strip p {
    color: var(--color-clay);
    font-weight: 800;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .reading-strip strong {
    font-family: var(--font-editorial);
    font-size: 1.65rem;
    font-weight: 520;
  }

  .administration-nav {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    border-inline: 1px solid var(--color-rule-strong);
    border-bottom: 1px solid var(--color-rule-strong);
    background: var(--color-surface);
  }

  .administration-nav a {
    min-width: 0;
    display: grid;
    gap: 0.3rem;
    border-right: 1px solid var(--color-rule);
    padding: 0.9rem 1rem 1rem;
    color: var(--color-ink-muted);
    text-decoration: none;
  }

  .administration-nav a:last-child {
    border-right: 0;
  }

  .administration-nav a:hover,
  .administration-nav a.active {
    background: var(--color-clay-muted);
    color: var(--color-ink);
  }

  .administration-nav a.active {
    box-shadow: inset 0 -3px 0 var(--color-clay);
  }

  .administration-nav span {
    color: var(--color-clay);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.57rem;
  }

  .administration-nav strong {
    font-family: var(--font-editorial);
    font-size: 1rem;
    font-weight: 560;
  }

  .administration-nav small {
    overflow-wrap: anywhere;
    font-size: 0.62rem;
    line-height: 1.45;
  }

  .administration-error {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.85rem;
    border-block: 1px solid var(--color-danger);
    margin-top: 1.5rem;
    padding: 1rem;
  }

  .administration-error strong,
  .administration-error p {
    margin: 0;
  }

  .administration-error p {
    margin-top: 0.2rem;
    color: var(--color-danger);
    font-size: 0.72rem;
  }

  .administration-loading {
    display: grid;
    gap: 1px;
    border: 1px solid var(--color-rule);
    margin-top: 1.5rem;
    background: var(--color-rule);
  }

  .administration-loading span {
    height: 8rem;
    background: linear-gradient(
      90deg,
      var(--color-paper),
      var(--color-surface),
      var(--color-paper)
    );
    background-size: 200% 100%;
    animation: loading 1.5s ease-in-out infinite;
  }

  @keyframes loading {
    to {
      background-position: -200% 0;
    }
  }

  @media (max-width: 70rem) {
    .administration-nav {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }

    .administration-nav a:nth-child(3) {
      border-right: 0;
    }

    .administration-nav a:nth-child(n + 4) {
      border-top: 1px solid var(--color-rule);
    }
  }

  @media (max-width: 44rem) {
    .administration-page {
      padding-inline: 0.75rem;
    }

    .reading-strip {
      grid-template-columns: 1fr;
    }

    .reading-strip article,
    .reading-strip article:last-child {
      border-right: 0;
      border-bottom: 1px solid var(--color-rule);
    }

    .reading-strip article:last-child {
      border-bottom: 0;
    }

    .administration-nav {
      grid-template-columns: 1fr;
    }

    .administration-nav a,
    .administration-nav a:nth-child(3),
    .administration-nav a:nth-child(n + 4) {
      border-right: 0;
      border-top: 1px solid var(--color-rule);
    }

    .administration-nav a:first-child {
      border-top: 0;
    }
  }
</style>
