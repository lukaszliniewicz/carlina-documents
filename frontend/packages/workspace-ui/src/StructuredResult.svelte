<script lang="ts">
  import StructuredResult from "./StructuredResult.svelte";

  let {
    value,
    depth = 0,
    label = "",
  }: { value: unknown; depth?: number; label?: string } = $props();

  const isObject = $derived(
    typeof value === "object" && value !== null && !Array.isArray(value),
  );
  const entries = $derived(
    isObject ? Object.entries(value as Record<string, unknown>) : [],
  );
  const title = $derived(
    label
      .replace(/[_-]+/g, " ")
      .replace(/\b\w/g, (letter) => letter.toUpperCase()),
  );
  const isUrl = $derived(
    typeof value === "string" && /^https?:\/\//i.test(value),
  );
  const valueIsUrl = (candidate: unknown) =>
    typeof candidate === "string" && /^https?:\/\//i.test(candidate);
</script>

{#if Array.isArray(value)}
  <section class="ws-result-group" data-depth={depth}>
    {#if label}<h4>{title} <span>{value.length}</span></h4>{/if}
    {#if value.length === 0}
      <p class="ws-empty-value">None</p>
    {:else}
      <div class="ws-result-list">
        {#each value as item, index}
          <StructuredResult value={item} depth={depth + 1} label={`Item ${index + 1}`} />
        {/each}
      </div>
    {/if}
  </section>
{:else if isObject}
  <section class="ws-result-group" data-depth={depth}>
    {#if label}<h4>{title}</h4>{/if}
    <dl class="ws-result-grid">
      {#each entries as [key, item]}
        {#if typeof item === "object" && item !== null}
          <div class="ws-result-nested">
            <StructuredResult value={item} depth={depth + 1} label={key} />
          </div>
        {:else}
          <div>
            <dt>{key.replace(/[_-]+/g, " ")}</dt>
            <dd>
              {#if valueIsUrl(item)}
                <a href={String(item)} target="_blank" rel="noreferrer">{String(item)}</a>
              {:else if typeof item === "boolean"}
                {item ? "Yes" : "No"}
              {:else if item === null || item === undefined || item === ""}
                <span class="ws-empty-value">Not provided</span>
              {:else}
                {String(item)}
              {/if}
            </dd>
          </div>
        {/if}
      {/each}
    </dl>
  </section>
{:else}
  <div class="ws-result-scalar">
    {#if label}<strong>{title}</strong>{/if}
    {#if isUrl}
      <a href={String(value)} target="_blank" rel="noreferrer">{String(value)}</a>
    {:else}
      <p>{value === null || value === undefined ? "Not provided" : String(value)}</p>
    {/if}
  </div>
{/if}
