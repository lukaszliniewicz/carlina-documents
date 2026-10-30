<script lang="ts">
  import { Icon } from "@carlina/ui";
  import { createEventDispatcher } from "svelte";
  import { getSourceFileUrl } from "../lib/api";
  import BaseModal from "./BaseModal.svelte";
  import PdfSourceViewer from "./PdfSourceViewer.svelte";

  export let open = false;
  export let documentId: string | null = null;
  export let filename: string = "";

  const dispatch = createEventDispatcher<{ close: void }>();

  $: pdfUrl = documentId ? getSourceFileUrl(documentId, "inline") : "";
  $: downloadUrl = documentId ? getSourceFileUrl(documentId, "attachment") : "";
</script>

<BaseModal {open} title={filename || "Source PDF"} size="full" on:close={() => dispatch("close")}>
  <div class="dp-pdf-modal">
    <div class="dp-pdf-toolbar">
      {#if downloadUrl}
        <a class="dp-icon-btn" href={downloadUrl} download={filename || "document.pdf"} aria-label="Download original PDF" title="Download original PDF">
          <Icon name="download" size={16} />
        </a>
      {/if}
      <span class="dp-muted">This is a read-only preview of the original source file.</span>
    </div>
    {#if pdfUrl}
      <PdfSourceViewer documentId={documentId || ""} {filename} />
    {:else}
      <div class="dp-empty-state">
        <Icon name="file" size={20} />
        <span>No source file available.</span>
      </div>
    {/if}
  </div>
</BaseModal>
