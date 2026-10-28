<script>
  import WorkspaceApp from "./WorkspaceApp.svelte";

  export let context = null;
  export let bootstrapData = null;
</script>

<WorkspaceApp {context} {bootstrapData} />
