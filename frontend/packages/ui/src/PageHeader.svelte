<script lang="ts">
  import type { Snippet } from "svelte";

  interface Props {
    eyebrow: string;
    title: string;
    description: string;
    actions?: Snippet;
  }

  let { eyebrow, title, description, actions }: Props = $props();
</script>

<header>
  <div class="heading">
    <p class="eyebrow">{eyebrow}</p>
    <h1>{title}</h1>
    <p class="description">{description}</p>
  </div>
  {#if actions}
    <div class="actions">{@render actions()}</div>
  {/if}
</header>

<style>
  header {
    display: flex;
    align-items: end;
    justify-content: space-between;
    gap: 2rem;
    border-bottom: 1px solid var(--color-rule-strong);
    padding-bottom: clamp(1.25rem, 3vw, 2rem);
  }

  .heading {
    max-width: 46rem;
  }

  .eyebrow {
    margin: 0 0 0.65rem;
    color: var(--color-clay);
    font-size: 0.66rem;
    font-weight: 800;
    letter-spacing: 0.18em;
    text-transform: uppercase;
  }

  h1 {
    margin: 0;
    font-family: var(--font-editorial);
    font-size: clamp(2.05rem, 5vw, 3.75rem);
    font-weight: 450;
    letter-spacing: -0.035em;
    line-height: 0.98;
  }

  .description {
    max-width: 42rem;
    margin: 1rem 0 0;
    color: var(--color-ink-muted);
    font-size: clamp(0.88rem, 1.4vw, 1rem);
    line-height: 1.65;
  }

  .actions {
    display: flex;
    flex: 0 0 auto;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 0.55rem;
  }

  @media (max-width: 42rem) {
    header {
      align-items: stretch;
      flex-direction: column;
      gap: 1.25rem;
    }

    .actions {
      justify-content: flex-start;
    }
  }
</style>
