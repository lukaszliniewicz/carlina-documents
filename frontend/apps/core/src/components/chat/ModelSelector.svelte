<script lang="ts">
  import type { FrontendChatModel } from "@carlina/sdk";
  import { Icon } from "@carlina/ui";

  import { providerGroupedModels } from "../../lib/model-selector.ts";

  interface Props {
    models: FrontendChatModel[];
    value: string;
    disabled?: boolean;
    onchange: (value: string) => void;
    onrouting?: () => void;
  }

  let {
    models,
    value,
    disabled = false,
    onchange,
    onrouting = undefined,
  }: Props = $props();

  let root: HTMLDivElement;
  let trigger: HTMLButtonElement;
  let searchInput = $state<HTMLInputElement>();
  let open = $state(false);
  let query = $state("");
  let activeIndex = $state(0);
  let panelStyle = $state("");

  const groups = $derived.by(() => providerGroupedModels(models, query));
  const visibleModels = $derived.by(() => groups.flatMap((group) => group.models));
  const selectedModel = $derived(
    models.find((model) => model.id === value) ?? null,
  );
  const activeOptionId = $derived(
    activeIndex === 0
      ? "chat-model-option-automatic"
      : `chat-model-option-${activeIndex - 1}`,
  );

  $effect(() => {
    if (!open) return;
    const selectedIndex = visibleModels.findIndex((model) => model.id === value);
    const nextIndex = selectedIndex >= 0 ? selectedIndex + 1 : 0;
    activeIndex = Math.min(nextIndex, visibleModels.length);
  });

  $effect(() => {
    if (!open) return;
    const reposition = () => positionMenu();
    const dismissOutside = (event: PointerEvent) => {
      if (root?.contains(event.target as Node)) return;
      closeMenu();
    };
    const dismissEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeMenu(true);
    };
    reposition();
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    window.addEventListener("pointerdown", dismissOutside, true);
    window.addEventListener("keydown", dismissEscape, true);
    return () => {
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
      window.removeEventListener("pointerdown", dismissOutside, true);
      window.removeEventListener("keydown", dismissEscape, true);
    };
  });

  function openMenu(): void {
    if (disabled) return;
    open = true;
    query = "";
    const selectedIndex = models.findIndex((model) => model.id === value);
    activeIndex = selectedIndex >= 0 ? selectedIndex + 1 : 0;
    queueMicrotask(() => {
      positionMenu();
      searchInput?.focus();
    });
  }

  function closeMenu(restoreFocus = false): void {
    open = false;
    query = "";
    if (restoreFocus) queueMicrotask(() => root?.querySelector("button")?.focus());
  }

  function positionMenu(): void {
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    const gutter = 8;
    const width = Math.min(Math.max(rect.width, 260), window.innerWidth - gutter * 2);
    const left = Math.max(gutter, Math.min(rect.right - width, window.innerWidth - width - gutter));
    const availableBelow = window.innerHeight - rect.bottom - gutter;
    const availableAbove = rect.top - gutter;
    const maxHeight = Math.max(160, Math.min(360, Math.max(availableBelow, availableAbove)));
    const placeBelow = availableBelow >= Math.min(260, availableAbove);
    const top = placeBelow
      ? Math.min(rect.bottom + 6, window.innerHeight - maxHeight - gutter)
      : Math.max(gutter, rect.top - maxHeight - 6);
    panelStyle = `left:${left}px;top:${top}px;width:${width}px;max-height:${maxHeight}px;`;
  }

  function choose(nextValue: string): void {
    onchange(nextValue);
    closeMenu(true);
  }

  function moveActive(delta: number): void {
    const count = visibleModels.length + 1;
    activeIndex = (activeIndex + delta + count) % count;
  }

  function chooseActive(): void {
    const selected = activeIndex === 0 ? "" : visibleModels[activeIndex - 1]?.id;
    if (selected !== undefined) choose(selected);
  }

  function handleTriggerKeydown(event: KeyboardEvent): void {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      openMenu();
      if (event.key === "ArrowUp") activeIndex = visibleModels.length;
    } else if (event.key === "Escape") {
      closeMenu();
    }
  }

  function handleSearchKeydown(event: KeyboardEvent): void {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      moveActive(1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      moveActive(-1);
    } else if (event.key === "Home") {
      event.preventDefault();
      activeIndex = 0;
    } else if (event.key === "End") {
      event.preventDefault();
      activeIndex = visibleModels.length;
    } else if (event.key === "Enter") {
      event.preventDefault();
      chooseActive();
    } else if (event.key === "Escape") {
      event.preventDefault();
      closeMenu(true);
    }
  }

  function closeWhenFocusLeaves(): void {
    requestAnimationFrame(() => {
      if (open && root && !root.contains(document.activeElement)) closeMenu();
    });
  }
</script>

