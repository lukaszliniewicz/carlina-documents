<script lang="ts">
  import { Icon } from "@carlina/ui";
  import { createEventDispatcher } from "svelte";

  export let saving = false;

  const dispatch = createEventDispatcher<{ open: void }>();
</script>

<details class="dp-sidebar-section">
  <summary><Icon name="code" size={16} /> Prompts</summary>
  <p class="dp-muted">
    Edit prompt fragments in a full-size modal, preview the resolved extraction prompt, and reset fields back to defaults.
  </p>
  <button class="button" type="button" disabled={saving} on:click={() => dispatch("open")}>
    {#if saving}<Icon name="refresh" size={15} class="dp-spin" />{:else}<Icon name="arrow-up-right" size={15} />{/if}
    Open Prompts
  </button>
</details>
