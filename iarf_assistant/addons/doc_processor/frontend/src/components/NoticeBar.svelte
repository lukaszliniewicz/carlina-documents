<script lang="ts">
  import type { Notice } from "../lib/types";

  export let notice: Notice | null = null;
</script>

{#if notice}
  <div
    class="dp-notice {notice.kind}"
    role={notice.kind === "error" ? "alert" : "status"}
    aria-live={notice.kind === "error" ? "assertive" : "polite"}
  >
    {notice.message}
  </div>
{/if}
