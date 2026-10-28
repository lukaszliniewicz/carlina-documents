<script lang="ts">
  import { Button, Dialog } from "@carlina/ui";

  import type { AddonActionConfirmation } from "../../../lib/addon-manager.ts";

  interface Props {
    action: AddonActionConfirmation | null;
    confirmation: string;
    pending: boolean;
    onclose: () => void;
    onconfirm: () => void;
  }

  let {
    action,
    confirmation = $bindable(),
    pending,
    onclose,
    onconfirm,
  }: Props = $props();
</script>

<Dialog
  open={Boolean(action)}
  title={action?.title ?? "Confirm operation"}
  description={action?.description}
  {onclose}
  size="sm"
>
  {#if action?.requiresIdentifier}
    <label class="typed-confirmation">
      <span>Type <code>{action.confirmationPhrase}</code> to confirm</span>
      <input
        type="text"
        autocomplete="off"
        spellcheck="false"
        bind:value={confirmation}
      />
    </label>
  {:else}
    <p class="dialog-note">
      Review the impact above before continuing. The activity will be recorded
      in the relevant system or addon log.
    </p>
  {/if}
  {#snippet footer()}
    <Button variant="quiet" disabled={pending} onclick={onclose}>Cancel</Button>
    <Button
      variant={action?.danger ? "danger" : "primary"}
      disabled={pending ||
        Boolean(
          action?.requiresIdentifier &&
            confirmation.trim() !== action.confirmationPhrase,
        )}
      onclick={onconfirm}
    >
      {pending ? "Working…" : action?.confirmLabel ?? "Continue"}
    </Button>
  {/snippet}
</Dialog>
