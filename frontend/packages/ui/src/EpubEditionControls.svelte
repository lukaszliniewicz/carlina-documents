<script lang="ts">
  import type { EpubEdition } from "./epub.ts";
  interface Props { edition: EpubEdition | null; busy?: boolean; onload: () => Promise<void>; onsave: () => Promise<void>; onclear: () => Promise<void>; }
  let { edition, busy = false, onload, onsave, onclear }: Props = $props();
  let working = $state(false);
  let error = $state("");
  let status = $state("");
  async function act(action: () => Promise<void>, message: string) {
    if (busy || working) return;
    working = true; error = ""; status = "";
    try { await action(); status = message; }
    catch (caught) { error = caught instanceof Error ? caught.message : "Edition choices could not be saved."; }
    finally { working = false; }
  }
</script>

<fieldset disabled={busy || working} aria-label="Saved EPUB edition choices">
  <legend>Saved edition</legend>
  <p>Save these choices for this book’s next export. The extracted text stays intact.</p>
  {#if edition?.stale}<p role="status">{edition.current_source_revision === null ? "The source is not ready for export." : "The source changed."} Saved omissions, text edits, PDF crops and image reviews need a fresh review. General preferences are retained.</p>{/if}
  <div>
    <button type="button" onclick={() => act(onload, "Saved choices loaded.")}>Load saved choices</button>
    <button type="button" disabled={!edition} onclick={() => act(onsave, "Edition choices saved.")}>Save edition choices</button>
    {#if edition?.recipe}<button type="button" onclick={() => act(onclear, "Saved choices removed.")}>Forget saved choices</button>{/if}
  </div>
  {#if status}<p role="status">{status}</p>{/if}
  {#if error}<p role="alert">{error}</p>{/if}
</fieldset>

<style>
  fieldset { min-width: 0; border: 1px solid var(--border-color, #8888); padding: .75rem; border-radius: .5rem; }
  p { font-size: .85rem; }
  div { display: flex; flex-wrap: wrap; gap: .5rem; }
  button { font: inherit; padding: .4rem .7rem; cursor: pointer; }
  button:focus-visible { outline: 2px solid var(--accent-color, #2563eb); outline-offset: 2px; }
</style>
