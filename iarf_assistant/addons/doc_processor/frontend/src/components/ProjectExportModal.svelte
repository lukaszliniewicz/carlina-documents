<script lang="ts">
  import { EpubImageFormat, Icon, prepareDownload, type EpubImageMode } from "@carlina/ui";
  import { createEventDispatcher, onDestroy } from "svelte";
  import { requestJson, waitForJob } from "../lib/api";
  import BaseModal from "./BaseModal.svelte";

  export let open = false;
  export let projectId: string | null = null;

  const dispatch = createEventDispatcher<{ close: void }>();
  let mode: "concatenated" | "separate" = "concatenated";
  let format: "md" | "html" | "epub" | "docx" | "pdf" = "md";
  let startH2OnNewPage = false;
  let imageMode: EpubImageMode = "jpeg";
  let busy = false;
  let status = "";
  let error = "";
  let prepared: Awaited<ReturnType<typeof prepareDownload>> | null = null;
  const controller = new AbortController();
  onDestroy(() => { controller.abort(); prepared?.dispose(); });

  $: if (format === "epub" && mode === "concatenated") {
    mode = "separate";
  }

  async function submit() {
    if (!projectId || busy) {
      return;
    }
    let url = `/addons/doc_processor/api/projects/${projectId}/export?format=${format}&mode=${mode}`;
    if (format === "epub") {
      url += `&epub_image_mode=${imageMode}`;
    }
    if ((format === "docx" || format === "pdf") && startH2OnNewPage) url += "&start_h2_on_new_page=true";
    if (format !== "epub" && format !== "docx" && format !== "pdf") {
      window.open(url, "_blank", "noopener,noreferrer");
      dispatch("close");
      return;
    }
    busy = true;
    prepared?.dispose();
    prepared = null;
    const requestedFormat = format;
    const requestedMode = mode;
    status = `Queued ${format.toUpperCase()} export...`;
    error = "";
    try {
      const payload = await requestJson<{ job_id: string; download_url?: string }>(url, { headers: { Accept: "application/json" } });
      const completed = await waitForJob(payload.job_id, (jobStatus) => {
        status = jobStatus.message || `Exporting ${format.toUpperCase()}...`;
      });
      const downloadUrl = completed.result?.download_url || payload.download_url;
      if (!downloadUrl) {
        throw new Error(`${format.toUpperCase()} export completed without a download URL.`);
      }
      const extension = requestedMode === "separate" ? "zip" : requestedFormat;
      prepared = await prepareDownload(downloadUrl, `Project-${projectId}.${extension}`, { signal: controller.signal });
      status = "Export ready. Select Download prepared document to save it.";
    } catch (err) {
      error = err instanceof Error ? err.message : "Failed to export project.";
    } finally {
      busy = false;
    }
  }
</script>

<BaseModal {open} title="Export Project" size="small" on:close={() => dispatch("close")}>
  <form class="dp-export-form" on:submit|preventDefault={submit}>
    <fieldset>
      <legend>Mode</legend>
      <label><input type="radio" bind:group={mode} value="concatenated" disabled={format === "epub"} /> Concatenated file</label>
      <label><input type="radio" bind:group={mode} value="separate" /> Separated files</label>
    </fieldset>
    <fieldset>
      <legend>Format</legend>
      <label><input type="radio" bind:group={format} value="md" /> Markdown (.md)</label>
      <label><input type="radio" bind:group={format} value="html" /> Semantic HTML (.html)</label>
      <label><input type="radio" bind:group={format} value="epub" /> eBook (.epub)</label>
      <label><input type="radio" bind:group={format} value="docx" /> Word Document (.docx)</label>
      <label><input type="radio" bind:group={format} value="pdf" /> PDF (.pdf)</label>
    </fieldset>
    {#if format === "epub"}
      <EpubImageFormat bind:mode={imageMode} {busy} />
    {/if}
    {#if format === "docx" || format === "pdf"}
      <fieldset disabled={busy}>
        <legend>Page layout</legend>
        <label><input type="checkbox" bind:checked={startH2OnNewPage} /> Start H2 headings on a new page</label>
        <p class="dp-muted">Useful when H2 marks chapters or letters.</p>
      </fieldset>
    {/if}
    {#if status}<p class="dp-muted">{status}</p>{/if}
    {#if prepared}<a href={prepared.url} download={prepared.filename}>Download prepared document</a>{/if}
    {#if error}<div class="dp-notice error">{error}</div>{/if}
  </form>

  <div slot="footer" class="dp-modal-footer">
    <button class="button ghost" type="button" disabled={busy} on:click={() => dispatch("close")}>Cancel</button>
    <button class="button" type="button" disabled={busy} on:click={submit}>
      {#if busy}<Icon name="refresh" size={15} class="dp-spin" />{:else}<Icon name="download" size={15} />{/if}
      Download
    </button>
  </div>
</BaseModal>
