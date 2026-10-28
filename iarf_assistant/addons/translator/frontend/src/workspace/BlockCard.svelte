<script>
  import BlockConflictPanel from "./BlockConflictPanel.svelte";
  import TargetBlockCandidateEditor from "./TargetBlockCandidateEditor.svelte";
  import BlockNotesEditor from "./BlockNotesEditor.svelte";
  import AlignmentGroupReview from "./AlignmentGroupReview.svelte";
  import { decorateSourceContent } from "./compact-editor.js";
  import { tick } from "svelte";

  export let block;
  export let readOnly = false;
  export let accessRole = "owner";
  export let reviewMode = "block";
  export let draft = null;
  export let saving = false;
  export let conflict = null;
  export let canMergePrevious = false;
  export let canMergeNext = false;
  export let structuralDisabled = false;
  export let structuralBusy = false;
  export let selectionMode = false;
  export let selected = false;
  export let selectionDisabled = false;
  export let onCandidateChange = () => {};
  export let onSave = () => {};
  export let onSentenceEdit = async () => ({ ok: false });
  export let onSentenceEditingChange = () => {};
  export let onReloadConflict = () => {};
  export let onCorrect = () => {};
  export let onSelect = () => {};
  export let noteBusy = false;
  export let noteError = "";
  export let onCreateNote = () => {};
  export let onReviseNote = () => {};
  export let onTransitionNote = () => {};
  export let onReview = () => {};
  export let onCoverage = () => {};

  const MAX_NOTE_ANCHOR_LENGTH = 160;
  const NOTE_TOOLBAR_HALF_WIDTH = 24;

  let menuOpen = false;
  let menuTriggerElement = null;
  let notesOpen = false;
  let reviewOpen = false;
  let reviewTriggerElement = null;
  let reviewPanelElement = null;
  let sourcePaneElement = null;
  let sourceSelectionRange = null;
  let sourceSelectionRoot = null;
  let sourceToolbarVisible = false;
  let sourceToolbarLeft = 0;
  let sourceToolbarTop = 0;
  let noteCreateSequence = 0;
  let noteCreateRequest = null;
  let continuousEditorOpen = false;
  let sentenceEditing = false;
  let shownRevisionKey = "";

  function closeOnOutsideClick(node) {
    function handle(event) {
      if (menuOpen && !node.contains(event.target)) menuOpen = false;
    }
    document.addEventListener("pointerdown", handle, true);
    return { destroy: () => document.removeEventListener("pointerdown", handle, true) };
  }

  function handleMenuKeydown(event) {
    if (event.key === "Escape") {
      menuOpen = false;
      menuTriggerElement?.focus();
    }
  }

  function chooseCorrection(action) {
    menuOpen = false;
    onCorrect(block, action);
  }

  function reviewDisclosure(node) {
    function outside(event) {
      if (!node.contains(event.target)) reviewOpen = false;
    }
    function keydown(event) {
      if (event.key === "Escape" && reviewOpen) {
        event.preventDefault();
        reviewOpen = false;
        reviewTriggerElement?.focus();
      }
    }
    document.addEventListener("pointerdown", outside, true);
    node.addEventListener("keydown", keydown);
    return { destroy() {
      document.removeEventListener("pointerdown", outside, true);
      node.removeEventListener("keydown", keydown);
    } };
  }

  async function toggleReview() {
    reviewOpen = !reviewOpen;
    menuOpen = false;
    if (reviewOpen) {
      await tick();
      reviewPanelElement?.querySelector("button:not(:disabled)")?.focus();
    }
  }

  function chooseReview(decision) {
    reviewOpen = false;
    reviewTriggerElement?.focus();
    onReview(decision);
  }

  function selectForRetranslation(event) {
    if (selectionDisabled) return;
    onSelect(block, event);
  }

  function nodeElement(node) {
    if (!node) return null;
    return node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
  }

  function hideSourceToolbar() {
    sourceToolbarVisible = false;
    sourceSelectionRange = null;
    sourceSelectionRoot = null;
  }

  function sourceSelectionActions(node) {
    function handleOutside(event) {
      if (sourceToolbarVisible && !node.contains(event.target)) hideSourceToolbar();
    }
    function handleEscape(event) {
      if (event.key === "Escape") hideSourceToolbar();
    }
    node.addEventListener("mouseup", captureSourceSelection);
    node.addEventListener("touchend", captureSourceSelection);
    node.addEventListener("keyup", captureSourceSelection);
    node.addEventListener("keydown", handleEscape);
    document.addEventListener("pointerdown", handleOutside, true);
    return {
      destroy: () => {
        node.removeEventListener("mouseup", captureSourceSelection);
        node.removeEventListener("touchend", captureSourceSelection);
        node.removeEventListener("keyup", captureSourceSelection);
        node.removeEventListener("keydown", handleEscape);
        document.removeEventListener("pointerdown", handleOutside, true);
      }
    };
  }

  function captureSourceSelection() {
    if (!canReview || !sourcePaneElement) {
      hideSourceToolbar();
      return;
    }
    const selection = window.getSelection();
    if (!selection?.rangeCount || selection.isCollapsed) {
      hideSourceToolbar();
      return;
    }
    const range = selection.getRangeAt(0);
    const element = nodeElement(range.commonAncestorContainer);
    const root = element?.closest?.('[data-note-anchor="source"]');
    if (!root || !sourcePaneElement.contains(root)) {
      hideSourceToolbar();
      return;
    }
    const rect = typeof range.getBoundingClientRect === "function"
      ? range.getBoundingClientRect()
      : root.getBoundingClientRect();
    const hostRect = sourcePaneElement.getBoundingClientRect();
    sourceSelectionRange = range.cloneRange();
    sourceSelectionRoot = root;
    sourceToolbarLeft = Math.max(
      NOTE_TOOLBAR_HALF_WIDTH,
      Math.min(hostRect.width - NOTE_TOOLBAR_HALF_WIDTH, rect.left - hostRect.left + rect.width / 2)
    );
    sourceToolbarTop = Math.max(2, rect.top - hostRect.top - 8);
    sourceToolbarVisible = true;
  }

  function occurrenceIndex(root, range, anchorText) {
    const prefix = document.createRange();
    prefix.selectNodeContents(root);
    prefix.setEnd(range.startContainer, range.startOffset);
    const before = prefix.toString();
    let count = 0;
    let offset = 0;
    while (offset <= before.length - anchorText.length) {
      const found = before.indexOf(anchorText, offset);
      if (found < 0) break;
      count += 1;
      offset = found + 1;
    }
    return count;
  }

  function prepareAnchoredNote(anchor) {
    if (!canReview || !anchor?.text?.trim() || anchor.text.length > MAX_NOTE_ANCHOR_LENGTH) return;
    noteCreateSequence += 1;
    noteCreateRequest = { key: `${block?.id || "block"}:${noteCreateSequence}`, anchor };
  }

  function addSourceNote() {
    if (!sourceSelectionRange || !sourceSelectionRoot) return;
    const text = sourceSelectionRange.toString();
    prepareAnchoredNote({
      kind: "source",
      text,
      occurrence_index: occurrenceIndex(sourceSelectionRoot, sourceSelectionRange, text)
    });
    hideSourceToolbar();
  }

  const roleLabels = {
    heading: "Heading",
    paragraph: "Paragraph",
    direct_prose: "Prose",
    list_item: "List item",
    blockquote: "Quotation",
    definition_term: "Definition term",
    definition_body: "Definition",
    table_caption: "Table caption",
    table_header: "Table heading",
    table_cell: "Table cell",
    figure_caption: "Figure caption",
    note_body: "Note",
    source_toc_entry: "Contents entry",
    document_title: "Document title"
  };

  function accessibilityRoleLabel(fields) {
    const names = new Set(fields.map((field) => String(field?.name || "").toLowerCase()));
    if (names.size === 1 && names.has("alt")) return "Image description";
    if (names.size === 1 && names.has("title")) return "Title";
    return "Accessible label";
  }

  $: target = block?.target || null;
  $: revisionKey = `${block?.id || "block"}:${target?.active_version || 0}`;
  $: if (revisionKey !== shownRevisionKey) {
    shownRevisionKey = revisionKey;
    continuousEditorOpen = false;
  }
  $: candidate = draft || target?.editor || null;
  $: source = block?.source || {};
  $: sourceEditor = source.editor || null;
  $: sourceFields = Array.isArray(sourceEditor?.fields) ? sourceEditor.fields : [];
  $: compactAltField = !sourceEditor?.content
    && sourceFields.length === 1
    && String(sourceFields[0]?.name || "").toLowerCase() === "alt";
  $: validationFindings = Array.isArray(target?.validation?.findings) ? target.validation.findings : [];
  $: translatorNotes = Array.isArray(target?.translator_notes) ? target.translator_notes : [];
  $: readerNotes = Array.isArray(block?.reader_notes) ? block.reader_notes : [];
  $: displayedNotes = readerNotes.length ? readerNotes : translatorNotes.map((note) => ({
    ...note,
    version: 1,
    state: "active",
    anchor: { kind: "target", text: note.anchor_text, occurrence_index: 0 },
    citation_ids: [],
    author: { type: note.author_type, label: "Model" }
  }));
  $: review = block?.review || { decision: "pending", version: 0 };
  $: effectiveReview = review.effective_decision || review.decision;
  $: reviewLabel = review.stale ? (review.current_target_version ? "Translation changed since review" : "No active translation for review") : effectiveReview === "approved" ? "Approved" : effectiveReview === "needs_changes" ? "Needs changes" : "Pending";
  $: visibleNoteCount = displayedNotes.filter((note) => note.state !== "retired").length;
  $: notesNeedReview = displayedNotes.some((note) => ["needs_review", "proposed"].includes(note.state));
  $: notesLabel = `Notes for block ${blockNumber}: ${visibleNoteCount}${notesNeedReview ? ", review needed" : ""}`;
  $: canReview = accessRole === "owner" || accessRole === "reviewer";
  $: canEditTranslation = canReview && !readOnly;
  $: alignedReview = reviewMode === "aligned_sentences" && Boolean(target?.alignment);
  $: showAlignmentGroups = alignedReview && !continuousEditorOpen && !draft && !conflict;
  $: alignmentDescription = target?.alignment?.origin === "automatic_fallback"
    ? "Automatically derived pairing — check the sentence boundaries and grouping."
    : target?.alignment?.review_state === "approved"
      ? "The sentence pairing has been approved."
        : target?.alignment?.origin === "carried_forward"
        ? "Sentence pairing retained after editing — review it alongside the translation."
        : target?.alignment?.origin === "user_edited"
          ? "Sentence segments edited by a reader — review the pairing alongside the translation."
        : "Model-reported pairing — review it alongside the translation.";
  $: canChangeStructure = accessRole === "owner" && !readOnly;
  $: needsAttention = Boolean(conflict) || Boolean(target && (target.validation?.valid === false || validationFindings.length));
  $: status = needsAttention ? "attention" : draft ? "edited" : target ? "translated" : "pending";
  $: statusMessage = conflict
    ? "Reload the server version before saving."
    : validationFindings[0]?.message
      || (target?.validation?.valid === false ? "This translation needs attention." : "")
      || (draft ? "Unsaved translation changes." : target ? "Translation saved." : "Not translated yet.");
  $: saveTitle = conflict
    ? "Reload the server version before saving."
    : saving
      ? "Saving translation…"
      : draft
        ? "Save translation"
        : "No unsaved changes.";
  $: saveDisabled = !canEditTranslation || !draft || saving || Boolean(conflict);
  $: roleLabel = block?.role === "accessibility_text"
    ? accessibilityRoleLabel(sourceFields)
    : roleLabels[block?.role] || "Semantic block";
  $: blockNumber = Math.max(1, Number(block?.order) || 1);
