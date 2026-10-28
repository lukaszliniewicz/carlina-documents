<script>
  import { onDestroy, onMount } from "svelte";

  export let models = [];
  export let modelId = "";
  export let slot = "default";
  export let label = "Reasoning effort";
  export let disabled = false;
  export let onerror = () => {};
  export let onbusychange = () => {};
  export let signal = null;

  let slots = {};
  let selectedLevel = "default";
  let lastConfigKey = "";
  let saving = false;
  const lifecycleController = new AbortController();
  let mounted = true;
  let removeParentAbort = () => {};

  $: model = models.find((candidate) => candidate.id === modelId);
  $: selectId = `translator-reasoning-${slot.replace(/[^a-z0-9_-]/gi, "-")}`;
  $: levels = model?.supports_reasoning && Array.isArray(model.supported_reasoning_levels)
    ? model.supported_reasoning_levels
    : [];
  $: saved = slots[slot];
  $: configKey = `${modelId}:${saved?.model_id || ""}:${saved?.level || ""}:${levels.join(",")}`;
  $: if (configKey !== lastConfigKey) {
    lastConfigKey = configKey;
    selectedLevel = saved?.model_id === modelId && levels.includes(saved.level) ? saved.level : "default";
  }

  const levelLabel = (level) => level === "xhigh" ? "Extra high" : `${level.charAt(0).toUpperCase()}${level.slice(1)}`;

  onMount(() => {
    if (signal) {
      if (signal.aborted) lifecycleController.abort();
      else {
        const abort = () => lifecycleController.abort();
        signal.addEventListener("abort", abort, { once: true });
        removeParentAbort = () => signal.removeEventListener("abort", abort);
      }
    }
    void load();
  });

  onDestroy(() => {
    mounted = false;
    lifecycleController.abort();
    removeParentAbort();
  });

  async function load() {
    try {
      const response = await fetch("/api/llm/addon-settings/translator/reasoning", {
        credentials: "same-origin",
        signal: lifecycleController.signal
      });
      if (!response.ok) throw new Error("Unable to load reasoning settings.");
      const payload = await response.json();
      if (mounted) slots = payload?.slots || {};
    } catch (error) {
      if (mounted && error?.name !== "AbortError") {
        console.warn("Unable to load Translator reasoning settings.", error);
        onerror("Unable to load reasoning settings. Please retry.");
      }
    }
  }

  async function save() {
    if (!mounted || lifecycleController.signal.aborted || saving) return;
    const previousSlots = slots;
    const record = { model_id: modelId, level: selectedLevel };
    slots = { ...slots, [slot]: record };
    onerror("");
    saving = true;
    onbusychange(true);
    try {
      const response = await fetch("/api/llm/addon-settings/translator/reasoning", {
        method: "PUT",
        credentials: "same-origin",
        signal: lifecycleController.signal,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slots: { [slot]: record } })
      });
      if (!response.ok) throw new Error((await response.json()).error || "Unable to save reasoning settings.");
      const payload = await response.json();
      if (mounted) slots = payload?.slots || slots;
    } catch (error) {
      if (mounted && error?.name !== "AbortError") {
        slots = previousSlots;
        onerror(error?.message || "Unable to save reasoning settings. Please retry.");
      }
    } finally {
      saving = false;
      if (mounted) onbusychange(false);
    }
  }
</script>

{#if levels.length}
  <div class="reasoning-control">
    <label for={selectId}>{label}</label>
    <select id={selectId} bind:value={selectedLevel} disabled={disabled || saving} on:change={save}>
      <option value="default">Model default</option>
      {#each levels as level}
        <option value={level}>{levelLabel(level)}</option>
      {/each}
    </select>
  </div>
{:else if model?.supports_reasoning}
  <p class="reasoning-metadata-note">
    This model supports reasoning, but its configurable levels are not recorded. The provider default will be used.
  </p>
{/if}

<style>
  .reasoning-control { display:grid; gap:.25rem; margin-top:.2rem; }
  label { color:var(--color-ink-muted,#69747a); font-size:.78rem; }
  select { min-width:0; border:1px solid var(--color-rule,#d0c8bc); border-radius:.5rem; padding:.55rem .6rem; background:var(--color-surface,#faf8f3); color:var(--color-ink,#24211e); font:inherit; font-size:.88rem; }
  select:disabled { opacity:.58; cursor:not-allowed; }
  .reasoning-metadata-note { margin:.2rem 0 0; color:var(--color-ink-muted,#69747a); font-size:.78rem; line-height:1.45; }
</style>
