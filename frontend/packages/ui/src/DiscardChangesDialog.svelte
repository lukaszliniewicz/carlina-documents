<script lang="ts">
  import Dialog from "./Dialog.svelte";
  import Button from "./Button.svelte";
  let { onresolve }: { onresolve: (discard: boolean) => void } = $props();
</script>

<Dialog open={true} title="Discard unsaved changes?" size="sm" onclose={() => onresolve(false)}>
  <p>Your latest changes have not been saved. Keep editing to preserve them, or discard them to continue.</p>
  {#snippet footer()}
    <Button variant="outline" onclick={() => onresolve(false)}>Keep editing</Button>
    <Button variant="danger" onclick={() => onresolve(true)}>Discard changes</Button>
  {/snippet}
</Dialog>
