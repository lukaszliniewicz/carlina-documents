<script lang="ts">
  import type { FrontendBootstrap } from "@carlina/sdk";
  import { Badge, Dialog, Icon } from "@carlina/ui";

  interface Props {
    open: boolean;
    bootstrap: FrontendBootstrap;
    onclose: () => void;
  }

  let { open, bootstrap, onclose }: Props = $props();
</script>

<Dialog
  {open}
  title="Notifications"
  description="A quiet place for system and workflow updates."
  onclose={onclose}
>
  <section class="notice">
    <span class="notice__icon"><Icon name="check" size={19} /></span>
    <div>
      <div class="notice__heading">
        <h3>You’re up to date</h3>
        <Badge tone="success">Ready</Badge>
      </div>
      <p>No new actions need your attention.</p>
    </div>
  </section>
  <section class="release-note">
    <p class="release-note__eyebrow">Interface</p>
    <h3>Editorial shell active</h3>
    <p>
      Build {bootstrap.build.core.id?.slice(0, 7) ?? "local"} is serving the
      new navigation foundation. Legacy destinations still open as complete
      pages during the migration.
    </p>
  </section>
</Dialog>

<style>
  .notice {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 0.9rem;
    border-block: 1px solid var(--color-rule);
    padding: 1rem 0;
  }

  .notice__icon {
    width: 2.35rem;
    height: 2.35rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-lichen);
    color: var(--color-lichen);
  }

  .notice__heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }

  h3 {
    margin: 0;
    font-family: var(--font-editorial);
    font-size: 1.1rem;
    font-weight: 550;
  }

  p {
    margin: 0.35rem 0 0;
    color: var(--color-ink-muted);
    font-size: 0.78rem;
    line-height: 1.55;
  }

  .release-note {
    margin-top: 1.5rem;
    border-left: 3px solid var(--color-clay);
    padding: 0.2rem 0 0.2rem 1rem;
  }

  .release-note__eyebrow {
    margin: 0 0 0.3rem;
    color: var(--color-clay);
    font-size: 0.58rem;
    font-weight: 800;
    letter-spacing: 0.15em;
    text-transform: uppercase;
  }
</style>
