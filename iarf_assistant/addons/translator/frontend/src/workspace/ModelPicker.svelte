<script>
  export let models = [];
  export let value = "";
  export let disabled = false;
  export let onChange = () => {};

  let root;
  let searchInput;
  let open = false;
  let query = "";
  let activeIndex = 0;

  $: groups = providerGroupedModels(models, query);
  $: visibleModels = groups.flatMap((group) => group.models);
  $: selectedModel = models.find((model) => model.id === value) || null;
  $: activeOptionId = visibleModels.length
    ? `translation-model-option-${Math.min(activeIndex, visibleModels.length - 1)}`
    : undefined;

  function providerGroupedModels(rows, needle = "") {
    const normalizedQuery = needle.trim().toLocaleLowerCase();
    const grouped = new Map();
    for (const model of rows) {
      const provider = String(model.provider || "Other providers");
      const searchText = `${model.name} ${provider} ${model.id}`.toLocaleLowerCase();
      if (normalizedQuery && !searchText.includes(normalizedQuery)) continue;
      const entries = grouped.get(provider) || [];
      entries.push(model);
      grouped.set(provider, entries);
    }
    const collator = new Intl.Collator(undefined, { sensitivity: "base", numeric: true });
    return [...grouped.entries()]
      .sort(([left], [right]) => collator.compare(left, right))
      .map(([provider, entries]) => ({
        provider,
        models: entries.sort((left, right) => collator.compare(left.name, right.name) || collator.compare(left.id, right.id))
      }));
  }

  function openMenu() {
    if (disabled) return;
    open = true;
    query = "";
    activeIndex = Math.max(0, models.findIndex((model) => model.id === value));
    queueMicrotask(() => searchInput?.focus());
  }

  function closeMenu(restoreFocus = false) {
    open = false;
    query = "";
    if (restoreFocus) queueMicrotask(() => root?.querySelector("button")?.focus());
  }

  function choose(model) {
    onChange(model.id);
    closeMenu(true);
  }

  function moveActive(delta) {
    if (!visibleModels.length) return;
    activeIndex = (activeIndex + delta + visibleModels.length) % visibleModels.length;
  }

  function handleSearchKeydown(event) {
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
      activeIndex = Math.max(0, visibleModels.length - 1);
    } else if (event.key === "Enter" && visibleModels[activeIndex]) {
      event.preventDefault();
      choose(visibleModels[activeIndex]);
    } else if (event.key === "Escape") {
      event.preventDefault();
      closeMenu(true);
    }
  }

  function closeWhenFocusLeaves() {
    requestAnimationFrame(() => {
      if (open && root && !root.contains(document.activeElement)) closeMenu();
    });
  }

  function dismissOutside(event) {
    if (open && root && !root.contains(event.target)) closeMenu();
  }

  function dismissEscape(event) {
    if (open && event.key === "Escape") closeMenu(true);
  }
</script>

<svelte:window on:pointerdown={dismissOutside} on:keydown={dismissEscape} />

