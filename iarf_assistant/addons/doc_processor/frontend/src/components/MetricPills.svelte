<script lang="ts">
  import type { DocumentSettings } from "../lib/types";
  import { formatDuration, formatModelsUsed } from "../lib/utils";

  export let settings: DocumentSettings | null | undefined = null;

  $: metrics = settings?.metrics;
  $: duration = formatDuration(metrics?.duration_seconds);
  $: cost = Number(metrics?.cost || 0);
  $: promptTokens = Number(metrics?.prompt_tokens || 0);
  $: completionTokens = Number(metrics?.completion_tokens || 0);
  $: cachedTokens = Number(metrics?.cached_prompt_tokens || 0);
  $: failedPages = Array.isArray(metrics?.failed_pages) ? metrics?.failed_pages.length : 0;
  $: boundaryReviews = Number(metrics?.boundary_llm_reviews || 0);
  $: modelsUsed = formatModelsUsed(settings);
</script>

<span class="dp-metrics">
  {#if duration}<span title="Processing duration"><i class="fa-regular fa-clock"></i> {duration}</span>{/if}
  {#if cost > 0}<span title="LLM token cost" class="cost"><i class="fa-solid fa-sack-dollar"></i> ${cost.toFixed(4)}</span>{/if}
  {#if promptTokens > 0 || completionTokens > 0}<span title="Tokens"><i class="fa-solid fa-coins"></i> In: {promptTokens.toLocaleString()} | Out: {completionTokens.toLocaleString()}</span>{/if}
  {#if cachedTokens > 0}<span title="Prompt-cache hit tokens" class="cached"><i class="fa-solid fa-bolt"></i> Cached: {cachedTokens.toLocaleString()}</span>{/if}
  {#if boundaryReviews > 0}<span title="Boundaries reviewed by LLM" class="boundary"><i class="fa-solid fa-link"></i> Boundary: {boundaryReviews}</span>{/if}
  {#if failedPages > 0}<span title="Pages that failed because of filters or transient extraction errors" class="failed"><i class="fa-solid fa-triangle-exclamation"></i> {failedPages} Pg Failed</span>{/if}
  {#if modelsUsed}<span title={modelsUsed} class="models"><i class="fa-solid fa-microchip"></i> Models used</span>{/if}
</span>
