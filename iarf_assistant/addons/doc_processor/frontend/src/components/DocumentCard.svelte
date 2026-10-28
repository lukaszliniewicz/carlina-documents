<script lang="ts">
  import { Icon } from "@carlina/ui";
  import { createEventDispatcher } from "svelte";
  import type { ProcessedDocument } from "../lib/types";
  import { formatDate, formatStatusLabel, statusTone } from "../lib/utils";
  import { getSourceFileUrl } from "../lib/api";
  import MetricPills from "./MetricPills.svelte";

  export let document: ProcessedDocument;
  export let index: number | null = null;
  export let canMoveUp = false;
  export let canMoveDown = false;
  export let removable = false;
  export let showDelete = true;

  type OverflowAction = "moveUp" | "moveDown" | "export" | "pdfPreview" | "reviewImages" | "stop" | "remove" | "delete";

  const dispatch = createEventDispatcher<{
    crop: string;
    reviewImages: string;
    preview: string;
    pdfPreview: string;
    export: string;
    delete: string;
    resume: string;
    pause: string;
    stop: string;
    moveUp: string;
    moveDown: string;
    remove: string;
  }>();

  $: isPdf = document.source_available && document.filename?.toLowerCase().endsWith(".pdf");
  $: downloadUrl = document.source_available ? getSourceFileUrl(document.id, "attachment") : "";

  $: status = document.source_available === false ? "missing_source" : document.status;
  $: tone = statusTone(status);
  $: progress = document.settings?.progress;
  $: progressValue = progress === undefined || progress === null ? null : Math.max(0, Math.min(100, Math.round(Number(progress) || 0)));
  $: progressMessage = document.settings?.progress_msg || "";
  $: canResume = status === "suspended" || Boolean(document.settings?.pdf_state);
  $: recoveryBlocked = status === "suspended" && document.settings?.mcp_recovery?.reason?.error_code === "recovery_replay_conflict";
  $: statusMessage =
    (recoveryBlocked ? "Saved responses no longer match the extraction request. Progress is preserved; recovery needs inspection." : "") ||
    (status === "suspended" ? "Extraction interrupted. Saved progress and accepted responses are preserved. Resume to continue." : "") ||
    document.error_message ||
    (status === "paused" ? "Extraction paused. Completed pages are saved." : "") ||
    (status === "cancelled" ? "Extraction stopped. Completed pages are saved." : "") ||
    (status === "missing_source" ? "Source file missing." : "") ||
    (status === "error" ? "Extraction error." : "");
  $: isCompleted = status === "completed" || status === "completed_with_errors";
  $: hasOverflowActions =
    canMoveUp ||
    canMoveDown ||
    isCompleted ||
    (status === "processing" || status === "pausing") && document.settings?.engine_id === "llm_vision" ||
    removable ||
    (document.source_available && !isPdf) ||
    showDelete;
  $: overflowMenuId = `doc-actions-${document.id}`;

  let overflowOpen = false;
  let overflowContainer: HTMLDivElement;
  let overflowToggle: HTMLButtonElement;

  function closeOverflow(returnFocus = false) {
    overflowOpen = false;
    if (returnFocus) {
      overflowToggle?.focus();
    }
  }

  function triggerOverflowAction(action: OverflowAction) {
    dispatch(action, document.id);
    closeOverflow();
  }

  function handleOverflowKeydown(event: KeyboardEvent) {
    if (overflowOpen && event.key === "Escape") {
      event.preventDefault();
      closeOverflow(true);
    }
  }

  function overflowKeyboard(node: HTMLElement) {
    node.addEventListener("keydown", handleOverflowKeydown);
    return {
      destroy() {
        node.removeEventListener("keydown", handleOverflowKeydown);
      },
    };
  }

  function handleOverflowFocusOut(event: FocusEvent) {
    const nextFocusedElement = event.relatedTarget;
    if (nextFocusedElement instanceof Node && overflowContainer?.contains(nextFocusedElement)) {
      return;
    }
    closeOverflow();
  }
</script>

