<script lang="ts">
  import type {
    FrontendBootstrap,
    FrontendNavigationItem,
  } from "@carlina/sdk";
  import { findAddonApplication } from "@carlina/sdk";
  import { Badge, Dialog, Icon, SearchField } from "@carlina/ui";

  interface CommandItem {
    id: string;
    label: string;
    detail: string;
    href?: string;
    componentId?: string;
    clientRoute: boolean;
    kind: "component" | "destination" | "workspace";
  }

  interface Props {
    open: boolean;
    bootstrap: FrontendBootstrap;
    navigation: FrontendNavigationItem[];
    onclose: () => void;
    onopencomponent: (componentId: string) => void;
    onselect: (href: string, clientRoute: boolean) => void;
  }

  let {
    open,
    bootstrap,
    navigation,
    onclose,
    onopencomponent,
    onselect,
  }: Props = $props();

  let query = $state("");
  let selectedIndex = $state(0);
  const searchId = "iarf-command-search";

  const commands = $derived.by<CommandItem[]>(() => {
    const destinations = navigation.map((item) => ({
      id: `navigation:${item.id}`,
      label: item.label,
      detail: item.section === "primary" ? "Core destination" : "System",
      href: item.href,
      clientRoute: item.client_route,
      kind: "destination" as const,
    }));
    const workspaces = bootstrap.ui_contributions
      .filter(
        (item) =>
          ["active", "degraded"].includes(item.status) &&
          item.routes.length > 0,
      )
      .map((item) => ({
        id: `workspace:${item.id}`,
        label: item.title,
        detail: `${item.section || "Workspace"} · ${item.addon_id}`,
        href: item.routes[0],
        clientRoute: Boolean(
          findAddonApplication(bootstrap, item.routes[0]),
        ),
        kind: "workspace" as const,
      }));
    const availableComponents = new Set(
      bootstrap.ui_components
        .filter(
          (component) =>
            ["active", "degraded"].includes(component.status) &&
            component.compatible &&
            Boolean(component.module_url),
        )
        .map((component) => component.id),
    );
    const componentCommands = new Map<string, CommandItem>();
    for (const contribution of bootstrap.ui_contributions) {
      const componentId = contribution.component_id;
      if (
        !componentId ||
        !availableComponents.has(componentId) ||
        !["active", "degraded"].includes(contribution.status) ||
        !contribution.entrypoint_kinds.some((kind) =>
          ["command_palette", "quick_access"].includes(kind),
        )
      ) {
        continue;
      }
      componentCommands.set(componentId, {
        id: `component:${componentId}`,
        label: contribution.title,
        detail: `${contribution.section || "Component"} · ${contribution.addon_id}`,
        componentId,
        clientRoute: true,
        kind: "component",
      });
    }
    return [...destinations, ...workspaces, ...componentCommands.values()];
  });

  const filteredCommands = $derived.by(() => {
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return commands;
    return commands.filter((item) =>
      `${item.label} ${item.detail}`.toLocaleLowerCase().includes(needle),
    );
  });

  $effect(() => {
    if (!open) return;
    query = "";
    selectedIndex = 0;
    queueMicrotask(() => document.getElementById(searchId)?.focus());
  });

  $effect(() => {
    if (filteredCommands.length === 0) {
      selectedIndex = 0;
    } else if (
      selectedIndex < 0 ||
      selectedIndex >= filteredCommands.length
    ) {
      selectedIndex = Math.max(
        0,
        Math.min(selectedIndex, filteredCommands.length - 1),
      );
    }
  });

  function activate(item: CommandItem) {
    if (item.componentId) {
      onopencomponent(item.componentId);
    } else if (item.href) {
      onselect(item.href, item.clientRoute);
    }
    onclose();
  }

  function handleKeys(event: KeyboardEvent) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      selectedIndex = Math.min(
        Math.max(0, filteredCommands.length - 1),
        selectedIndex + 1,
      );
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      selectedIndex = Math.max(0, selectedIndex - 1);
    } else if (event.key === "Enter") {
      const item = filteredCommands[selectedIndex];
      if (item) {
        event.preventDefault();
        activate(item);
      }
    }
  }
</script>

<Dialog
  {open}
  title="Go anywhere"
  description="Destinations, addon workspaces, and reusable tools—loaded only when you choose them."
  onclose={onclose}
  size="lg"
>
  <div class="command-palette">
    <SearchField
      id={searchId}
      value={query}
      label="Search destinations, workspaces, and tools"
      placeholder="Type a destination, workspace, or tool…"
      oninput={(value) => (query = value)}
      onkeydown={handleKeys}
    />

    <div class="results" role="listbox" aria-label="Navigation results">
      {#if filteredCommands.length}
        {#each filteredCommands as item, index (item.id)}
          <button
            class:selected={index === selectedIndex}
            role="option"
            aria-selected={index === selectedIndex}
            onmouseenter={() => (selectedIndex = index)}
            onclick={() => activate(item)}
          >
            <span class="result-icon">
              <Icon
                name={item.kind === "workspace"
                  ? "workspaces"
                  : item.kind === "component"
                    ? "plus"
                    : "arrow-right"}
                size={17}
              />
            </span>
            <span class="result-copy">
              <strong>{item.label}</strong>
              <small>{item.detail}</small>
            </span>
            <Badge tone={item.clientRoute ? "success" : "neutral"}>
              {item.kind === "component"
                ? "Tool"
                : item.clientRoute
                  ? "Instant"
                  : "Page"}
            </Badge>
          </button>
        {/each}
      {:else}
        <p class="no-results">No destination matches “{query}”.</p>
      {/if}
    </div>
  </div>

  {#snippet footer()}
    <p class="key-guide">
      <span><kbd>↑</kbd><kbd>↓</kbd> move</span>
      <span><kbd>Enter</kbd> open</span>
      <span><kbd>Esc</kbd> close</span>
    </p>
  {/snippet}
</Dialog>

<style>
  .command-palette {
    display: grid;
    gap: 0.9rem;
  }

  .results {
    max-height: min(23rem, 50vh);
    overflow-y: auto;
    border-block: 1px solid var(--color-rule);
  }

  .results button {
    width: 100%;
    min-height: 3.8rem;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.8rem;
    border: 0;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.65rem 0.75rem;
    background: transparent;
    color: inherit;
    cursor: pointer;
    text-align: left;
  }

  .results button:last-child {
    border-bottom: 0;
  }

  .results button:hover,
  .results button.selected {
    background: var(--color-clay-muted);
    box-shadow: inset 3px 0 0 var(--color-clay);
  }

  .result-icon {
    width: 2rem;
    height: 2rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-rule);
    color: var(--color-clay);
  }

  .result-copy {
    min-width: 0;
    display: grid;
    gap: 0.16rem;
  }

  .result-copy strong {
    overflow: hidden;
    font-family: var(--font-editorial);
    font-size: 1rem;
    font-weight: 550;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .result-copy small {
    overflow: hidden;
    color: var(--color-ink-muted);
    font-size: 0.67rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .no-results {
    margin: 0;
    padding: 2.5rem 1rem;
    color: var(--color-ink-muted);
    font-family: var(--font-editorial);
    text-align: center;
  }

  .key-guide {
    width: 100%;
    display: flex;
    justify-content: flex-start;
    gap: 1rem;
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.64rem;
  }

  .key-guide span {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
  }

  kbd {
    border: 1px solid var(--color-rule);
    padding: 0.15rem 0.28rem;
    background: var(--color-surface);
    font-family: inherit;
    font-size: 0.6rem;
  }
</style>
