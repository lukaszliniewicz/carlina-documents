<script lang="ts">
  import type { EpubImageMode, EpubOptions, EpubPreflight, EpubQuality } from "./epub.ts";
  import { requireEpubPreflight } from "./epub.ts";

  interface Props {
    options?: EpubOptions;
    preflight?: EpubPreflight | null;
    quality?: EpubQuality | null;
    busy?: boolean;
    imageMode?: EpubImageMode;
    onreview: (options: EpubOptions) => Promise<EpubPreflight>;
    oninspect?: (anchor: string) => void;
  }
  let { options = $bindable({}), preflight = null, quality = null, busy = false, imageMode = "auto", onreview, oninspect }: Props = $props();
  let reviewing = $state(false);
  let error = $state("");
  let reviewedChoices = $state("");
  let report = $derived(quality || preflight?.quality);
  let stale = $derived(Boolean(preflight) && JSON.stringify(options) !== reviewedChoices);
  $effect(() => { if (preflight) reviewedChoices = JSON.stringify(preflight.options); });
  const roles = ["cover", "titlepage", "copyright-page", "toc", "contributors", "introduction", "part", "chapter", "endnotes", "appendix", "bibliography", "backmatter", "bodymatter"];
  const roleNames: Record<string, string> = {
    cover: "Cover", titlepage: "Title page", "copyright-page": "Copyright page", toc: "Printed contents",
    contributors: "Contributors", introduction: "Introduction", part: "Part", chapter: "Chapter",
    endnotes: "Endnotes", appendix: "Appendix", bibliography: "Bibliography", backmatter: "Other back matter", bodymatter: "Main text",
  };
  const messages: Record<string, string> = {
    "structure-needs-review": "Choose the chapter heading level or review the publication roles.",
    "bodymatter-needs-review": "Mark where the main text begins so Start reading opens the right section.",
    "notes-heading-needs-review": "Review the heading that introduces the collected notes.",
    "note-ownership-needs-review": "This note has no single chapter owner. Its source position is retained.",
    "note-arrangement-needs-review": "The source note arrangement is uncertain. Review placement before publication.",
    "chapter-notes-needs-level": "Choose a chapter heading level before moving notes to chapter ends.",
    "document-notes-needs-review": "Some notes are outside the collected endnotes. Review their placement.",
    "note-reference-semantics": "Review a link to a note that is missing note-reference semantics.",
    "note-unreferenced": "This note has no incoming reference. Check it against the source.",
    "image-description-missing": "Add a useful image description or mark the image as decorative.",
    "image-description-review": "Review whether this description conveys the image’s meaning.",
    "long-description-review": "Review the full description or diagram topology for equivalent information, separately from the short alt text.",
    "ace-findings": "Ace found accessibility issues in this review draft. Resolve them and run the required publishing check before release. A completed manual accessibility evaluation may also be needed.",
    "table-headers-missing": "Review the table’s header row or column.",
    "page-label-context": "Page navigation currently uses source PDF positions. Supply printed labels when known.",
    "language-needs-review": "The publication language is unknown. Correct the book details.",
    "generated-label-language": "Generated navigation labels use English for this edition’s language.",
    "accessibility-evaluation-stale": "The accessibility evaluation describes a different edition. Its claims have been omitted.",
    "wrapper-description-needs-review": "A description referenced from a document wrapper is outside this reading section. Review its accessible description.",
    "content-document-oversized": "A large source element remains intact. Check loading and navigation in the intended readers.",
  };
  function choose(key: keyof EpubOptions, value: unknown) {
    options = { ...options, [key]: value, accessibility_evaluation: null };
  }
  function reviewItem(key: "roles" | "page_labels" | "table_headers" | "image_descriptions", id: string, value: string) {
    const mapping = { ...options[key] };
    if (value.trim()) mapping[id] = value;
    else delete mapping[id];
    choose(key, mapping);
  }
  function decorative(id: string, checked: boolean) {
    const images = new Set(options.decorative_images || []);
    if (checked) images.add(id); else images.delete(id);
    const descriptions = { ...options.image_descriptions };
    if (checked) delete descriptions[id];
    options = { ...options, decorative_images: [...images], image_descriptions: descriptions, accessibility_evaluation: null };
  }
  function confirmLong(id: string, digest: string, checked: boolean) {
    const reviews = { ...options.long_descriptions };
    if (checked) reviews[id] = digest; else delete reviews[id];
    choose("long_descriptions", reviews);
  }
  function photograph(id: string, checked: boolean) {
    const images = new Set(options.photographic_images || []);
    if (checked) images.add(id); else images.delete(id);
    choose("photographic_images", [...images]);
  }
  function omit(id: string, digest: string, checked: boolean) {
    const omitted = { ...options.omitted_front_matter };
    const edits = { ...options.front_matter_edits };
    if (checked) {
      const unit = preflight?.outline.front_matter?.find((item) => item.id === id);
      for (const blockId of [id, ...(unit?.blocks.map((block) => block.id) || [])]) delete edits[blockId];
    }
    if (checked) omitted[id] = digest; else delete omitted[id];
    options = { ...options, omitted_front_matter: omitted, front_matter_edits: edits, front_matter_order: [], accessibility_evaluation: null };
  }
  function editFront(id: string, digest: string, sourceText: string, text: string) {
    const edits = { ...options.front_matter_edits };
    if (text === sourceText) delete edits[id]; else if (text.trim()) edits[id] = { sha256: digest, text };
    choose("front_matter_edits", edits);
  }
  function startFrontSection(id: string, digest: string, role: string) {
    const breaks = { ...options.front_matter_breaks };
    if (role) breaks[id] = { sha256: digest, role }; else delete breaks[id];
    const units = new Set(frontUnits.map((unit) => unit.id));
    const omitted = Object.fromEntries(Object.entries(options.omitted_front_matter || {}).filter(([key]) => !units.has(key) && key !== id));
    options = { ...options, front_matter_breaks: breaks, front_matter_order: [], omitted_front_matter: omitted, accessibility_evaluation: null };
  }
  function moveFront(id: string, change: number) {
    const order = options.front_matter_order?.length ? [...options.front_matter_order] : (preflight?.outline.front_matter || []).filter((unit) => !options.omitted_front_matter?.[unit.id]).map((unit) => unit.id);
    const index = order.indexOf(id);
    if (index < 0 || index + change < 0 || index + change >= order.length) return;
    [order[index], order[index + change]] = [order[index + change], order[index]];
    choose("front_matter_order", order);
  }
  let frontUnits = $derived([...(preflight?.outline.front_matter || [])].sort((a, b) => options.front_matter_order?.length ? options.front_matter_order.indexOf(a.id) - options.front_matter_order.indexOf(b.id) : 0));
  async function review() {
    if (reviewing || busy) return;
    reviewing = true;
    error = "";
    try {
      const result = requireEpubPreflight(await onreview(options));
      options = result.options;
      reviewedChoices = JSON.stringify(result.options);
    } catch (caught) {
      error = caught instanceof Error ? caught.message : "Unable to review this edition.";
    } finally { reviewing = false; }
  }
  async function resetChoices() {
    options = {};
    await review();
  }
  function checkLabel(state: string | undefined) {
    return state === "passed" ? "Passed" : state === "failed" ? "Failed" : state === "reviewed" ? "Recorded evaluation" : state === "not_reviewed" ? "Needs human evaluation" : "Not run";
  }