<div class="model-selector" bind:this={root} onfocusout={closeWhenFocusLeaves}>
  <button
    bind:this={trigger}
    class="model-selector__trigger"
    type="button"
    role="combobox"
    aria-label={`Language model: ${selectedModel?.name ?? "Automatic selection"}`}
    aria-controls="chat-model-options"
    aria-expanded={open}
    aria-haspopup="listbox"
    disabled={disabled}
    onkeydown={handleTriggerKeydown}
    onclick={() => (open ? closeMenu() : openMenu())}
  >
    <span>
      <strong>{selectedModel?.name ?? "Automatic selection"}</strong>
      <small>{selectedModel?.provider ?? "Use the administrator's routing default"}</small>
    </span>
    <Icon name="chevron-right" size={15} />
  </button>

  {#if open}
    <div class="model-selector__panel" style={panelStyle}>
      <label class="model-selector__search">
        <Icon name="search" size={14} />
        <span class="sr-only">Search language models</span>
        <input
          bind:this={searchInput}
          bind:value={query}
          type="search"
          role="combobox"
          aria-controls="chat-model-options"
          aria-activedescendant={activeOptionId}
          aria-expanded="true"
          placeholder="Search models or providers"
          onkeydown={handleSearchKeydown}
        />
      </label>
      <div id="chat-model-options" class="model-selector__options" role="listbox" aria-label="Language models">
        <button
          id="chat-model-option-automatic"
          class:selected={activeIndex === 0}
          class:chosen={!value}
          type="button"
          role="option"
          aria-selected={!value}
          onmouseenter={() => (activeIndex = 0)}
          onclick={() => choose("")}
        >
          <span>
            <strong>Automatic selection</strong>
            <small>Use the administrator's routing default</small>
          </span>
          {#if !value}<Icon name="check" size={14} />{/if}
        </button>
        {#each groups as group, groupIndex (group.provider)}
          <section role="group" aria-labelledby={`chat-model-provider-${groupIndex}`}>
            <p id={`chat-model-provider-${groupIndex}`}>{group.provider}</p>
            {#each group.models as model}
              {@const index = visibleModels.findIndex((item) => item.id === model.id) + 1}
              <button
                id={`chat-model-option-${index - 1}`}
                class:selected={activeIndex === index}
                class:chosen={value === model.id}
                type="button"
                role="option"
                aria-selected={value === model.id}
                onmouseenter={() => (activeIndex = index)}
                onclick={() => choose(model.id)}
              >
                <span>
                  <strong>{model.name}</strong>
                  <small>{model.id}</small>
                </span>
                {#if value === model.id}<Icon name="check" size={14} />{/if}
              </button>
            {/each}
          </section>
        {:else}
          <p class="model-selector__empty">No models match “{query}”.</p>
        {/each}
      </div>
      <button
        class="model-selector__routing"
        type="button"
        onclick={() => {
          closeMenu();
          onrouting?.();
        }}
      >
        Manage app routing defaults
      </button>
    </div>
  {/if}
</div>

<style>
  .model-selector {
    position: relative;
    min-width: 0;
  }

  .model-selector__trigger,
  .model-selector__options button {
    width: 100%;
    min-width: 0;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.6rem;
    border: 1px solid var(--color-rule);
    background: var(--color-surface);
    color: var(--color-ink);
    cursor: pointer;
    text-align: left;
  }

  .model-selector__trigger {
    min-height: 2.5rem;
    padding: 0.35rem 0.6rem;
  }

  .model-selector__trigger > span,
  .model-selector__options button > span {
    min-width: 0;
    display: grid;
    gap: 0.08rem;
  }

  .model-selector strong,
  .model-selector small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .model-selector strong {
    font-size: 0.68rem;
    font-weight: 650;
  }

  .model-selector small {
    color: var(--color-ink-muted);
    font-size: 0.56rem;
  }

  .model-selector__trigger:hover,
  .model-selector__trigger:focus-visible {
    border-color: var(--color-focus);
    outline: 0;
  }

  .model-selector__trigger > :global(svg) {
    transform: rotate(90deg);
    transition: transform 150ms ease;
  }

  .model-selector__trigger[aria-expanded="true"] > :global(svg) {
    transform: rotate(-90deg);
  }

  .model-selector__panel {
    position: fixed;
    z-index: 1000;
    display: grid;
    gap: 0.45rem;
    border: 1px solid var(--color-rule-strong);
    padding: 0.5rem;
    background: var(--color-surface);
    box-shadow: 0 0.8rem 2rem color-mix(in srgb, var(--color-ink) 16%, transparent);
    overflow: hidden;
  }

  .model-selector__search {
    min-height: 2rem;
    display: flex;
    align-items: center;
    gap: 0.4rem;
    border: 1px solid var(--color-rule);
    padding: 0 0.5rem;
    color: var(--color-ink-muted);
  }

  .model-selector__search:focus-within {
    border-color: var(--color-focus);
    box-shadow: 0 0 0 2px var(--color-focus-muted);
  }

  .model-selector__search input {
    width: 100%;
    min-width: 0;
    border: 0;
    outline: 0;
    background: transparent;
    color: var(--color-ink);
    font-size: 0.64rem;
  }

  .model-selector__options {
    max-height: min(17rem, calc(100vh - 9rem));
    overflow-y: auto;
    overscroll-behavior: contain;
  }

  .model-selector__options section + section {
    margin-top: 0.45rem;
  }

  .model-selector__options section > p {
    margin: 0;
    padding: 0.45rem 0.4rem 0.25rem;
    color: var(--color-clay);
    font-size: 0.5rem;
    font-weight: 750;
    letter-spacing: 0.11em;
    text-transform: uppercase;
  }

  .model-selector__options button {
    min-height: 2.45rem;
    border-color: transparent;
    padding: 0.35rem 0.45rem;
  }

  .model-selector__options button:hover,
  .model-selector__options button.selected {
    background: var(--color-clay-muted);
  }

  .model-selector__options button.chosen {
    color: var(--color-clay);
  }

  .model-selector__routing {
    justify-self: start;
    border: 0;
    padding: 0.1rem 0.35rem;
    background: transparent;
    color: var(--color-clay);
    cursor: pointer;
    font-size: 0.61rem;
    text-decoration: underline;
  }

  .model-selector__empty {
    margin: 0;
    padding: 0.75rem 0.45rem;
    color: var(--color-ink-muted);
    font-size: 0.62rem;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }
</style>
