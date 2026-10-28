<script>
  export let guestMode = false;
  export let currentTab = "memory";
  export let workingMemory = "";
  export let humanGlossary = [];
  export let discoveredGlossary = [];
  export let ragCollectionIds = [];
  export let ragCollections = [];
  export let inlineGlossary = { source: "", target: "", notes: "" };
  export let sidebarWidth = 360;
  export let onTabChange = () => {};
  export let onMemoryChange = () => {};
  export let onMemoryFocus = () => {};
  export let onMemoryBlur = () => {};
  export let onSaveMemory = () => {};
  export let onEditGlossary = () => {};
  export let onDeleteGlossary = () => {};
  export let onInlineFieldChange = () => {};
  export let onAddInlineGlossary = () => {};
  export let onUploadGlossary = () => {};
  export let onKnowledgeSelectionChange = () => {};
  export let onSaveKnowledgeSelection = () => {};

  let glossaryFileInput;

  function setInlineField(field, value) {
    onInlineFieldChange(field, value);
  }
</script>

<aside class="sidebar" style={`width:${sidebarWidth}px`}>
  <div class="tab-row">
    <button class:active={currentTab === "memory"} type="button" on:click={() => onTabChange("memory")}>Memory</button>
    <button class:active={currentTab === "glossary"} type="button" on:click={() => onTabChange("glossary")}>Glossary</button>
    {#if !guestMode}
      <button class:active={currentTab === "knowledge"} type="button" on:click={() => onTabChange("knowledge")}>Knowledge</button>
    {/if}
  </div>

  {#if currentTab === "memory"}
    <section class="tab-panel">
      <p class="hint">Persistent context the translation agent should remember.</p>
      <textarea
        class="memory-box"
        value={workingMemory}
        readonly={guestMode}
        on:focus={onMemoryFocus}
        on:blur={onMemoryBlur}
        on:input={(event) => !guestMode && onMemoryChange(event.currentTarget.value)}
      ></textarea>
      {#if !guestMode}
        <button class="btn primary" type="button" on:click={onSaveMemory}>Save Memory</button>
      {:else}
        <p class="muted">Guest mode: memory editing is disabled.</p>
      {/if}
    </section>
  {/if}

  {#if currentTab === "glossary"}
    <section class="tab-panel">
      <div class="tab-actions">
        <p class="hint">Source and target terminology used throughout this project.</p>
        {#if !guestMode}
          <button class="btn" type="button" on:click={() => glossaryFileInput?.click()}>Import CSV</button>
          <input bind:this={glossaryFileInput} type="file" accept=".csv" class="hidden" on:change={onUploadGlossary} />
        {/if}
      </div>

      <h3>Human Terms</h3>
      {#if !humanGlossary.length}
        <p class="muted">No human-defined terms yet.</p>
      {:else}
        <div class="glossary-list">
          {#each humanGlossary as term (term.id)}
            <article class={`glossary-item ${term.needs_review ? "flagged" : ""}`}>
              <div>
                <strong>{term.source_term}</strong> to {term.target_term}
                {#if term.context_notes}<p>{term.context_notes}</p>{/if}
              </div>
              {#if !guestMode}
                <div class="mini-actions">
                  <button class="icon-btn" type="button" on:click={() => onEditGlossary(term)} aria-label="Edit term" title="Edit term">
                    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75-10.06 10.06zM20.71 7.21a1 1 0 000-1.41L18.2 3.29a1 1 0 00-1.41 0l-1.84 1.84 3.75 3.75 2.01-2.01z"></path></svg>
                    <span class="sr-only">Edit</span>
                  </button>
                  <button class="icon-btn danger" type="button" on:click={() => onDeleteGlossary(term.id)} aria-label="Delete term" title="Delete term">
                    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 3h6l1 2h4v2H4V5h4l1-2zm1 6h2v9h-2V9zm4 0h2v9h-2V9zM7 9h2v9H7V9z"></path></svg>
                    <span class="sr-only">Delete</span>
                  </button>
                </div>
              {/if}
            </article>
          {/each}
        </div>
      {/if}

      <h3>AI Discovered</h3>
      {#if !discoveredGlossary.length}
        <p class="muted">No AI-discovered terms yet.</p>
      {:else}
        <div class="glossary-list">
          {#each discoveredGlossary as term (term.id)}
            <article class={`glossary-item ${term.needs_review ? "flagged" : ""}`}>
              <div>
                <strong>{term.source_term}</strong> to {term.target_term}
                {#if term.context_notes}<p>{term.context_notes}</p>{/if}
              </div>
              {#if !guestMode}
                <div class="mini-actions">
                  <button class="icon-btn" type="button" on:click={() => onEditGlossary(term)} aria-label="Edit term" title="Edit term">
                    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75-10.06 10.06zM20.71 7.21a1 1 0 000-1.41L18.2 3.29a1 1 0 00-1.41 0l-1.84 1.84 3.75 3.75 2.01-2.01z"></path></svg>
                    <span class="sr-only">Edit</span>
                  </button>
                  <button class="icon-btn danger" type="button" on:click={() => onDeleteGlossary(term.id)} aria-label="Delete term" title="Delete term">
                    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 3h6l1 2h4v2H4V5h4l1-2zm1 6h2v9h-2V9zm4 0h2v9h-2V9zM7 9h2v9H7V9z"></path></svg>
                    <span class="sr-only">Delete</span>
                  </button>
                </div>
              {/if}
            </article>
          {/each}
        </div>
      {/if}

      {#if !guestMode}
        <div class="add-term">
          <h3>Add Term</h3>
          <input placeholder="Source term" value={inlineGlossary.source || ""} on:input={(event) => setInlineField("source", event.currentTarget.value)} />
          <input placeholder="Target term" value={inlineGlossary.target || ""} on:input={(event) => setInlineField("target", event.currentTarget.value)} />
          <textarea placeholder="Notes (optional)" value={inlineGlossary.notes || ""} on:input={(event) => setInlineField("notes", event.currentTarget.value)}></textarea>
          <button class="btn primary" type="button" on:click={onAddInlineGlossary}>Add Term</button>
        </div>
      {/if}
    </section>
  {/if}

  {#if !guestMode && currentTab === "knowledge"}
    <section class="tab-panel">
      <p class="hint">Select reusable knowledge collections available to translation agents.</p>
      {#if !ragCollections.length}
        <p class="muted">No vector collections found in Knowledge Manager.</p>
      {:else}
        <div class="checklist collection-list">
          {#each ragCollections as collection (collection.id)}
            <label class="check-item">
              <input
                class="check-input"
                type="checkbox"
                value={String(collection.id)}
                checked={ragCollectionIds.includes(String(collection.id))}
                on:change={(event) => onKnowledgeSelectionChange(String(collection.id), event.currentTarget.checked)}
              />
              <span class="check-copy"><span class="check-title">{collection.name}</span><span class="check-meta">Knowledge collection</span></span>
            </label>
          {/each}
        </div>
        <button class="btn primary" type="button" on:click={onSaveKnowledgeSelection}>Save Selection</button>
      {/if}
    </section>
  {/if}
</aside>

<style>
  .sidebar { background:color-mix(in srgb,var(--color-paper-muted) 74%,var(--color-surface)); border-left:1px solid var(--color-rule); display:flex; flex-direction:column; min-width:280px; max-width:50vw; overflow:hidden; }
  .tab-row { display:flex; border-bottom:1px solid var(--color-rule); background:color-mix(in srgb,var(--color-paper-muted) 90%,var(--color-surface)); }
  .tab-row button { flex:1; border:none; border-right:1px solid var(--color-rule); background:transparent; padding:.64rem .46rem; cursor:pointer; font-weight:600; color:var(--color-ink-muted); transition:background-color 120ms ease,color 120ms ease; }
  .tab-row button:last-child { border-right:none; }
  .tab-row button.active { background:var(--color-surface); color:var(--color-active); }
  .tab-panel { min-height:0; overflow:auto; display:flex; flex-direction:column; gap:.75rem; padding:.85rem; }
  .tab-actions { display:flex; justify-content:space-between; gap:.5rem; align-items:center; }
  .hint,.muted { margin:0; font-size:.84rem; color:var(--color-ink-muted); }
  .muted { color:color-mix(in srgb,var(--color-ink-muted) 85%,#7c7c84); }
  h3 { margin:.2rem 0 0; font-size:.84rem; text-transform:uppercase; letter-spacing:.08em; color:var(--color-ink-muted); }
  .memory-box { min-height:220px; resize:vertical; }
  .glossary-list { display:flex; flex-direction:column; gap:.5rem; }
  .glossary-item { border:1px solid var(--color-rule); background:var(--color-surface); border-radius:.6rem; padding:.58rem .62rem; display:flex; justify-content:space-between; align-items:flex-start; gap:.55rem; }
  .glossary-item.flagged { border-color:color-mix(in srgb,var(--color-warning) 35%,var(--color-rule)); background:var(--color-warning-muted); }
  .glossary-item p { margin:.35rem 0 0; font-size:.8rem; color:var(--color-ink-muted); }
  .mini-actions { display:flex; gap:.3rem; flex-shrink:0; align-items:flex-start; }
  .icon-btn { border:1px solid var(--color-rule); background:var(--color-surface); color:var(--color-ink-soft); width:1.9rem; height:1.9rem; display:inline-flex; align-items:center; justify-content:center; border-radius:.45rem; cursor:pointer; padding:0; }
  .icon-btn svg { width:.95rem; height:.95rem; fill:currentColor; }
  .icon-btn:hover { border-color:color-mix(in srgb,var(--color-active) 38%,var(--color-rule)); color:var(--color-active); background:var(--color-active-muted); }
  .icon-btn.danger { color:var(--color-danger); border-color:color-mix(in srgb,var(--color-danger) 32%,var(--color-rule)); background:var(--color-danger-muted); }
  .icon-btn.danger:hover { background:var(--color-danger); color:var(--color-surface); }
  .add-term { border-top:1px solid var(--color-rule); padding-top:.75rem; display:flex; flex-direction:column; gap:.5rem; }
  .checklist { display:flex; flex-direction:column; gap:.5rem; }
  .check-item { display:grid; grid-template-columns:auto minmax(0,1fr); align-items:start; gap:.62rem; padding:.55rem .62rem; border:1px solid var(--color-rule); border-radius:.55rem; background:var(--color-surface); cursor:pointer; transition:border-color 120ms ease,background-color 120ms ease; }
  .check-item:hover { border-color:color-mix(in srgb,var(--color-active) 26%,var(--color-rule)); background:color-mix(in srgb,var(--color-active-muted) 52%,var(--color-surface)); }
  .check-item:focus-within { outline:2px solid color-mix(in srgb,var(--color-active) 22%,transparent); outline-offset:1px; }
  .check-input { width:1rem; height:1rem; margin:.13rem 0 0; accent-color:var(--color-active); }
  .check-copy { min-width:0; display:flex; flex-direction:column; gap:.12rem; }
  .check-title { font-size:.88rem; color:var(--color-ink-soft); line-height:1.35; }
  .check-meta { font-size:.75rem; color:var(--color-ink-muted); }
  .collection-list { margin-bottom:.2rem; }
  input:not([type="checkbox"]):not([type="radio"]), textarea { width:100%; box-sizing:border-box; border:1px solid var(--color-rule); background:var(--color-surface); color:var(--color-ink); padding:.56rem .62rem; font:inherit; border-radius:.5rem; }
  input:not([type="checkbox"]):not([type="radio"]):focus, textarea:focus { outline:2px solid color-mix(in srgb,var(--color-active) 20%,transparent); border-color:color-mix(in srgb,var(--color-active) 38%,var(--color-rule)); outline-offset:1px; }
  .btn { border:1px solid var(--color-rule); background:var(--color-surface); color:var(--color-ink); padding:.46rem .76rem; cursor:pointer; font-size:.86rem; border-radius:.48rem; }
  .btn:disabled { opacity:.55; cursor:not-allowed; }
  .btn.primary { border-color:var(--color-active); background:var(--color-active); color:var(--color-surface); }
  .hidden { display:none; }
  .sr-only { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }
  @media (max-width:1100px) { .sidebar { width:100% !important; max-width:none; border-left:none; border-top:1px solid var(--color-rule); min-height:320px; } }
  @media (max-width:760px) { .tab-panel { padding:.7rem; } }
</style>