<div class="model-picker" bind:this={root} on:focusout={closeWhenFocusLeaves}>
  <button
    type="button"
    class="model-trigger"
    role="combobox"
    aria-label="Model for next translation"
    aria-controls="translation-model-options"
    aria-expanded={open}
    aria-haspopup="listbox"
    {disabled}
    on:click={() => (open ? closeMenu() : openMenu())}
  >
    <span>
      <strong>{selectedModel?.name || "Choose model"}</strong>
      <small>{selectedModel?.provider || "Configured chat models"}</small>
    </span>
    <svg viewBox="0 0 20 20" aria-hidden="true"><path d="m6.5 8 3.5 3.5L13.5 8" /></svg>
  </button>

  {#if open}
    <div class="model-panel">
      <label class="model-search">
        <svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="8.5" cy="8.5" r="4.5" /><path d="m12 12 4 4" /></svg>
        <span class="sr-only">Search translation models</span>
        <input
          bind:this={searchInput}
          bind:value={query}
          type="search"
          role="combobox"
          aria-label="Search translation models"
          aria-controls="translation-model-options"
          aria-activedescendant={activeOptionId}
          aria-expanded="true"
          placeholder="Search models or providers"
          on:keydown={handleSearchKeydown}
        />
      </label>
      <div id="translation-model-options" class="model-options" role="listbox" aria-label="Translation models">
        {#each groups as group, groupIndex (group.provider)}
          <section role="group" aria-labelledby={`translation-model-provider-${groupIndex}`}>
            <p id={`translation-model-provider-${groupIndex}`}>{group.provider}</p>
            {#each group.models as model}
              {@const index = visibleModels.findIndex((item) => item.id === model.id)}
              <button
                id={`translation-model-option-${index}`}
                type="button"
                role="option"
                class:active={activeIndex === index}
                class:chosen={value === model.id}
                aria-selected={value === model.id}
                on:mouseenter={() => (activeIndex = index)}
                on:click={() => choose(model)}
              >
                <span><strong>{model.name}</strong><small>{model.id}</small></span>
                {#if value === model.id}<span class="check" aria-hidden="true">✓</span>{/if}
              </button>
            {/each}
          </section>
        {:else}
          <p class="empty">No models match “{query}”.</p>
        {/each}
      </div>
    </div>
  {/if}
</div>

<style>
  .model-picker { position:relative; min-width:0; }
  .model-trigger, .model-options button { width:100%; min-width:0; display:flex; align-items:center; justify-content:space-between; gap:.6rem; border:1px solid var(--color-rule,#d0c8bc); background:var(--color-surface,#faf8f3); color:var(--color-ink-soft,#4d4842); cursor:pointer; text-align:left; }
  .model-trigger { min-height:2.05rem; padding:.28rem .48rem; }
  .model-trigger > span, .model-options button > span:first-child { min-width:0; display:grid; gap:.02rem; }
  strong, small { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  strong { font-size:.7rem; font-weight:650; }
  small { color:var(--color-ink-muted,#675f56); font-size:.57rem; }
  .model-trigger:hover, .model-trigger:focus-visible { border-color:var(--color-rule-strong,#968b7d); outline:0; }
  .model-trigger:disabled { cursor:not-allowed; opacity:.6; }
  .model-trigger svg { width:1rem; height:1rem; flex:0 0 auto; fill:none; stroke:currentColor; stroke-width:1.4; stroke-linecap:round; stroke-linejoin:round; transition:transform 140ms ease; }
  .model-trigger[aria-expanded="true"] svg { transform:rotate(180deg); }
  .model-panel { position:absolute; z-index:30; top:calc(100% + .35rem); right:0; width:min(24rem,calc(100vw - 2rem)); display:grid; gap:.4rem; border:1px solid var(--color-rule-strong,#968b7d); padding:.5rem; background:var(--color-surface,#faf8f3); box-shadow:0 .8rem 2rem color-mix(in srgb,var(--color-ink,#292621) 16%,transparent); }
  .model-search { min-height:2rem; display:flex; align-items:center; gap:.4rem; border:1px solid var(--color-rule,#d0c8bc); padding:0 .5rem; color:var(--color-ink-muted,#675f56); }
  .model-search:focus-within { border-color:var(--color-rule-strong,#968b7d); box-shadow:0 0 0 2px color-mix(in srgb,var(--color-clay,#7c6053) 15%,transparent); }
  .model-search svg { width:.9rem; height:.9rem; fill:none; stroke:currentColor; stroke-width:1.4; }
  .model-search input { width:100%; min-width:0; border:0; outline:0; background:transparent; color:var(--color-ink,#292621); font:inherit; font-size:.7rem; }
  .model-options { max-height:min(19rem,calc(100vh - 10rem)); overflow-y:auto; overscroll-behavior:contain; }
  .model-options section + section { margin-top:.35rem; }
  .model-options section > p { margin:0; padding:.42rem .4rem .2rem; color:var(--color-clay,#7c6053); font-size:.52rem; font-weight:750; letter-spacing:.11em; text-transform:uppercase; }
  .model-options button { min-height:2.4rem; border-color:transparent; padding:.34rem .45rem; }
  .model-options button:hover, .model-options button.active { background:var(--color-surface-muted,#e9e4db); }
  .model-options button.chosen { color:var(--color-clay,#7c6053); }
  .check { flex:0 0 auto; color:var(--color-clay,#7c6053); }
  .empty { margin:0; padding:.75rem .45rem; color:var(--color-ink-muted,#675f56); font-size:.68rem; }
  .sr-only { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; }
</style>
