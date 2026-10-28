<script lang="ts">
  import { onMount, untrack } from "svelte";
  import {
    createFrontendApiClient,
    type FrontendApiClient,
    type FrontendBootstrap,
  } from "@carlina/sdk";
  import { Icon } from "@carlina/ui";

  import AppShell from "./components/AppShell.svelte";

  interface Props {
    bootstrap?: FrontendBootstrap;
    bootstrapUrl?: string;
    api?: FrontendApiClient;
    initialPath?: string;
    label?: string;
  }

  let {
    bootstrap: suppliedBootstrap,
    bootstrapUrl = "/api/frontend/bootstrap",
    api: suppliedApi,
    initialPath,
  }: Props = $props();

  let bootstrap = $state<FrontendBootstrap | null>(
    untrack(() => suppliedBootstrap ?? null),
  );
  let api = $state<FrontendApiClient | null>(
    untrack(() => suppliedApi ?? null),
  );
  let error = $state("");
  let showBootstrapSkeleton = $state(false);

  onMount(() => {
    if (!api) {
      api = createFrontendApiClient(window.location.origin);
    }
    if (bootstrap) {
      return;
    }

    const skeletonTimer = window.setTimeout(() => {
      showBootstrapSkeleton = true;
    }, 180);
    const path = initialPath ?? `${window.location.pathname}${window.location.search}`;
    const url = new URL(bootstrapUrl, window.location.origin);
    url.searchParams.set("path", path);
    void api
      .request<FrontendBootstrap>(`${url.pathname}${url.search}`)
      .then((payload) => {
        bootstrap = payload;
      })
      .catch((reason: unknown) => {
        error = reason instanceof Error ? reason.message : "The application shell could not be loaded.";
      })
      .finally(() => {
        window.clearTimeout(skeletonTimer);
      });
    return () => window.clearTimeout(skeletonTimer);
  });

</script>

{#if bootstrap && api}
  <AppShell {bootstrap} {api} initialPath={initialPath ?? bootstrap.current_path} />
{:else if error}
  <main class="shell-failure">
    <span class="shell-failure__mark"><Icon name="activity" size={22} /></span>
    <p class="shell-failure__eyebrow">Frontend unavailable</p>
    <h1>The workspace could not open.</h1>
    <p>{error}</p>
    <button class="shell-failure__link" type="button" onclick={() => window.location.reload()}>
      Try loading the workspace again
    </button>
    <form method="post" action="/logout">
      <input
        type="hidden"
        name="csrf_token"
        value={document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content ?? ""}
      />
      <button class="shell-failure__link shell-failure__link--quiet" type="submit">
        Sign out
      </button>
    </form>
  </main>
{:else}
  {#if showBootstrapSkeleton}
    <div class="shell-skeleton" role="status" aria-live="polite" aria-label="Loading workspace">
      <aside aria-hidden="true">
        <span class="shell-skeleton__brand"></span>
        <span></span><span></span><span></span><span></span>
      </aside>
      <div>
        <header aria-hidden="true"><span></span><span></span></header>
        <main aria-hidden="true">
          <span class="shell-skeleton__line shell-skeleton__line--short"></span>
          <span class="shell-skeleton__line"></span>
          <span class="shell-skeleton__line"></span>
          <span class="shell-skeleton__card"></span>
        </main>
      </div>
      <span class="sr-only">Loading workspace</span>
    </div>
  {:else}
    <div class="shell-boot-blank" aria-hidden="true"></div>
  {/if}
{/if}
