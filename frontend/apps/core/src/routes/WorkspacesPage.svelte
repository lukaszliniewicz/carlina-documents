<script lang="ts">
  import type {
    FrontendApiClient,
    FrontendBootstrap,
    FrontendUiContribution,
  } from "@carlina/sdk";
  import {
    findAddonApplication,
    shouldUseClientNavigation,
  } from "@carlina/sdk";
  import {
    EmptyState,
    Icon,
    PageHeader,
    SearchField,
  } from "@carlina/ui";

  interface Props {
    api: FrontendApiClient;
    bootstrap: FrontendBootstrap;
    route: string;
    navigate: (href: string, clientRoute?: boolean) => void;
  }

  let { bootstrap, navigate }: Props = $props();
  let query = $state("");

  const available = $derived(
    bootstrap.ui_contributions.filter(
      (item) =>
        ["active", "degraded"].includes(item.status) &&
        item.routes.length > 0,
    ),
  );
  const filtered = $derived.by(() => {
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return available;
    return available.filter((item) =>
      `${item.title} ${item.description} ${item.addon_id} ${item.section}`
        .toLocaleLowerCase()
        .includes(needle),
    );
  });
  const groups = $derived.by(() => {
    const grouped = new Map<string, FrontendUiContribution[]>();
    for (const item of filtered) {
      const section = item.section || "Workspaces";
      grouped.set(section, [...(grouped.get(section) ?? []), item]);
    }
    return [...grouped.entries()].sort(([left], [right]) =>
      left.localeCompare(right),
    );
  });

  function initials(title: string): string {
    return (
      title
        .split(/\s+/)
        .filter(Boolean)
        .slice(0, 2)
        .map((part) => part[0]?.toUpperCase())
        .join("") || "W"
    );
  }

  function openWorkspace(
    item: FrontendUiContribution,
    event: MouseEvent,
  ) {
    const href = item.routes[0];
    if (
      !href ||
      !findAddonApplication(bootstrap, href) ||
      !shouldUseClientNavigation(event)
    ) {
      return;
    }
    event.preventDefault();
    navigate(href, true);
  }

  function openAutomations(event: MouseEvent) {
    if (!shouldUseClientNavigation(event)) return;
    event.preventDefault();
    navigate("/automations", true);
  }
</script>

