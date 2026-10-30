<script lang="ts">
  import { Icon } from "@carlina/ui";
  import { createEventDispatcher } from "svelte";
  import {
    canonicalDocumentMetadata,
    metadataFormFromDocument,
    metadataPayloadFromForm
  } from "../lib/documentMetadata";
  import type { EditableMetadataForm, ProcessedDocument } from "../lib/types";

  export let document: ProcessedDocument | null = null;
  export let saving = false;
  export let reextracting = false;

  const dispatch = createEventDispatcher<{ save: ReturnType<typeof metadataPayloadFromForm>; reextract: void; dirty: boolean }>();
  let form: EditableMetadataForm = metadataFormFromDocument(null);
  let initialForm: EditableMetadataForm = form;
  let lastDocument: ProcessedDocument | null = null;
  let reextractConfirmationOpen = false;

  $: if (document !== lastDocument) {
    lastDocument = document;
    form = metadataFormFromDocument(document);
    initialForm = form;
    reextractConfirmationOpen = false;
  }
  $: metadata = canonicalDocumentMetadata(document);
  $: metadataStale = document?.settings?.metadata_stale === true;
  $: changed = JSON.stringify(form) !== JSON.stringify(initialForm);
  $: dispatch("dirty", changed);
  $: headingStructure = metadata.heading_structure;

  function setField<K extends keyof EditableMetadataForm>(field: K, value: EditableMetadataForm[K]) {
    form = { ...form, [field]: value };
  }

  function cancel() {
    form = { ...initialForm };
  }

  function save() {
    if (!changed || saving || reextracting) return;
    dispatch("save", metadataPayloadFromForm(form));
  }

  function requestReextract() {
    if (!saving && !reextracting) reextractConfirmationOpen = true;
  }

  function confirmReextract() {
    if (saving || reextracting) return;
    reextractConfirmationOpen = false;
    dispatch("reextract");
  }

  function formatConfidence(value: number | undefined): string {
    return typeof value === "number" ? ` · ${Math.round(value * 100)}% confidence` : "";
  }

  function evidenceLabel(value: string | undefined): string {
    return value ? value.replaceAll("_", " ") : "Unclassified";
  }
</script>

