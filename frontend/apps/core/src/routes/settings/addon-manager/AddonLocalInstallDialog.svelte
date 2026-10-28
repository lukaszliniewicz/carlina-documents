<script lang="ts">
  import type { FrontendAddonDependencyNotice } from "@carlina/sdk";
  import { Button, Dialog } from "@carlina/ui";

  interface Props {
    open: boolean;
    file: File | null;
    trustApproved: boolean;
    dependencyNotices: FrontendAddonDependencyNotice[];
    dependencyReviewApproved: boolean;
    pending: boolean;
    onclose: () => void;
    oninstall: () => void;
  }

  let {
    open,
    file = $bindable(),
    trustApproved = $bindable(),
    dependencyNotices = $bindable(),
    dependencyReviewApproved = $bindable(),
    pending,
    onclose,
    oninstall,
  }: Props = $props();
</script>

<Dialog
  {open}
  title="Install local addon ZIP"
  description="Upload a package obtained outside the configured catalog. Its archive and contract are validated locally before activation."
  {onclose}
  size="sm"
>
  <label class="package-picker" for="addon-local-package">
    <span>Addon ZIP</span>
    <input
      id="addon-local-package"
      name="addon_local_package"
      aria-label="Addon ZIP"
      type="file"
      accept=".zip,application/zip"
      onchange={(event) => {
        file = event.currentTarget.files?.[0] ?? null;
        dependencyNotices = [];
        dependencyReviewApproved = false;
      }}
    />
    <small>
      {file
        ? `${file.name} · ${(file.size / 1_048_576).toFixed(1)} MB`
        : "No package selected"}
    </small>
  </label>
  <p class="dialog-note">
    In-process runtimes and database migrations are trusted host code, not
    sandboxed plugins. They can access the same files, environment, network,
    and application data as Carlina.
  </p>
  <label class="trust-approval">
    <input type="checkbox" bind:checked={trustApproved} />
    <span>
      I reviewed the package source/publisher and approve full host-code access.
      Carlina will record its archive hash and declared permissions.
    </span>
  </label>
  {#if dependencyNotices.length}
    <section class="install-dependency-review">
      <h4>Optional dependency license review</h4>
      <p class="dialog-note">
        These packages are downloaded only if you continue. Review the upstream
        terms before enabling them.
      </p>
      <ul class="permission-list">
        {#each dependencyNotices as dependencyNotice}
          <li>
            <strong>{dependencyNotice.packages.join(", ")}</strong>
            <span>{dependencyNotice.license}</span>
            <span>{dependencyNotice.purpose}</span>
          </li>
        {/each}
      </ul>
      <label class="trust-approval">
        <input type="checkbox" bind:checked={dependencyReviewApproved} />
        <span>I reviewed these dependency licenses and approve their installation.</span>
      </label>
    </section>
  {/if}
  {#snippet footer()}
    <Button variant="quiet" disabled={pending} onclick={onclose}>Cancel</Button>
    <Button
      disabled={pending ||
        !file ||
        !trustApproved ||
        (dependencyNotices.length > 0 && !dependencyReviewApproved)}
      onclick={oninstall}
    >
      {pending ? "Preparing package…" : "Install and activate"}
    </Button>
  {/snippet}
</Dialog>