<article class:menu-open={overflowOpen} class="dp-document-card {tone}">
  <div class="dp-document-main">
    <strong title={document.filename}>{index !== null ? `${index + 1}. ` : ""}{document.filename}</strong>
    <div class="dp-document-meta">
      <span class="dp-status-badge {tone}">{formatStatusLabel(status)}</span>
      <span>{formatDate(document.created_at)}</span>
      <MetricPills settings={document.settings} />
    </div>
  </div>

  <div class="dp-document-actions">
    {#if status === "preparing_crop_review"}
      <div class="dp-progress-note">
        <strong>{progressValue === null ? "Preparing crop review" : `Preparing crop review (${progressValue}%)`}</strong>
        {#if progressMessage}<small>{progressMessage}</small>{/if}
      </div>
    {:else if status === "needs_cropping"}
      <button class="button warning" type="button" on:click={() => dispatch("crop", document.id)}>
        <Icon name="crop" size={15} /> Crop
      </button>
    {:else if status === "reviewing_images"}
      <button class="button info" type="button" on:click={() => dispatch("reviewImages", document.id)}>
        <Icon name="image" size={15} /> Review
      </button>
    {:else if status === "processing" || status === "pausing"}
      <div class="dp-progress-note">
        <strong>{progressValue === null ? formatStatusLabel(status) : `${formatStatusLabel(status)} (${progressValue}%)`}</strong>
        {#if progressMessage}<small>{progressMessage}</small>{/if}
      </div>
      {#if document.settings?.engine_id === "llm_vision"}
        <button class="button" type="button" on:click={() => dispatch("pause", document.id)}><Icon name="pause" size={14} /> Pause</button>
      {/if}
    {:else if isCompleted}
      <button class="button" type="button" on:click={() => dispatch("preview", document.id)}>
        <Icon name="eye" size={15} /> View
      </button>
    {:else if ["error", "paused", "cancelled", "missing_source", "suspended"].includes(status)}
      <div class="dp-error-note" title={statusMessage}>
        <span>{statusMessage}</span>
        {#if status !== "missing_source" && !recoveryBlocked}
          <button class="button" type="button" on:click={() => dispatch("resume", document.id)}>
            <Icon name="play" size={14} />
            {canResume ? "Resume" : "Retry"}
          </button>
        {/if}
      </div>
    {:else}
      <button class="button" type="button" on:click={() => dispatch("resume", document.id)}>
        <Icon name="play" size={14} /> Start
      </button>
    {/if}

    {#if hasOverflowActions}
      <div
        bind:this={overflowContainer}
        use:overflowKeyboard
        class="dp-document-overflow"
        role="group"
        aria-label={`Document actions for ${document.filename}`}
        on:focusout={handleOverflowFocusOut}
      >
        <button
          bind:this={overflowToggle}
          class="dp-icon-btn dp-document-overflow-toggle"
          type="button"
          title="More document actions"
          aria-label={`More actions for ${document.filename}`}
          aria-controls={overflowMenuId}
          aria-expanded={overflowOpen}
          on:click={() => (overflowOpen = !overflowOpen)}
        >
          <Icon name="more" size={18} />
        </button>
        <div
          id={overflowMenuId}
          class="dp-document-overflow-menu"
          role="group"
          aria-label={`More actions for ${document.filename}`}
          hidden={!overflowOpen}
        >
          {#if canMoveUp || canMoveDown}
            <button class="button" type="button" disabled={!canMoveUp} on:click={() => triggerOverflowAction("moveUp")}>
              <Icon name="arrow-up" size={15} /> Move up
            </button>
            <button class="button" type="button" disabled={!canMoveDown} on:click={() => triggerOverflowAction("moveDown")}>
              <Icon name="arrow-down" size={15} /> Move down
            </button>
          {/if}

          {#if isCompleted}
            <button class="button" type="button" on:click={() => triggerOverflowAction("export")}>
              <Icon name="download" size={15} /> Export
            </button>
            {#if isPdf}
              <button class="button" type="button" on:click={() => triggerOverflowAction("reviewImages")}>
                <Icon name="crop" size={15} /> Adjust image crops
              </button>
              <button class="button" type="button" on:click={() => triggerOverflowAction("pdfPreview")}>
                <Icon name="file" size={15} /> Preview original PDF
              </button>
            {/if}
          {/if}

          {#if (status === "processing" || status === "pausing") && document.settings?.engine_id === "llm_vision"}
            <button class="button danger" type="button" on:click={() => triggerOverflowAction("stop")}>
              <Icon name="stop" size={14} /> Stop
            </button>
          {/if}

          {#if removable}
            <button class="button danger" type="button" on:click={() => triggerOverflowAction("remove")}><Icon name="close" size={14} /> Remove</button>
          {/if}

          {#if document.source_available && !isPdf}
            <a class="button" href={downloadUrl} download={document.filename} on:click={() => closeOverflow()}>
              <Icon name="download" size={15} /> Download original
            </a>
          {/if}

          {#if showDelete}
            <button class="button danger" type="button" on:click={() => triggerOverflowAction("delete")}>
              <Icon name="trash" size={15} /> Delete document
            </button>
          {/if}
        </div>
      </div>
    {/if}
  </div>
</article>