<div class="workspace-page">
  <PageHeader
    eyebrow="Directory / {available.length} available"
    title="Workspaces"
    description="Purpose-built rooms for focused work. The directory stays light: an addon is loaded only after you choose to enter it."
  >
    {#snippet actions()}
      <a class="studio-link" href="/automations" onclick={openAutomations}>
        Automation studio
        <Icon name="arrow-up-right" size={15} />
      </a>
    {/snippet}
  </PageHeader>

  <section class="directory-tools" aria-label="Workspace filters">
    <SearchField
      id="workspace-search"
      value={query}
      label="Filter workspaces"
      placeholder="Filter by name, purpose, or addon…"
      oninput={(value) => (query = value)}
    />
    <p>
      <strong>{filtered.length}</strong>
      {filtered.length === 1 ? "result" : "results"}
    </p>
  </section>

  {#if groups.length}
    <div class="workspace-groups">
      {#each groups as [section, items] (section)}
        <section class="workspace-group" aria-labelledby={`workspace-section-${section.replace(/\W+/g, "-")}`}>
          <header>
            <p>{section}</p>
            <span>{items.length.toString().padStart(2, "0")}</span>
          </header>
          <div class="workspace-grid">
            {#each items as item, index (item.id)}
              <a
                class="workspace-card"
                href={item.routes[0]}
                onclick={(event) => openWorkspace(item, event)}
              >
                <span class="workspace-card__number">
                  {(index + 1).toString().padStart(2, "0")}
                </span>
                <span class="workspace-card__copy">
                  <span class="workspace-card__header">
                    <span class="workspace-card__identity">{initials(item.title)}</span>
                    <span class="workspace-card__heading">
                      <span class="workspace-card__meta">
                        <span>{item.addon_id.replaceAll("_", " ")}</span>
                        <span class="workspace-status" title={item.status}>
                          <span
                            class="workspace-status__dot"
                            class:workspace-status__dot--active={item.status === "active"}
                            aria-hidden="true"
                          ></span>
                          <span class="sr-only">{item.status}</span>
                        </span>
                      </span>
                      <strong title={item.title}>{item.title}</strong>
                    </span>
                  </span>
                  <small>{item.description || "Open this focused workspace."}</small>
                </span>
                <Icon class="workspace-card__arrow" name="arrow-up-right" size={18} />
              </a>
            {/each}
          </div>
        </section>
      {/each}
    </div>
  {:else}
    <EmptyState
      icon="search"
      title={query ? "No matching workspace" : "No workspaces are available"}
      description={query
        ? "Try a broader phrase or clear the filter."
        : "Activate an addon with a workspace contribution, or compose a reusable automation."}
    >
      {#snippet actions()}
        {#if query}
          <button class="text-action" onclick={() => (query = "")}>Clear filter</button>
        {:else}
          <a class="text-action" href="/automations" onclick={openAutomations}>Open automation studio</a>
        {/if}
      {/snippet}
    </EmptyState>
  {/if}
</div>

<style>
  .workspace-page {
    width: min(100%, 90rem);
    margin: 0 auto;
    padding: clamp(1.4rem, 4vw, 3.5rem);
  }

  .studio-link,
  .text-action {
    min-height: 2.5rem;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.45rem;
    border: 1px solid var(--color-rule-strong);
    padding: 0.55rem 0.78rem;
    background: var(--color-surface);
    color: var(--color-ink);
    cursor: pointer;
    font-size: 0.72rem;
    font-weight: 680;
    text-decoration: none;
  }

  .studio-link:hover,
  .text-action:hover {
    background: var(--color-surface-muted);
  }

  .directory-tools {
    display: grid;
    grid-template-columns: minmax(15rem, 31rem) auto;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 1.2rem 0;
  }

  .directory-tools p {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.69rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .directory-tools strong {
    margin-right: 0.35rem;
    color: var(--color-ink);
    font-family: var(--font-editorial);
    font-size: 1rem;
    font-weight: 600;
  }

  .workspace-groups {
    display: grid;
    gap: 2.6rem;
  }

  .workspace-group > header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-block: 1px solid var(--color-rule-strong);
    padding: 0.65rem 0.15rem;
  }

  .workspace-group > header p,
  .workspace-group > header span {
    margin: 0;
    font-size: 0.63rem;
    font-weight: 750;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .workspace-group > header span {
    color: var(--color-clay);
    font-family: var(--font-editorial);
  }

  .workspace-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    border-left: 1px solid var(--color-rule);
  }

  .workspace-card {
    position: relative;
    min-height: 11.5rem;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: 1rem;
    border-right: 1px solid var(--color-rule);
    border-bottom: 1px solid var(--color-rule);
    padding: 1.25rem;
    background: color-mix(in srgb, var(--color-surface) 72%, transparent);
    color: inherit;
    text-decoration: none;
    transition:
      background var(--shell-transition),
      box-shadow var(--shell-transition);
  }

  .workspace-card:hover {
    background: var(--color-surface);
    box-shadow: inset 0 -3px 0 var(--color-clay);
  }

  .workspace-card__number {
    position: absolute;
    top: 0.52rem;
    right: 0.65rem;
    color: var(--color-rule-strong);
    font-family: var(--font-editorial);
    font-size: 0.65rem;
  }

  .workspace-card__identity {
    width: 2.65rem;
    height: 2.65rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-rule-strong);
    background: var(--color-clay-muted);
    color: var(--color-clay);
    font-family: var(--font-editorial);
    font-size: 0.86rem;
    font-weight: 600;
    letter-spacing: 0.04em;
  }

  .workspace-card__copy {
    min-width: 0;
    display: grid;
    gap: 0.8rem;
  }

  .workspace-card__header {
    min-width: 0;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: center;
    gap: 1rem;
  }

  .workspace-card__heading {
    min-width: 0;
    display: grid;
    gap: 0.35rem;
  }

  .workspace-card__meta {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    color: var(--color-ink-muted);
    font-size: 0.58rem;
    letter-spacing: 0.09em;
    text-transform: uppercase;
  }

  .workspace-status {
    display: inline-flex;
    align-items: center;
  }

  .workspace-status__dot {
    width: 0.58rem;
    height: 0.58rem;
    border-radius: 50%;
    background: var(--color-tobacco);
  }

  .workspace-status__dot--active {
    background: var(--color-lichen);
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  .workspace-card__heading > strong {
    min-width: 0;
    font-family: var(--font-editorial);
    font-size: clamp(1.2rem, 2.2vw, 1.55rem);
    font-weight: 520;
    letter-spacing: -0.01em;
    overflow-wrap: anywhere;
  }

  .workspace-card__copy > small {
    max-width: 36rem;
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.76rem;
    line-height: 1.55;
  }

  :global(.workspace-card__arrow) {
    align-self: center;
    color: var(--color-clay);
  }

  @media (max-width: 48rem) {
    .workspace-grid {
      grid-template-columns: 1fr;
    }
  }

  @media (max-width: 34rem) {
    .workspace-page {
      padding-inline: 1rem;
    }

    .directory-tools {
      grid-template-columns: 1fr;
      align-items: start;
    }

    .workspace-card {
      padding-inline: 0.9rem;
    }

    :global(.workspace-card__arrow) {
      display: none;
    }
  }
</style>
