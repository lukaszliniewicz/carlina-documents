<script lang="ts">
  import { EmptyState, Icon } from "@carlina/ui";
  import { createEventDispatcher } from "svelte";
  import type { DocumentProject, ProcessedDocument, ProjectRunDocument } from "../lib/types";
  import { ACCEPTED_DOCUMENT_TYPES } from "../lib/options";
  import { formatStatusLabel, projectHasCompletedDocument } from "../lib/utils";
  import DocumentCard from "./DocumentCard.svelte";

  export let project: DocumentProject;
  export let documents: ProcessedDocument[] = [];
  export let uploadBusy = false;
  const dispatch = createEventDispatcher<{
    back: void;
    deleteProject: string;
    exportProject: string;
    updateConcurrency: number;
    addDocument: string;
    uploadDocuments: FileList;
    runProject: string;
    controlRun: { runId: string; action: "pause" | "resume" | "stop" };
    removeDocument: string;
    moveDocument: { documentId: string; offset: number };
    crop: string;
    reviewImages: string;
    preview: string;
    pdfPreview: string;
    export: string;
    delete: string;
    resume: string;
    pause: string;
    stop: string;
  }>();

  let uploadInput: HTMLInputElement;
  let selectedDocumentId = "";

  $: run = project.latest_run;
  $: runStatus = run?.status || "created";
  $: runDocuments = new Map<string, ProjectRunDocument>((run?.documents || []).map((entry) => [entry.document_id, entry]));
  $: existingIds = new Set(project.documents.map((entry) => entry.document_id));
  $: availableDocuments = documents.filter((document) => !existingIds.has(document.id) && document.source_available !== false);
  $: hasCompleted = projectHasCompletedDocument(project);
  $: activeRun = run && !["completed", "completed_with_errors", "cancelled"].includes(runStatus);

  function displayDocument(entryDocument: ProcessedDocument | null | undefined, documentId: string): ProcessedDocument | null {
    if (!entryDocument) {
      return null;
    }

    const runEntry = runDocuments.get(documentId);
    const isRunActive = Boolean(activeRun);
    const status = entryDocument.source_available === false
      ? "missing_source"
      : ["completed", "completed_with_errors"].includes(entryDocument.status)
        ? entryDocument.status
        : isRunActive && runEntry?.status
          ? runEntry.status
          : entryDocument.status || "created";

    return { ...entryDocument, status, error_message: runEntry?.error_message || entryDocument.error_message };
  }

  function addSelectedDocument() {
    if (selectedDocumentId) {
      dispatch("addDocument", selectedDocumentId);
      selectedDocumentId = "";
    }
  }
</script>

<section class="dp-content-panel">
  <header class="dp-content-header project">
    <div>
      <button class="button ghost" type="button" on:click={() => dispatch("back")}>
        <Icon name="arrow-left" size={15} /> Back
      </button>
      <h2>{project.name}</h2>
      <p>Status: {formatStatusLabel(runStatus)}</p>
    </div>
    <div class="dp-header-actions">
      <button class="button" type="button" disabled={!hasCompleted} on:click={() => dispatch("exportProject", project.id)}>
        <Icon name="download" size={15} /> Export
      </button>
      <button class="button danger" type="button" on:click={() => dispatch("deleteProject", project.id)}>
        <Icon name="trash" size={15} /> Delete
      </button>
    </div>
  </header>

  <div class="dp-project-toolbar">
    <label class="dp-inline-field">
      <span>Concurrent docs</span>
      <input
        type="number"
        min="1"
        max="10"
        value={Number(project.settings?.document_concurrency || 2)}
        on:change={(event) => dispatch("updateConcurrency", Number(event.currentTarget.value) || 1)}
      />
    </label>

    <input
      bind:this={uploadInput}
      type="file"
      multiple
      hidden
      accept={ACCEPTED_DOCUMENT_TYPES}
      on:change={(event) => {
        if (!uploadBusy && event.currentTarget.files?.length) {
          dispatch("uploadDocuments", event.currentTarget.files);
          event.currentTarget.value = "";
        }
      }}
    />
    <button class="button" type="button" disabled={uploadBusy} aria-describedby="project-upload-status" on:click={() => uploadInput?.click()}>
      <Icon name="upload" size={15} /> {uploadBusy ? "Uploading…" : "Upload"}
    </button>
    <span id="project-upload-status" class="dp-sr-only" role="status" aria-live="polite">
      {uploadBusy ? "Uploading project documents. Please wait before selecting more files." : ""}
    </span>

    <select bind:value={selectedDocumentId} disabled={!availableDocuments.length}>
      <option value="">Add existing document...</option>
      {#each availableDocuments as document (document.id)}
        <option value={document.id}>{document.filename}</option>
      {/each}
    </select>
    <button class="button" type="button" disabled={!selectedDocumentId} on:click={addSelectedDocument}>Add</button>

    <span class="dp-toolbar-spacer"></span>

    {#if !run || ["completed", "completed_with_errors", "cancelled"].includes(runStatus)}
      <button class="button" type="button" disabled={!project.document_count} on:click={() => dispatch("runProject", project.id)}>
        <Icon name="play" size={15} /> Run
      </button>
    {:else if runStatus === "paused"}
      <button class="button" type="button" on:click={() => dispatch("controlRun", { runId: run.id, action: "resume" })}>
        <Icon name="play" size={15} /> Resume
      </button>
      <button class="button danger" type="button" on:click={() => dispatch("controlRun", { runId: run.id, action: "stop" })}>
        <Icon name="stop" size={15} /> Stop
      </button>
    {:else}
      <button class="button" type="button" on:click={() => dispatch("controlRun", { runId: run.id, action: "pause" })}>
        <Icon name="pause" size={15} /> Pause
      </button>
      <button class="button danger" type="button" on:click={() => dispatch("controlRun", { runId: run.id, action: "stop" })}>
        <Icon name="stop" size={15} /> Stop
      </button>
    {/if}
  </div>

  {#if run && activeRun}
    <div class="dp-run-progress">
      <span style={`width:${Number(run.progress || 0)}%`}></span>
    </div>
  {/if}

  <div class="dp-document-list">
    {#if project.documents.length}
      {#each project.documents as entry, index (entry.document_id)}
        {@const displayDoc = displayDocument(entry.document, entry.document_id)}
        {#if displayDoc}
          <DocumentCard
            document={displayDoc}
            {index}
            showDelete={false}
            removable={true}
            canMoveUp={index > 0}
            canMoveDown={index < project.documents.length - 1}
            on:moveUp={(event) => dispatch("moveDocument", { documentId: event.detail, offset: -1 })}
            on:moveDown={(event) => dispatch("moveDocument", { documentId: event.detail, offset: 1 })}
            on:remove={(event) => dispatch("removeDocument", event.detail)}
            on:crop={(event) => dispatch("crop", event.detail)}
            on:reviewImages={(event) => dispatch("reviewImages", event.detail)}
            on:preview={(event) => dispatch("preview", event.detail)}
            on:pdfPreview={(event) => dispatch("pdfPreview", event.detail)}
            on:export={(event) => dispatch("export", event.detail)}
            on:delete={(event) => dispatch("delete", event.detail)}
            on:resume={(event) => dispatch("resume", event.detail)}
            on:pause={(event) => dispatch("pause", event.detail)}
            on:stop={(event) => dispatch("stop", event.detail)}
          />
        {/if}
      {/each}
    {:else}
      <EmptyState
        icon="folder"
        title="No project documents"
        description="Upload source files or add existing library documents to prepare this run."
      />
    {/if}
  </div>
</section>
