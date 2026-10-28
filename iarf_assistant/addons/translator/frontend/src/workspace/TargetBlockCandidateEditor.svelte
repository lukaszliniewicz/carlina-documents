<script>
  import {
    decorateProtectedLiterals,
    editorStructureMatches,
    serializeCompactContent
  } from "./compact-editor.js";

  export let candidate = null;
  export let source = {};
  export let guestMode = false;
  export let canAnnotate = false;
  export let onChange = () => {};
  export let onAddNote = () => {};

  const TOOLBAR_HALF_WIDTH = 102;
  const MAX_NOTE_ANCHOR_LENGTH = 160;

  let editorElement = null;
  let candidateElement = null;
  let appliedElement = null;
  let appliedContent = null;
  let editError = "";
  let selectedRange = null;
  let toolbarVisible = false;
  let toolbarLeft = 0;
  let toolbarTop = 0;
  let formattingAvailable = false;

  $: fields = Array.isArray(candidate?.fields) ? candidate.fields : [];
  $: sourceEditor = source?.editor || null;
  $: content = typeof candidate?.content === "string" ? candidate.content : "";
  $: hasProse = Boolean(sourceEditor?.content);
  $: compactAltField = !hasProse
    && fields.length === 1
    && String(fields[0]?.name || "").toLowerCase() === "alt";
  $: if (editorElement && candidate && (editorElement !== appliedElement || content !== appliedContent)) applyContent(content);

  function applyContent(value) {
    if (!editorElement) return;
    // The contenteditable subtree is intentionally browser-owned while focused;
    // Svelte owns no children here, and every value already passed the compact allowlist.
    // eslint-disable-next-line svelte/no-dom-manipulating
    editorElement.innerHTML = value;
    decorateProtectedLiterals(editorElement);
    appliedElement = editorElement;
    appliedContent = value;
    editError = "";
    hideFormattingToolbar();
  }

  function updateContent() {
    if (guestMode || !candidate || !editorElement) return;
    try {
      const nextContent = serializeCompactContent(editorElement);
      if (!sourceEditor || !editorStructureMatches(sourceEditor, nextContent)) {
        throw new Error("Source formatting or fixed text was removed.");
      }
      appliedContent = nextContent;
      decorateProtectedLiterals(editorElement);
      editError = "";
      onChange({ ...candidate, content: nextContent });
    } catch (caught) {
      const message = caught?.message || "This edit changed the source structure and was undone.";
      applyContent(appliedContent ?? content);
      editError = message;
    }
  }

  function updateField(index, value) {
    if (guestMode || !candidate) return;
    const nextFields = fields.map((field, fieldIndex) => fieldIndex === index
      ? { name: field.name, value }
      : { ...field });
    onChange({ ...candidate, fields: nextFields });
  }

  function fieldLabel(name) {
    return String(name || "translated field").replaceAll("_", " ");
  }

  function selectionInsideEditor(selection) {
    if (!selection?.rangeCount || !editorElement) return false;
    const container = selection.getRangeAt(0).commonAncestorContainer;
    const element = container.nodeType === Node.ELEMENT_NODE ? container : container.parentNode;
    return container === editorElement || Boolean(element && editorElement.contains(element));
  }

  function nodeElement(node) {
    if (!node) return null;
    return node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
  }

  function hideFormattingToolbar() {
    toolbarVisible = false;
    formattingAvailable = false;
    selectedRange = null;
  }

  function rangeTouchesProtectedText(range) {
    return [...(editorElement?.querySelectorAll?.("lit") || [])]
      .some((literal) => range.intersectsNode(literal));
  }

  function captureFormattingSelection() {
    if ((!canAnnotate && guestMode) || !candidateElement) {
      hideFormattingToolbar();
      return;
    }
    const selection = window.getSelection();
    if (!selectionInsideEditor(selection) || selection.isCollapsed) {
      hideFormattingToolbar();
      return;
    }
    const range = selection.getRangeAt(0);
    formattingAvailable = !guestMode && !rangeTouchesProtectedText(range);
    if (!formattingAvailable && !canAnnotate) {
      hideFormattingToolbar();
      return;
    }
    const rect = typeof range.getBoundingClientRect === "function"
      ? range.getBoundingClientRect()
      : editorElement.getBoundingClientRect();
    const hostRect = candidateElement.getBoundingClientRect();
    selectedRange = range.cloneRange();
    toolbarLeft = Math.max(
      TOOLBAR_HALF_WIDTH,
      Math.min(hostRect.width - TOOLBAR_HALF_WIDTH, rect.left - hostRect.left + rect.width / 2)
    );
    toolbarTop = Math.max(2, rect.top - hostRect.top - 8);
    toolbarVisible = true;
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

  function addNoteForSelection() {
    if (!canAnnotate || !selectedRange || !editorElement) return;
    const anchorText = selectedRange.toString();
    if (!anchorText.trim() || anchorText.length > MAX_NOTE_ANCHOR_LENGTH) return;
    onAddNote({
      kind: "target",
      text: anchorText,
      occurrence_index: occurrenceIndex(editorElement, selectedRange, anchorText)
    });
    hideFormattingToolbar();
  }

  function matchingFormatAncestor(range, tag) {
    const start = nodeElement(range.startContainer)?.closest?.(tag);
    const end = nodeElement(range.endContainer)?.closest?.(tag);
    return start && start === end && editorElement?.contains(start) ? start : null;
  }

  function unwrapElement(element) {
    const parent = element.parentNode;
    if (!parent) return;
    while (element.firstChild) parent.insertBefore(element.firstChild, element);
    parent.removeChild(element);
  }

  function applyInlineFormat(tag) {
    if (guestMode || !selectedRange || !editorElement) return;
    const range = selectedRange.cloneRange();
    if (range.collapsed || rangeTouchesProtectedText(range)) {
      editError = "Select editable translation text before adding formatting.";
      hideFormattingToolbar();
      return;
    }
    const selection = window.getSelection();
    const existing = matchingFormatAncestor(range, tag);
    if (existing) {
      unwrapElement(existing);
    } else {
      const wrapper = document.createElement(tag);
      wrapper.appendChild(range.extractContents());
      range.insertNode(wrapper);
      range.selectNodeContents(wrapper);
      selection.removeAllRanges();
      selection.addRange(range);
    }
    updateContent();
    captureFormattingSelection();
  }

  function insertPlainText(value) {
    const selection = window.getSelection();
    if (!selectionInsideEditor(selection)) return;
    const range = selection.getRangeAt(0);
    const parent = range.commonAncestorContainer.nodeType === Node.ELEMENT_NODE
      ? range.commonAncestorContainer
      : range.commonAncestorContainer.parentElement;
    if (parent?.closest?.("lit")) return;
    const normalized = String(value || "").replace(/\s*\r?\n+\s*/g, " ");
    range.deleteContents();
    const text = document.createTextNode(normalized);
    range.insertNode(text);
    range.setStartAfter(text);
    range.collapse(true);
    selection.removeAllRanges();
    selection.addRange(range);
    updateContent();
  }

  function handlePaste(event) {
    if (guestMode) return;
    event.preventDefault();
    insertPlainText(event.clipboardData?.getData("text/plain") || "");
  }

  function handleBeforeInput(event) {
    const type = String(event.inputType || "");
    if (guestMode || type === "insertParagraph" || type === "insertLineBreak" || type.startsWith("format") || type.includes("List")) {
      event.preventDefault();
      if (!guestMode) editError = "This semantic block keeps the source paragraph and formatting structure.";
    }
  }

  function handleKeydown(event) {
    const shortcut = (event.metaKey || event.ctrlKey) && !event.altKey
      ? event.key.toLowerCase()
      : "";
    if (shortcut === "b" || shortcut === "i" || shortcut === "u") {
      event.preventDefault();
      captureFormattingSelection();
      if (!selectedRange) {
        editError = "Select translation text before adding formatting.";
        return;
      }
      applyInlineFormat(shortcut === "b" ? "strong" : shortcut === "i" ? "em" : "u");
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      editError = "This semantic block stays one directly comparable unit.";
    } else if (event.key === "Escape") {
      hideFormattingToolbar();
    }
  }

  function handleEditorBlur(event) {
    if (!candidateElement?.contains(event.relatedTarget)) hideFormattingToolbar();
  }
</script>

{#if candidate}
  <div class="candidate-editor" bind:this={candidateElement} data-testid="target-candidate-editor" data-guest-mode={guestMode}>
    {#if toolbarVisible && (formattingAvailable || canAnnotate)}
      <div
        class="formatting-toolbar"
        role="toolbar"
        aria-label="Text actions"
        style={`left:${toolbarLeft}px;top:${toolbarTop}px;`}
      >
        {#if formattingAvailable}
          <button
            type="button"
            aria-label="Bold selected text"
            title="Bold · Ctrl/Cmd+B"
            on:mousedown|preventDefault
            on:click={() => applyInlineFormat("strong")}
          >
            <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M6.5 3.8h4.2a3 3 0 0 1 0 6H6.5m0 0h4.8a3.2 3.2 0 0 1 0 6.4H6.5V3.8" /></svg>
          </button>
          <button
            type="button"
            aria-label="Italicize selected text"
            title="Italic · Ctrl/Cmd+I"
            on:mousedown|preventDefault
            on:click={() => applyInlineFormat("em")}
          >
            <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M8.2 3.8h6M5.8 16.2h6m.1-12.4-3.8 12.4" /></svg>
          </button>
          <button
            type="button"
            aria-label="Underline selected text"
            title="Underline · Ctrl/Cmd+U"
            on:mousedown|preventDefault
            on:click={() => applyInlineFormat("u")}
          >
            <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 3.8v6.1a5 5 0 0 0 10 0V3.8M4 16.2h12" /></svg>
          </button>
          <button
            type="button"
            aria-label="Superscript selected text"
            title="Superscript"
            on:mousedown|preventDefault
            on:click={() => applyInlineFormat("sup")}
          >
            <svg class="script-icon" viewBox="0 0 20 20" aria-hidden="true"><text x="3" y="14.7">x</text><text x="11.6" y="8.2">2</text></svg>
          </button>
          <button
            type="button"
            aria-label="Subscript selected text"
            title="Subscript"
            on:mousedown|preventDefault
            on:click={() => applyInlineFormat("sub")}
          >
            <svg class="script-icon" viewBox="0 0 20 20" aria-hidden="true"><text x="3" y="11.8">x</text><text x="11.6" y="16.2">2</text></svg>
          </button>
        {/if}
        {#if canAnnotate}
          <button
            type="button"
            class:with-rule={formattingAvailable}
            class="note-action"
            aria-label="Add note to selected translation"
            title={selectedRange?.toString().length > MAX_NOTE_ANCHOR_LENGTH ? "Select no more than 160 characters." : "Add translator's note"}
            disabled={!selectedRange?.toString().trim() || selectedRange?.toString().length > MAX_NOTE_ANCHOR_LENGTH}
            on:mousedown|preventDefault
            on:click={addNoteForSelection}
          >
            <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 3.5h12v10H9l-3.5 3v-3H4z" /><path d="M10 6.1v4.8M7.6 8.5h4.8" /></svg>
          </button>
        {/if}
      </div>
    {/if}
    {#if hasProse}
      <div
        class="translation-surface"
        class:read-only={guestMode}
        bind:this={editorElement}
        contenteditable={guestMode ? "false" : "true"}
        tabindex="0"
        role="textbox"
        aria-label="Translation text"
        aria-multiline="false"
        spellcheck={!guestMode}
        data-placeholder="Translation text"
        on:input={updateContent}
        on:paste={handlePaste}
        on:beforeinput={handleBeforeInput}
        on:keydown={handleKeydown}
        on:blur={handleEditorBlur}
        on:mouseup={captureFormattingSelection}
        on:touchend={captureFormattingSelection}
        on:keyup={captureFormattingSelection}
      ></div>
    {/if}

    {#if compactAltField}
      <textarea
        class="translation-field-surface"
        aria-label="Translated alt"
        rows="4"
        value={fields[0].value || ""}
        readonly={guestMode}
        spellcheck={!guestMode}
        on:input={(event) => updateField(0, event.currentTarget.value)}
      ></textarea>
    {:else if fields.length}
      <fieldset class="lexical-fields">
        <legend>Accessible text</legend>
        {#each fields as field, index (field.name)}
          <label>
            <span>{fieldLabel(field.name)}</span>
            <input
              aria-label={`Translated ${fieldLabel(field.name)}`}
              value={field.value || ""}
              readonly={guestMode}
              on:input={(event) => updateField(index, event.currentTarget.value)}
            />
          </label>
        {/each}
      </fieldset>
    {/if}

    {#if editError}<p class="edit-note" role="status">{editError}</p>{/if}
  </div>
{:else}
  <div class="pending">
    <strong>Not translated yet</strong>
    <span>Run the remaining-block translation to create a draft.</span>
  </div>
{/if}

<style>
  .candidate-editor { position:relative; display:grid; gap:.75rem; }
  .translation-surface { box-sizing:border-box; width:100%; min-height:1.62em; padding:0 0 .15rem; border:0; border-bottom:1px solid transparent; outline:0; overflow-wrap:anywhere; color:var(--color-ink,#302d29); background:transparent; font-family:var(--font-editorial,Georgia,serif); font-size:1rem; line-height:1.62; unicode-bidi:plaintext; white-space:pre-wrap; }
  .translation-surface:empty::before { content:attr(data-placeholder); color:var(--color-ink-muted,#675f56); opacity:.58; pointer-events:none; }
  .translation-surface[contenteditable="true"]:hover { border-bottom-color:var(--color-rule,#d0c8bc); }
  .translation-surface[contenteditable="true"]:focus { border-bottom-color:var(--color-umber,#5c5149); box-shadow:0 2px 0 color-mix(in srgb,var(--color-umber,#5c5149) 22%,transparent); }
  .translation-surface.read-only { color:var(--color-ink-soft,#4d4842); }
  .translation-surface :global(a) { color:inherit; text-decoration:underline; text-decoration-color:color-mix(in srgb,currentColor 45%,transparent); text-underline-offset:.14em; }
  .translation-surface :global(code), .translation-surface :global(kbd), .translation-surface :global(samp), .translation-surface :global(var) { font-family:var(--font-mono,ui-monospace,monospace); font-size:.9em; }
  .translation-surface :global(mark) { background:var(--color-tobacco-muted,#e4ded2); color:inherit; }
  .translation-surface :global(lit) { color:inherit; text-decoration:underline dotted color-mix(in srgb,var(--color-clay,#7c6053) 48%,transparent); text-underline-offset:.18em; cursor:default; }
  .translation-surface :global(lit[data-boundary-whitespace]) { display:none; }
  .formatting-toolbar { position:absolute; z-index:3; display:flex; gap:.08rem; padding:.16rem; border:1px solid var(--color-rule,#d0c8bc); background:var(--color-surface,#faf8f3); box-shadow:0 .3rem .9rem color-mix(in srgb,var(--color-ink,#302d29) 13%,transparent); transform:translate(-50%,-100%); }
  .formatting-toolbar button { display:grid; place-items:center; width:1.75rem; height:1.65rem; padding:0; border:0; background:transparent; color:var(--color-ink-muted,#675f56); cursor:pointer; }
  .formatting-toolbar button:hover, .formatting-toolbar button:focus-visible { background:var(--color-surface-muted,#e9e4db); color:var(--color-ink,#302d29); outline:0; }
  .formatting-toolbar button:disabled { cursor:not-allowed; opacity:.42; }
  .formatting-toolbar .note-action.with-rule { margin-left:.12rem; border-left:1px solid var(--color-rule,#d0c8bc); }
  .formatting-toolbar button:focus-visible { box-shadow:inset 0 0 0 1px var(--color-rule-strong,#968b7d); }
  .formatting-toolbar svg { width:.92rem; height:.92rem; fill:none; stroke:currentColor; stroke-width:1.55; stroke-linecap:round; stroke-linejoin:round; }
  .formatting-toolbar .script-icon { fill:currentColor; stroke:none; }
  .formatting-toolbar .script-icon text:first-child { font:italic 13px var(--font-editorial,Georgia,serif); }
  .formatting-toolbar .script-icon text:last-child { font:600 8px var(--font-sans,system-ui,sans-serif); }
  .translation-field-surface { box-sizing:border-box; width:100%; min-height:2.6rem; margin:0; padding:0 0 .35rem; resize:none; border:0; border-bottom:1px solid transparent; outline:0; overflow:auto; color:var(--color-ink,#302d29); background:transparent; font-family:var(--font-editorial,Georgia,serif); font-size:1rem; line-height:1.62; }
  .translation-field-surface:hover:not([readonly]) { border-bottom-color:var(--color-rule,#d0c8bc); }
  .translation-field-surface:focus { border-bottom-color:var(--color-umber,#5c5149); box-shadow:0 2px 0 color-mix(in srgb,var(--color-umber,#5c5149) 22%,transparent); }
  .translation-field-surface[readonly] { color:var(--color-ink-soft,#4d4842); }
  input { box-sizing:border-box; width:100%; border:1px solid var(--color-rule,#d0c8bc); padding:.58rem .62rem; background:var(--color-surface,#faf8f3); color:var(--color-ink,#302d29); font:inherit; }
  input:focus { outline:2px solid color-mix(in srgb,var(--color-umber,#5c5149) 30%,transparent); outline-offset:1px; border-color:var(--color-umber,#5c5149); }
  input[readonly] { background:var(--color-paper-muted,#f1eee7); }
  .lexical-fields { display:grid; gap:.45rem; min-width:0; margin:0; padding:.65rem .7rem; border:1px solid var(--color-rule,#d0c8bc); }
  .lexical-fields legend { padding:0 .3rem; color:var(--color-ink-muted,#675f56); font-size:.7rem; }
  .lexical-fields label { display:grid; grid-template-columns:minmax(7rem,.3fr) minmax(0,1fr); align-items:center; gap:.6rem; color:var(--color-ink-muted,#675f56); font-size:.75rem; text-transform:capitalize; }
  .edit-note { margin:0; color:var(--color-tobacco,#69583f); font-size:.73rem; }
  .pending { min-height:5.5rem; display:grid; align-content:center; gap:.25rem; padding:.7rem; border:1px dashed var(--color-rule,#d0c8bc); color:var(--color-ink-muted,#675f56); }
  .pending strong { color:var(--color-ink-soft,#4d4842); font-family:var(--font-editorial,Georgia,serif); font-weight:500; }
  .pending span { font-size:.78rem; }
  @media (max-width:620px) { .lexical-fields label { grid-template-columns:1fr; } }
</style>