</script>

<section class="epub-review" aria-label="EPUB publication review">
  <fieldset disabled={busy || reviewing}>
    <legend>Publication structure</legend>
    {#if options.profile === "publishing-v1"}<p class="hint">These saved choices use the earlier layout.</p><button type="button" onclick={() => choose("profile", "publishing-v2")}>Use improved publishing layout</button>{/if}
    <label>Chapter headings
      <select value={options.chapter_level ?? ""} onchange={(event) => choose("chapter_level", event.currentTarget.value ? Number(event.currentTarget.value) : null)}>
        <option value="">Follow reviewed source structure{preflight?.suggested_chapter_level ? ` (H${preflight.suggested_chapter_level})` : ""}</option>
        {#each [1, 2, 3, 4, 5, 6] as level}<option value={level}>H{level}</option>{/each}
      </select>
    </label>
    <label>Note placement
      <select value={options.note_placement || "source"} onchange={(event) => choose("note_placement", event.currentTarget.value)}>
        <option value="source">Follow the source arrangement</option>
        <option value="chapter">Place owned notes at chapter ends</option>
        <option value="document">Keep collected endnotes</option>
      </select>
    </label>
    <label class="check"><input type="checkbox" checked={options.group_endnotes ?? true} onchange={(event) => choose("group_endnotes", event.currentTarget.checked)} /> Group collected notes by chapter</label>
    <label>Reading direction
      <select value={options.direction || "source"} onchange={(event) => choose("direction", event.currentTarget.value)}>
        <option value="source">Follow source and edition language</option><option value="ltr">Left to right</option><option value="rtl">Right to left</option>
      </select>
    </label>
    <p class="hint">The source text and note labels are preserved. Uncertain structure remains available for review.</p>
    <button type="button" onclick={review}>{reviewing ? "Reviewing…" : stale ? "Review changed choices" : "Review EPUB"}</button>
    {#if preflight || Object.keys(options).length}<button type="button" onclick={resetChoices}>Reset choices and review current edition</button>{/if}
  </fieldset>
  {#if error}<p role="alert">{error}</p>{/if}
  {#if report}
    <div role="status" class="summary">
      <p>{quality ? "Prepared edition" : "Preflight"}: {report.issue_count} {report.issue_count === 1 ? "item" : "items"} to review.</p>
      {#if report.image_bytes}<p>Images: {(report.image_bytes.source / 1048576).toFixed(2)} MB source → {(report.image_bytes.packaged / 1048576).toFixed(2)} MB packaged.</p>{/if}
      <dl>
        <div><dt>Archive structure</dt><dd>{checkLabel(report.checks.structure)}</dd></div>
        <div><dt>EPUBCheck{report.epubcheck_version ? ` ${report.epubcheck_version}` : ""}</dt><dd>{checkLabel(report.checks.epubcheck)}</dd></div>
        <div><dt>Ace{report.ace_version ? ` ${report.ace_version}` : ""}</dt><dd>{checkLabel(report.checks.ace)}</dd></div>
        <div><dt>Accessibility evaluation</dt><dd>{checkLabel(report.checks.manual)}</dd></div>
      </dl>
      {#if stale}<p>Choices have changed. Review again before exporting.</p>{/if}
      <p class="hint">A clean archive check confirms EPUB structure. Publishing also needs editorial and accessibility review in reading systems.</p>
    </div>
    {#if report.issues.length}
      <details><summary>Warnings and review items ({report.issue_count})</summary>
        <ul>{#each report.issues as issue}
          <li><strong>{issue.severity === "warning" ? "Warning" : "Review"}:</strong> {messages[issue.code] || "Review this publication item."}
            {#if issue.target && oninspect && !preflight?.outline.images.some((image) => image.id === issue.target && image.is_selected_cover)}<button type="button" onclick={() => oninspect?.(issue.target)}>View affected section</button>{/if}
          </li>
        {/each}</ul>
        {#if report.issue_count > report.issues.length}<p>Only the first {report.issues.length} items are shown. Resolve these, then review again.</p>{/if}
      </details>
    {/if}
  {/if}
  {#if preflight}
    <fieldset disabled={busy || reviewing}>
      <legend>Review edition details</legend>
      {#if options.profile !== "publishing-v1"}
        <label>Image optimization<select disabled={imageMode !== "auto"} value={options.image_policy || "balanced"} onchange={(event) => choose("image_policy", event.currentTarget.value)}><option value="balanced">Balanced: optimize covers and confirmed photographs</option><option value="preserve">Preserve original image bytes</option></select></label>
        {#if imageMode !== "auto"}<p class="hint">The selected image format takes priority over this optimization policy.</p>{:else}<p class="hint">Balanced uses compatible JPEG with high-quality color detail. Diagrams, transparent images and unclassified interior images retain their original bytes.</p>{/if}
        <details><summary>Front matter ({frontUnits.length} sections)</summary>
          <p class="hint">Review repeated title and credit material here. Keep copyright, rights and source-art credits. Nothing is omitted automatically; linked content is protected.</p>
          <p class="hint">If the PDF has no copyright heading, choose where that section starts below. Changing section starts clears section omissions and order; review the new sections before arranging them.</p>
          {#if !frontUnits.length}<p class="hint">Mark the title page and main-text roles above, then review again to identify the front matter.</p>{/if}
          <div class="items">{#each frontUnits as unit}
            <div class="front-unit">
              <p>{roleNames[unit.role] || "Front matter"}: {unit.title}</p>
              <label class="check"><input type="checkbox" checked={Boolean(options.omitted_front_matter?.[unit.id])} onchange={(event) => omit(unit.id, unit.sha256, event.currentTarget.checked)} /> Omit this section from the EPUB</label>
              <div class="actions"><button type="button" disabled={Boolean(options.omitted_front_matter?.[unit.id])} onclick={() => moveFront(unit.id, -1)}>Move earlier</button><button type="button" disabled={Boolean(options.omitted_front_matter?.[unit.id])} onclick={() => moveFront(unit.id, 1)}>Move later</button>{#if oninspect}<button type="button" onclick={() => oninspect?.(unit.source_anchor || unit.id)}>View source</button>{/if}</div>
              {#each unit.blocks as block}
                {#if block.duplicate_of}<p class="hint">Repeated text also appears in another front matter block. Check whether both copies are needed.</p>{/if}
                {#if block.truncated}<p class="hint">Excerpt of a long paragraph. View the source before deciding whether to omit it. This preview cannot be edited.</p>{/if}
                {#if block.editable}<label>EPUB paragraph<textarea rows="3" maxlength="4096" disabled={Boolean(options.omitted_front_matter?.[unit.id] || options.omitted_front_matter?.[block.id])} value={options.front_matter_edits?.[block.id]?.text ?? block.text} onchange={(event) => editFront(block.id, block.sha256, block.text, event.currentTarget.value)}></textarea></label>{:else}<p>{block.text}</p>{/if}
                {#if block.can_start_section}<label>Start a section at this paragraph<select disabled={Boolean(options.omitted_front_matter?.[unit.id] || options.omitted_front_matter?.[block.id])} value={options.front_matter_breaks?.[block.id]?.role || ""} onchange={(event) => startFrontSection(block.id, block.sha256, event.currentTarget.value)}><option value="">Continue the current section</option><option value="copyright-page">Copyright page</option><option value="titlepage">Title page</option><option value="contributors">Contributors</option></select></label>{/if}
                {#if block.id !== unit.id}<label class="check"><input type="checkbox" disabled={Boolean(options.omitted_front_matter?.[unit.id] || options.front_matter_edits?.[block.id])} checked={Boolean(options.omitted_front_matter?.[block.id])} onchange={(event) => omit(block.id, block.sha256, event.currentTarget.checked)} /> Omit this paragraph</label>{/if}
              {/each}
            </div>
          {/each}</div>
        </details>
      {/if}
      <details><summary>Heading roles ({preflight.outline.headings.length})</summary>
        <div class="items">{#each preflight.outline.headings as heading}
          <label>H{heading.level} · {heading.title}
            <select value={options.roles?.[heading.id] || ""} onchange={(event) => reviewItem("roles", heading.id, event.currentTarget.value)}>
              <option value="">Follow source{heading.role ? ` (${roleNames[heading.role] || heading.role})` : ""}</option>
              {#each roles as role}<option value={role}>{roleNames[role]}</option>{/each}
            </select>
          </label>
        {/each}</div>
      </details>
      {#if preflight.outline.tables.length}<details><summary>Table headers ({preflight.outline.tables.length})</summary>
        <div class="items">{#each preflight.outline.tables as table, index}
          <label>{table.caption || `Table ${index + 1}`}<select value={options.table_headers?.[table.id] || ""} onchange={(event) => reviewItem("table_headers", table.id, event.currentTarget.value)}>
            <option value="">Keep source{table.has_headers ? " headers" : "; headers need review"}</option><option value="row">First column contains row headers</option><option value="column">First row contains column headers</option>
          </select></label>
        {/each}</div>
      </details>{/if}
      {#if preflight.outline.images.length}<details><summary>Image descriptions ({preflight.outline.images.length})</summary>
        <p class="hint">Use concise alt text here. Review each image in context, including any extended description in the document preview. Charts and diagrams need their essential data and relationships in accessible text.</p>
        <div class="items">{#each preflight.outline.images as image, index}
          <label>{image.is_cover ? "Cover alt text" : `Image ${index + 1} alt text`}<textarea rows="2" maxlength="4096" disabled={(options.decorative_images || []).includes(image.id)} value={options.image_descriptions?.[image.id] ?? image.alt} onchange={(event) => reviewItem("image_descriptions", image.id, event.currentTarget.value)}></textarea></label>
          {#if image.is_selected_cover}<p class="hint">Review the cover selected above. A replacement needs its own description or decorative-image decision.</p>{/if}
          <label class="check"><input type="checkbox" checked={(options.decorative_images || []).includes(image.id)} onchange={(event) => decorative(image.id, event.currentTarget.checked)} /> Confirm decorative or fully described in nearby text</label>
          {#if image.decorative}<p class="hint">The source marks this image as decorative. Confirm that choice or supply an informative description.</p>{/if}
          {#if oninspect && !image.is_selected_cover}<button type="button" onclick={() => oninspect?.(image.id)}>View source and full description</button>{/if}
          {#if image.alt && !options.image_descriptions?.[image.id]}<button type="button" onclick={() => reviewItem("image_descriptions", image.id, image.alt)}>Confirm this alt text</button>{/if}
          {#if options.profile !== "publishing-v1" && image.description_sha256}
            <label class="check"><input type="checkbox" checked={options.long_descriptions?.[image.id] === image.description_sha256} onchange={(event) => confirmLong(image.id, image.description_sha256 || "", event.currentTarget.checked)} /> I checked the full description or topology against the image and confirmed its meaning and relationships</label>
          {/if}
          {#if options.profile !== "publishing-v1" && !image.is_cover && !image.description_sha256}<label class="check"><input type="checkbox" checked={(options.photographic_images || []).includes(image.id)} onchange={(event) => photograph(image.id, event.currentTarget.checked)} /> This is a photograph or painting suitable for JPEG optimization</label>{/if}
        {/each}</div>
      </details>{/if}
      {#if preflight.outline.pages.length}<details><summary>Printed page labels ({preflight.outline.pages.length})</summary>
        <p class="hint">Enter the printed label only when checked against the source. Blank entries retain physical PDF positions.</p>
        <div class="items">{#each preflight.outline.pages as page, index}<label>Source position {page.label || index + 1}<input maxlength="128" value={options.page_labels?.[page.id] || ""} onchange={(event) => reviewItem("page_labels", page.id, event.currentTarget.value)} /></label>{/each}</div>
      </details>{/if}
    </fieldset>
  {/if}
</section>

<style>
  .epub-review { display: grid; gap: 0.75rem; min-width: 0; }
  fieldset { display: grid; gap: 0.75rem; min-width: 0; border: 1px solid var(--border-color, #8886); padding: 0.75rem; border-radius: 0.5rem; }
  label { display: grid; gap: 0.3rem; font-size: 0.9rem; }
  .check { display: flex; align-items: flex-start; gap: 0.5rem; }
  select, textarea, input:not([type="checkbox"]) { width: 100%; max-width: 100%; box-sizing: border-box; font: inherit; color: inherit; background: var(--surface-color, transparent); border: 1px solid var(--border-color, #8888); padding: 0.45rem; border-radius: 0.3rem; }
  button { font: inherit; color: inherit; cursor: pointer; padding: 0.4rem 0.7rem; border: 1px solid var(--border-color, #8888); border-radius: 0.3rem; background: var(--surface-color, transparent); }
  button:disabled { cursor: wait; opacity: 0.6; }
  button:focus-visible, select:focus-visible, input:focus-visible, textarea:focus-visible, summary:focus-visible { outline: 2px solid var(--accent-color, #3b82f6); outline-offset: 2px; }
  .hint { font-size: 0.85rem; color: var(--muted-color, inherit); margin: 0; }
  .summary { padding: 0.75rem; background: var(--surface-color, #8881); border-radius: 0.5rem; }
  .summary p { margin: 0 0 0.5rem; }
  dl { margin: 0.5rem 0; font-size: 0.85rem; display: grid; gap: 0.4rem; }
  dl div { display: flex; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
  dd { margin: 0; }
  summary { cursor: pointer; padding: 0.3rem 0; }
  li { margin-block: 0.75rem; }
  li button { display: block; margin-block-start: 0.3rem; font-size: 0.85rem; }
  .items { display: grid; gap: 0.75rem; max-height: 22rem; overflow: auto; padding: 0.4rem; }
  .front-unit { display: grid; gap: .65rem; padding-block: .75rem; border-block-end: 1px solid var(--border-color, #8886); }
  .front-unit p { margin: 0; }
  .actions { display: flex; flex-wrap: wrap; gap: .5rem; }
</style>
