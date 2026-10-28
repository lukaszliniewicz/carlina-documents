<script lang="ts">
  import type { Snippet } from "svelte";

  interface Props {
    children: Snippet;
  }

  let { children }: Props = $props();
</script>

<div id="overlay-host" class="overlay-host">
  {@render children()}
</div>

<style>
  .overlay-host {
    position: relative;
    z-index: 100;
  }
</style>