<section class="dp-metadata-panel" aria-label="Document metadata">
  <header class="dp-metadata-panel-header">
    <div>
      <h3>Canonical metadata</h3>
      <p>Edit the publication record deliberately. Saving metadata does not alter extracted text, page selection, or semantic structure.</p>
    </div>
    {#if metadataStale}
      <span class="dp-status-badge warning" title="Text was edited after metadata extraction">Metadata may be stale</span>
    {/if}
  </header>

  <div class="dp-metadata-form" aria-describedby="dp-metadata-form-help">
    <p id="dp-metadata-form-help" class="dp-muted">Contributor names and subjects accept one entry per line; semicolons also separate entries. Commas are preserved inside names and subject labels.</p>
    <div class="dp-metadata-form-grid">
      <label><span>Title</span><input value={form.title} on:input={(event) => setField("title", event.currentTarget.value)} /></label>
      <label><span>Subtitle</span><input value={form.subtitle} on:input={(event) => setField("subtitle", event.currentTarget.value)} /></label>
      <label><span>Publication type</span><select value={form.publication_type} on:change={(event) => setField("publication_type", event.currentTarget.value)}><option value="">Not specified</option><option value="book">Book</option><option value="journal_article">Journal article</option><option value="article">Article</option><option value="chapter">Chapter</option><option value="report">Report</option><option value="thesis">Thesis</option><option value="newsletter">Newsletter</option><option value="magazine">Magazine</option><option value="proceedings">Proceedings</option><option value="pamphlet">Pamphlet</option><option value="letter">Letter</option><option value="unknown">Unknown</option>{#if form.publication_type && !["book", "journal_article", "article", "chapter", "report", "thesis", "newsletter", "magazine", "proceedings", "pamphlet", "letter", "unknown"].includes(form.publication_type)}<option value={form.publication_type}>{form.publication_type} (legacy value)</option>{/if}</select></label>
      <label><span>Language</span><input value={form.language} on:input={(event) => setField("language", event.currentTarget.value)} placeholder="en, pl, fr…" /></label>
      <label><span>Authors</span><textarea value={form.authors} on:input={(event) => setField("authors", event.currentTarget.value)}></textarea></label>
      <label><span>Editors</span><textarea value={form.editors} on:input={(event) => setField("editors", event.currentTarget.value)}></textarea></label>
      <label><span>Translators</span><textarea value={form.translators} on:input={(event) => setField("translators", event.currentTarget.value)}></textarea></label>
      <label><span>Publisher</span><input value={form.publisher} on:input={(event) => setField("publisher", event.currentTarget.value)} /></label>
      <label><span>Publication date</span><input value={form.publication_date} on:input={(event) => setField("publication_date", event.currentTarget.value)} placeholder="YYYY or printed date" /></label>
      <label><span>ISBN</span><input value={form.isbn} on:input={(event) => setField("isbn", event.currentTarget.value)} /></label>
      <label><span>ISSN</span><input value={form.issn} on:input={(event) => setField("issn", event.currentTarget.value)} /></label>
      <label><span>Journal or series</span><input value={form.journal_or_series} on:input={(event) => setField("journal_or_series", event.currentTarget.value)} /></label>
      <label><span>Volume</span><input value={form.volume} on:input={(event) => setField("volume", event.currentTarget.value)} /></label>
      <label><span>Issue</span><input value={form.issue} on:input={(event) => setField("issue", event.currentTarget.value)} /></label>
      <label><span>Rights</span><input value={form.rights} on:input={(event) => setField("rights", event.currentTarget.value)} /></label>
      <label><span>Source</span><input value={form.source} on:input={(event) => setField("source", event.currentTarget.value)} /></label>
      <label><span>Subjects</span><textarea value={form.subjects} on:input={(event) => setField("subjects", event.currentTarget.value)}></textarea></label>
      <label class="dp-metadata-wide"><span>Description</span><textarea value={form.description} on:input={(event) => setField("description", event.currentTarget.value)}></textarea></label>
    </div>
    <div class="dp-metadata-actions">
      <button class="button success" type="button" disabled={!changed || saving || reextracting} on:click={save}>
        <Icon name={saving ? "refresh" : "check"} size={15} class={saving ? "dp-spin" : ""} /> Save metadata
      </button>
      <button class="button ghost" type="button" disabled={!changed || saving || reextracting} on:click={cancel}>Cancel edits</button>
      <button class="button" type="button" disabled={saving || reextracting} on:click={requestReextract}>
        <Icon name="refresh" size={15} class={reextracting ? "dp-spin" : ""} /> {reextracting ? "Refreshing metadata…" : "Re-extract metadata"}
      </button>
    </div>
    {#if reextracting}<p class="dp-muted" role="status" aria-live="polite">Refreshing source metadata with this document’s saved metadata model. This may take a while; page recommendations remain read-only.</p>{/if}
  </div>

  {#if reextractConfirmationOpen}
    <div class="dp-metadata-confirmation" role="alert" aria-labelledby="dp-reextract-title" aria-describedby="dp-reextract-description">
      <strong id="dp-reextract-title">Refresh extracted metadata?</strong>
      <p id="dp-reextract-description">This explicitly asks the saved metadata workflow to refresh the record from the source. It may be slow. It will not restart document extraction or automatically delete suggested pages.</p>
      <div class="dp-metadata-actions"><button class="button ghost" type="button" on:click={() => (reextractConfirmationOpen = false)}>Cancel</button><button class="button" type="button" on:click={confirmReextract}><Icon name="refresh" size={15} /> Refresh metadata</button></div>
    </div>
  {/if}

  <section class="dp-metadata-evidence" aria-labelledby="dp-metadata-evidence-title">
    <header><h4 id="dp-metadata-evidence-title">Extraction evidence</h4><p>Read-only model evidence. Review it critically; it does not change the document or authorize page removal.</p></header>
    {#if metadata.table_of_contents?.length}
      <details open><summary>Table of contents ({metadata.table_of_contents.length})</summary><ol>{#each metadata.table_of_contents as entry, index (`${entry.title}:${index}`)}<li style={`margin-left:${Math.max(0, (entry.level || 1) - 1) * 1}rem`}>{entry.title}{#if entry.page_label || entry.page} <span class="dp-muted">— {entry.page_label || entry.page}</span>{/if}{#if entry.pdf_page} <span class="dp-muted">(PDF p. {entry.pdf_page})</span>{/if}</li>{/each}</ol></details>
    {/if}
    {#if metadata.pages_to_remove?.length}
      <details><summary>Suggested pages to remove ({metadata.pages_to_remove.length})</summary><p class="dp-muted">Recommendations only; no page is removed here.</p><ul>{#each metadata.pages_to_remove as entry (`${entry.page}:${entry.reason || ""}`)}<li>PDF p. {entry.page}: {entry.reason || "No reason supplied"}{entry.safe_to_remove ? " · marked safe by the model" : " · not marked safe"}{formatConfidence(entry.confidence)}</li>{/each}</ul></details>
    {/if}
    {#if metadata.extraction_notes?.length}
      <details><summary>Extraction notes ({metadata.extraction_notes.length})</summary><ul>{#each metadata.extraction_notes as note, index (`${note}:${index}`)}<li>{note}</li>{/each}</ul></details>
    {/if}
    {#if headingStructure}
      <details open><summary>Heading-structure evidence</summary><dl class="dp-metadata-evidence-list"><dt>Validation</dt><dd>{headingStructure.validation?.status || "Unknown"}</dd>{#if headingStructure.validation?.toc_only_roles?.length}<dt>TOC-only roles</dt><dd>{headingStructure.validation.toc_only_roles.join(", ")}</dd>{/if}{#if headingStructure.validation?.unverified_roles?.length}<dt>Unverified roles</dt><dd>{headingStructure.validation.unverified_roles.join(", ")}</dd>{/if}{#if headingStructure.validation?.invalid_relationships?.length}<dt>Unverified relationships</dt><dd>{headingStructure.validation.invalid_relationships.join(", ")}</dd>{/if}</dl>{#if headingStructure.roles?.length}<h5>Roles</h5><ul>{#each headingStructure.roles as role (role.role)}<li>{role.label || role.role} · H{role.html_level || "?"} · depth {role.depth ?? "?"}{role.extractor_ready === false ? " · needs verification" : ""}{formatConfidence(role.confidence)}</li>{/each}</ul>{/if}{#if headingStructure.running_matter?.length}<h5>Running matter observations</h5><ul>{#each headingStructure.running_matter as item, index (index)}<li><strong>{evidenceLabel(item.kind)}</strong>{item.position ? ` · ${evidenceLabel(item.position)}` : ""}{item.observation ? ` — ${item.observation}` : ""}{item.pages?.length ? ` · PDF pp. ${item.pages.join(", ")}` : ""}{item.examples?.length ? ` · examples: ${item.examples.join("; ")}` : ""}{formatConfidence(item.confidence)}</li>{/each}</ul>{/if}{#if headingStructure.uncertainties?.length}<h5>Uncertainties</h5><ul>{#each headingStructure.uncertainties as note, index (`${note}:${index}`)}<li>{note}</li>{/each}</ul>{/if}{#if headingStructure.document_specific_notes?.length}<h5>Document-specific notes</h5><ul>{#each headingStructure.document_specific_notes as note, index (index)}<li><strong>{evidenceLabel(note.kind)}</strong>{note.applies_to ? ` · ${evidenceLabel(note.applies_to)}` : ""}{note.observation ? ` — ${note.observation}` : ""}{note.examples?.length ? ` · examples: ${note.examples.join("; ")}` : ""}{formatConfidence(note.confidence)}</li>{/each}</ul>{/if}</details>
    {/if}
    {#if !metadata.table_of_contents?.length && !metadata.pages_to_remove?.length && !metadata.extraction_notes?.length && !headingStructure}<p class="dp-muted">No extraction evidence was stored for this document.</p>{/if}
  </section>
</section>