</script>

<article class={`block-card status-${status}`} class:selected data-block-id={block?.id} data-block-status={status} data-review-mode={reviewMode} data-has-alignment={Boolean(target?.alignment)}>
  <header class="block-heading">
    <div class="block-title">
      <span class="order">Block {blockNumber}</span>
      <h2>{roleLabel}</h2>
      {#if alignedReview}
        <span class="alignment-info" role="img" aria-label={alignmentDescription} title={alignmentDescription}>
          <svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="7" /><path d="M10 9v5m0-8v.2" /></svg>
        </span>
      {/if}
    </div>
    <div class="block-controls">
      {#if readerNotes.length || translatorNotes.length || canReview}
        <button type="button" class="icon-button notes-toggle" class:active={notesOpen} class:needs-review={notesNeedReview}
          aria-label={notesLabel} title={notesLabel} aria-expanded={notesOpen} aria-controls={`block-notes-${block.id}`}
          on:click={() => (notesOpen = !notesOpen)}>
          <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 3.5h12v10H9l-3.5 3v-3H4z" /><path d="M7 7h6M7 10h4" /></svg>
          {#if visibleNoteCount}<span class="note-count" aria-hidden="true">{visibleNoteCount}</span>{/if}
        </button>
      {/if}
      {#if target}
        {#if canReview}
          <div class="review-menu" use:reviewDisclosure>
            <button bind:this={reviewTriggerElement} type="button" class="icon-button review-toggle"
              class:review-approved={effectiveReview === "approved" && !review.stale} class:needs-review={effectiveReview === "needs_changes" || review.stale}
              disabled={noteBusy} aria-label={`Review block ${blockNumber}: ${reviewLabel}`} title={`Review: ${reviewLabel}`}
              aria-expanded={reviewOpen} aria-controls={`block-review-${block.id}`} on:click={toggleReview}>
              {#if effectiveReview === "needs_changes" || review.stale}
                <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 17V3h10l-2 4 2 4H5" /></svg>
              {:else}
                <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M7 3.5H4v13h12v-13h-3M7 2.5h6v3H7zM6.8 11l2.1 2.1 4.3-4.4" /></svg>
              {/if}
            </button>
            {#if reviewOpen}
              <div bind:this={reviewPanelElement} id={`block-review-${block.id}`} class="menu-popover" role="group" aria-label={`Review decision for block ${blockNumber}`}>
                {#if review.stale}<p>{review.current_target_version ? "The translation changed after the recorded review." : "This review has no current active translation."}</p>{/if}
                <button type="button" aria-pressed={effectiveReview === "approved" && !review.stale} disabled={noteBusy} on:click={() => chooseReview("approved")}>Approved</button>
                <button type="button" aria-pressed={effectiveReview === "needs_changes"} disabled={noteBusy} on:click={() => chooseReview("needs_changes")}>Needs changes</button>
                <p>Record coverage only after checking this whole block:</p>
                {#each [["structural", "Structure checked"], ["target_reading", "Target text read"], ["bilingual", "Compared with source"]] as [kind, label]}
                  <button type="button" disabled={noteBusy || readOnly} aria-pressed={Boolean(review.coverage?.[kind]?.complete)} on:click={() => onCoverage(kind)}>{label}{review.coverage?.[kind]?.complete ? " ✓" : ""}</button>
                {/each}
              </div>
            {/if}
          </div>
        {:else if review.decision !== "pending"}
          <span class="review-readonly" title={`Review: ${reviewLabel}`}>{reviewLabel}</span>
        {/if}
      {/if}
      {#if alignedReview}
        <button type="button" class="icon-button" class:active={!showAlignmentGroups}
          disabled={sentenceEditing || (!showAlignmentGroups && Boolean(draft || conflict))}
          aria-label={showAlignmentGroups ? (canEditTranslation ? "Edit or annotate block" : "View formatted block") : "Return to sentence groups"}
          title={showAlignmentGroups ? (canEditTranslation ? "Edit or annotate block" : "View formatted block") : draft || conflict ? "Save or resolve changes before returning to sentence groups" : "Return to sentence groups"}
          on:click={() => (continuousEditorOpen = !continuousEditorOpen)}>
          {#if showAlignmentGroups}
            <svg viewBox="0 0 20 20" aria-hidden="true"><path d="m4 12 9-9 4 4-9 9-5 1zM11 5l4 4" /></svg>
          {:else}
            <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M3 4h5v12H3zM12 4h5v12h-5zM4.5 8h2M13.5 8h2M4.5 12h2M13.5 12h2" /></svg>
          {/if}
        </button>
      {/if}
      {#if canReview}
        <button
          type="button"
          class:active={selected}
          class:selection-mode={selectionMode}
          class="retranslate-select"
          disabled={selectionDisabled}
          aria-label={selected ? `Block ${blockNumber} selected for retranslation` : `Select block ${blockNumber} for retranslation`}
          aria-pressed={selected}
          title={selected ? "Selected for retranslation. Ctrl/Cmd-click toggles; Shift-click selects a range." : "Select for retranslation"}
          on:click={selectForRetranslation}
        >
          <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M15.5 6.8A6.4 6.4 0 0 0 4.1 7.9" /><path d="M4.2 4.7v3.4h3.4" /><path d="M4.5 13.2a6.4 6.4 0 0 0 11.4-1.1" /><path d="M15.8 15.3v-3.4h-3.4" /></svg>
        </button>
      {/if}
      {#if status !== "translated"}
      <span class={`status-icon ${status}`} role="img" aria-label={statusMessage} title={statusMessage}>
        {#if status === "attention"}
          <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 2.6 18 17H2L10 2.6Zm0 4.2v5.1m0 2.4v.1" /></svg>
        {:else if status === "translated"}
          <svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="7.25" /><path d="m6.8 10.1 2 2.1 4.5-4.7" /></svg>
        {:else if status === "edited"}
          <svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="7.25" /><path d="M10 6.2v4.3l2.8 1.7" /></svg>
        {:else}
          <svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="7.25" /></svg>
        {/if}
      </span>
      {/if}
      {#if canEditTranslation && candidate && draft}
        <button
          type="button"
          class="approve-button"
          class:ready={Boolean(draft) && !saveDisabled}
          disabled={saveDisabled}
          aria-label={saveTitle}
          title={saveTitle}
          on:click={() => onSave(block, draft)}
        >
          <svg viewBox="0 0 20 20" aria-hidden="true"><path d="m4.5 10.2 3.4 3.5 7.7-8" /></svg>
        </button>
      {/if}
      {#if canChangeStructure}
        <div class="block-menu" role="group" aria-label={`Block ${blockNumber} menu`} use:closeOnOutsideClick>
          <button
            bind:this={menuTriggerElement}
            type="button"
            class="menu-trigger"
            aria-label={`Block ${blockNumber} actions`}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            disabled={structuralBusy}
            title={structuralDisabled ? "Save or discard local edits and wait for active work to finish before changing block structure." : "Block actions"}
            on:keydown={handleMenuKeydown}
            on:click={() => { menuOpen = !menuOpen; reviewOpen = false; }}
          >
            <svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="4.5" r="1" /><circle cx="10" cy="10" r="1" /><circle cx="10" cy="15.5" r="1" /></svg>
          </button>
          {#if menuOpen}
            <div class="menu-popover" role="menu" tabindex="-1" aria-label={`Actions for block ${blockNumber}`} on:keydown={handleMenuKeydown}>
              <button type="button" role="menuitem" disabled={structuralDisabled || !canMergePrevious} on:click={() => chooseCorrection("merge_previous")}>Merge with previous</button>
              <button type="button" role="menuitem" disabled={structuralDisabled || !canMergeNext} on:click={() => chooseCorrection("merge_next")}>Merge with next</button>
              <span class="menu-rule" aria-hidden="true"></span>
              <button type="button" role="menuitem" class="exclude-action" disabled={structuralDisabled} on:click={() => chooseCorrection("exclude")}>Exclude from translation</button>
            </div>
          {/if}
        </div>
      {/if}
    </div>
  </header>

  <BlockConflictPanel conflict={conflict} onReload={onReloadConflict} />

  {#if showAlignmentGroups}
    <AlignmentGroupReview
      alignment={target.alignment}
      blockId={block.id}
      notes={displayedNotes}
      {blockNumber}
      targetVersion={target.active_version}
      editable={canEditTranslation}
      {saving}
      onSentenceEdit={(edit, version) => onSentenceEdit(block, edit, version)}
      onEditingChange={(value) => { sentenceEditing = value; onSentenceEditingChange(block.id, value); }}
    />
  {:else}
    <div class="comparison" data-testid="source-target-comparison">
    <section
      class="source-pane"
      aria-label={`Source text for block ${blockNumber}`}
      bind:this={sourcePaneElement}
      use:sourceSelectionActions
    >
      {#if sourceToolbarVisible}
        <div
          class="source-note-toolbar"
          role="toolbar"
          aria-label="Source text actions"
          style={`left:${sourceToolbarLeft}px;top:${sourceToolbarTop}px;`}
        >
          <button
            type="button"
            aria-label="Add note to selected source text"
            title={sourceSelectionRange?.toString().length > MAX_NOTE_ANCHOR_LENGTH ? "Select no more than 160 characters." : "Add translator's note"}
            disabled={!sourceSelectionRange?.toString().trim() || sourceSelectionRange?.toString().length > MAX_NOTE_ANCHOR_LENGTH}
            on:mousedown|preventDefault
            on:click={addSourceNote}
          >
            <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 3.5h12v10H9l-3.5 3v-3H4z" /><path d="M10 6.1v4.8M7.6 8.5h4.8" /></svg>
          </button>
        </div>
      {/if}
      {#if sourceEditor?.content}
        <div class="source-content" data-note-anchor="source" use:decorateSourceContent={sourceEditor.content}>{@html sourceEditor.content}</div> <!-- eslint-disable-line svelte/no-at-html-tags -- validated compact allowlist -->
      {:else if compactAltField}
        <div class="source-content" data-note-anchor="source">{sourceFields[0].value}</div>
      {:else if !sourceFields.length}
        <div class="source-content">No translatable source text was supplied for this block.</div>
      {/if}
      {#if sourceFields.length && !compactAltField}
        <dl class="source-fields">
          {#each sourceFields as field (field.name)}
            <div><dt>{String(field.name).replaceAll("_", " ")}</dt><dd>{field.value}</dd></div>
          {/each}
        </dl>
      {/if}
    </section>
    <section class="target-pane" aria-label={`Translation for block ${blockNumber}`}>
      <TargetBlockCandidateEditor
        candidate={candidate}
        {source}
        guestMode={!canEditTranslation}
        canAnnotate={canReview && Boolean(target) && !draft}
        onChange={(next) => onCandidateChange(block.id, next)}
        onAddNote={prepareAnchoredNote}
      />
    </section>
    </div>
  {/if}

  {#if validationFindings.length}
    <aside class="validation-findings" role="alert">
      <strong>This translation needs attention.</strong>
      <ul>
        {#each validationFindings as finding}
          <li>{finding.message || "The translation no longer matches its protected source structure."}</li>
        {/each}
      </ul>
    </aside>
  {/if}

  {#if readerNotes.length || translatorNotes.length || canReview}
    <BlockNotesEditor
      {block}
      notes={displayedNotes}
      {accessRole}
      bind:expanded={notesOpen}
      createRequest={noteCreateRequest}
      busy={noteBusy}
      error={noteError}
      onCreate={onCreateNote}
      onRevise={onReviseNote}
      onTransition={onTransitionNote}
    />
  {/if}

</article>

<style>
  .block-card { display:grid; gap:0; border:1px solid color-mix(in srgb,var(--color-rule,#d0c8bc) 72%,transparent); background:var(--color-surface,#faf8f3); }
  .block-card.status-attention { border-color:color-mix(in srgb,var(--color-tobacco,#69583f) 48%,var(--color-rule,#d0c8bc)); }
  .block-card.selected { border-color:color-mix(in srgb,var(--color-clay,#7c6053) 62%,var(--color-rule,#d0c8bc)); box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--color-clay,#7c6053) 16%,transparent); }
  .block-heading { display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:.15rem .6rem; padding:.25rem .65rem .25rem 1rem; }
  .block-title { display:flex; align-items:center; flex-wrap:wrap; gap:.45rem; }
  .order { color:var(--color-ink-muted,#675f56); font-size:.61rem; font-weight:650; font-variant-numeric:tabular-nums; }
  h2 { margin:0; color:var(--color-ink-muted,#675f56); font:inherit; font-size:.66rem; font-weight:400; }
  .block-controls { display:flex; align-items:center; gap:.12rem; margin-left:auto; }
  .icon-button { display:flex; align-items:center; justify-content:center; gap:.18rem; min-width:1.8rem; height:1.8rem; padding:0 .25rem; border:0; background:transparent; color:var(--color-ink-muted,#675f56); cursor:pointer; }
  .icon-button svg, .alignment-info svg { width:1rem; height:1rem; fill:none; stroke:currentColor; stroke-width:1.4; stroke-linecap:round; stroke-linejoin:round; }
  .icon-button:hover:not(:disabled), .icon-button:focus-visible, .icon-button.active, .icon-button[aria-expanded="true"] { color:var(--color-ink,#302d29); background:var(--color-surface-muted,#e9e4db); }
  .icon-button:focus-visible { outline:1px solid var(--color-rule-strong,#968b7d); outline-offset:1px; }
  .icon-button:disabled { opacity:.4; cursor:not-allowed; }
  .icon-button.review-approved { color:var(--color-lichen,#56604f); }
  .icon-button.needs-review { color:var(--color-warning,#9a6a18); }
  .note-count { font-size:.58rem; font-variant-numeric:tabular-nums; }
  .alignment-info { display:grid; place-items:center; color:var(--color-ink-muted,#675f56); cursor:help; }
  .alignment-info svg { width:.8rem; height:.8rem; }
  .review-menu { position:relative; }
  .review-readonly { color:var(--color-ink-muted,#675f56); font-size:.62rem; }
  .status-icon, .approve-button, .menu-trigger, .retranslate-select { display:grid; place-items:center; width:1.8rem; height:1.8rem; color:var(--color-ink-muted,#675f56); }
  .retranslate-select { border:0; padding:0; background:transparent; opacity:.28; cursor:pointer; }
  .retranslate-select.selection-mode { opacity:.52; }
  .retranslate-select.active { color:var(--color-clay,#7c6053); background:var(--color-surface-muted,#e9e4db); opacity:.95; }
  .retranslate-select:hover:not(:disabled), .retranslate-select:focus-visible { background:var(--color-surface-muted,#e9e4db); opacity:.9; outline:0; }
  .retranslate-select:focus-visible { box-shadow:0 0 0 1px var(--color-rule-strong,#968b7d); }
  .retranslate-select:disabled { cursor:not-allowed; opacity:.16; }
  .retranslate-select svg { width:1rem; height:1rem; fill:none; stroke:currentColor; stroke-width:1.35; stroke-linecap:round; stroke-linejoin:round; }
  .status-icon { opacity:.48; outline:0; }
  .status-icon.attention { color:var(--color-tobacco,#69583f); opacity:.82; }
  .status-icon.edited { color:var(--color-clay,#7c6053); opacity:.72; }
  .status-icon svg, .approve-button svg, .menu-trigger svg { width:1rem; height:1rem; fill:none; stroke:currentColor; stroke-width:1.45; stroke-linecap:round; stroke-linejoin:round; }
  .approve-button { border:0; background:transparent; opacity:.36; cursor:pointer; padding:0; }
  .approve-button.ready { color:var(--color-lichen,#56604f); opacity:.82; }
  .approve-button:hover:not(:disabled), .approve-button:focus-visible { background:var(--color-surface-muted,#e9e4db); opacity:1; outline:0; }
  .approve-button:focus-visible { box-shadow:0 0 0 1px var(--color-rule-strong,#968b7d); }
  .approve-button:disabled { cursor:default; opacity:.25; }
  .block-menu { position:relative; }
  .menu-trigger { border:0; padding:0; background:transparent; opacity:.45; cursor:pointer; }
  .menu-trigger:hover:not(:disabled), .menu-trigger:focus-visible, .menu-trigger[aria-expanded="true"] { background:var(--color-surface-muted,#e9e4db); opacity:.9; outline:0; }
  .menu-trigger:focus-visible { box-shadow:0 0 0 1px var(--color-rule-strong,#968b7d); }
  .menu-trigger:disabled { cursor:not-allowed; opacity:.22; }
  .menu-trigger svg circle { fill:currentColor; stroke:none; }
  .menu-popover { position:absolute; z-index:12; top:calc(100% + .25rem); right:0; width:max-content; min-width:12.5rem; display:grid; padding:.3rem; border:1px solid var(--color-rule,#d0c8bc); background:var(--color-surface,#faf8f3); box-shadow:0 .7rem 1.8rem color-mix(in srgb,var(--color-ink,#302d29) 14%,transparent); }
  .menu-popover button { border:0; padding:.5rem .6rem; background:transparent; color:var(--color-ink-soft,#4d4842); cursor:pointer; font:inherit; font-size:.76rem; text-align:left; }
  .menu-popover button:hover:not(:disabled), .menu-popover button:focus-visible { background:var(--color-surface-muted,#e9e4db); outline:0; }
  .menu-popover button:disabled { color:var(--color-ink-muted,#675f56); cursor:not-allowed; opacity:.4; }
  .menu-popover button[aria-pressed="true"] { background:var(--color-surface-muted,#e9e4db); font-weight:700; }
  .menu-popover .exclude-action { color:var(--color-clay,#7c6053); }
  .menu-rule { height:1px; margin:.25rem .35rem; background:var(--color-rule,#d0c8bc); }
  .comparison { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); }
  .source-pane, .target-pane { min-width:0; display:grid; align-content:start; gap:.75rem; padding:1rem 1rem 1.1rem 1.2rem; }
  .source-pane { position:relative; border-right:1px solid color-mix(in srgb,var(--color-rule,#d0c8bc) 55%,transparent); }
  .source-note-toolbar { position:absolute; z-index:3; display:flex; padding:.16rem; border:1px solid var(--color-rule,#d0c8bc); background:var(--color-surface,#faf8f3); box-shadow:0 .3rem .9rem color-mix(in srgb,var(--color-ink,#302d29) 13%,transparent); transform:translate(-50%,-100%); }
  .source-note-toolbar button { display:grid; place-items:center; width:1.75rem; height:1.65rem; padding:0; border:0; background:transparent; color:var(--color-ink-muted,#675f56); cursor:pointer; }
  .source-note-toolbar button:hover, .source-note-toolbar button:focus-visible { background:var(--color-surface-muted,#e9e4db); color:var(--color-ink,#302d29); outline:0; }
  .source-note-toolbar button:focus-visible { box-shadow:inset 0 0 0 1px var(--color-rule-strong,#968b7d); }
  .source-note-toolbar button:disabled { cursor:not-allowed; opacity:.42; }
  .source-note-toolbar svg { width:.92rem; height:.92rem; fill:none; stroke:currentColor; stroke-width:1.55; stroke-linecap:round; stroke-linejoin:round; }
  .target-pane { background:var(--color-surface,#faf8f3); }
  .source-content { white-space:pre-wrap; overflow-wrap:anywhere; color:var(--color-ink,#302d29); font-family:var(--font-editorial,Georgia,serif); font-size:1rem; line-height:1.62; unicode-bidi:plaintext; }
  .source-content :global(a) { color:inherit; text-decoration:underline; text-decoration-color:color-mix(in srgb,currentColor 45%,transparent); text-underline-offset:.14em; }
  .source-content :global(code), .source-content :global(kbd), .source-content :global(samp), .source-content :global(var) { font-family:var(--font-mono,ui-monospace,monospace); font-size:.9em; }
  .source-content :global(mark) { background:var(--color-tobacco-muted,#e4ded2); color:inherit; }
  .source-content :global(lit) { color:inherit; text-decoration:underline dotted color-mix(in srgb,var(--color-clay,#7c6053) 48%,transparent); text-underline-offset:.18em; }
  .source-content :global(lit[data-boundary-whitespace]) { display:none; }
  .source-fields { display:grid; gap:.42rem; margin:.1rem 0 0; padding:.65rem .7rem; border:1px solid var(--color-rule,#d0c8bc); }
  .source-fields div { display:grid; grid-template-columns:minmax(7rem,.3fr) minmax(0,1fr); gap:.6rem; align-items:baseline; }
  .source-fields dt { color:var(--color-ink-muted,#675f56); font-size:.75rem; text-transform:capitalize; }
  .source-fields dd { min-width:0; margin:0; overflow-wrap:anywhere; color:var(--color-ink,#302d29); font-size:.82rem; line-height:1.45; }
  .validation-findings { margin:0 1rem 1rem 1.2rem; padding:.7rem .8rem; border:1px solid color-mix(in srgb,var(--color-tobacco,#69583f) 42%,var(--color-rule,#d0c8bc)); background:var(--color-tobacco-muted,#e4ded2); color:var(--color-ink-soft,#4d4842); font-size:.8rem; }
  .validation-findings strong { color:var(--color-tobacco,#69583f); }
  .validation-findings ul { margin:.4rem 0 0; padding-left:1.1rem; }
  @media (max-width: 860px) { .comparison { grid-template-columns:1fr; } .source-pane { border-right:0; border-bottom:1px solid var(--color-rule,#d0c8bc); } }
  @media (max-width: 620px) { .block-heading { padding:.25rem .5rem .25rem .75rem; } .source-pane, .target-pane { padding:.85rem .85rem 1rem 1rem; } .source-fields div { grid-template-columns:1fr; gap:.15rem; } }
</style>
