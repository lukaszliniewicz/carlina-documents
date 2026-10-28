<script lang="ts">
  import { untrack } from "svelte";
  import {
    addonApplicationMountKey,
    findAddonApplication,
    mountAddonApplication,
    type FrontendApiClient,
    type FrontendBootstrap,
    type MountedFrontend,
  } from "@carlina/sdk";
  import { Icon } from "@carlina/ui";

  interface Props {
    api: FrontendApiClient;
    bootstrap: FrontendBootstrap;
    route: string;
    navigate: (
      href: string,
      clientRoute?: boolean,
      options?: { replace?: boolean },
    ) => "client" | "document";
  }

  let { bootstrap, route, navigate }: Props = $props();
  let target = $state<Element | null>(null);
  let mounted = $state<MountedFrontend | undefined>();
  let loading = $state(true);
  let error = $state("");

  const match = $derived(findAddonApplication(bootstrap, route));
  const mountKey = $derived(addonApplicationMountKey(match, bootstrap));

  $effect(() => {
    const identity = mountKey;
    if (!target || !identity) {
      loading = false;
      error = "This addon application is not available in the new shell.";
      return;
    }

    const currentMatch = untrack(() => match);
    const currentBootstrap = untrack(() => bootstrap);
    const initialRoute = untrack(() => route);
    if (!currentMatch) return;

    const controller = new AbortController();
    let handle: MountedFrontend | undefined;
    mounted = undefined;
    loading = true;
    error = "";

    void mountAddonApplication(
      currentMatch,
      target,
      currentBootstrap,
      initialRoute,
      {
        applicationOrigin: window.location.origin,
        document,
        signal: controller.signal,
        navigate: (href, options) => navigate(href, true, options),
      },
    )
      .then((nextHandle) => {
        handle = nextHandle;
        mounted = nextHandle;
        if (!controller.signal.aborted) loading = false;
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        loading = false;
        error =
          reason instanceof Error
            ? reason.message
            : "This addon application could not be loaded.";
      });

    return () => {
      controller.abort();
      if (mounted === handle) mounted = undefined;
      void handle?.unmount();
    };
  });

  $effect(() => {
    const currentRoute = route;
    void mounted?.updateCurrentPath?.(currentRoute);
  });
</script>

<section
  class="addon-application"
  aria-busy={loading}
  data-addon-application={match?.addon.id}
>
  {#if loading}
    <div class="addon-loading" role="status" aria-live="polite">
      <span aria-hidden="true"></span>
      <p>Opening {match?.addon.name ?? "workspace"}…</p>
    </div>
  {/if}

  {#if error}
    <div class="addon-error" role="alert">
      <span><Icon name="activity" size={22} /></span>
      <p class="eyebrow">Addon application unavailable</p>
      <h1>{match?.addon.name ?? "Workspace"} could not open.</h1>
      <p>{error}</p>
      <a href={route}>Try loading the addon again</a>
    </div>
  {/if}

  <div
    class:mounted={!loading && !error}
    class="addon-target"
    bind:this={target}
  ></div>
</section>

<style>
  .addon-application {
    position: relative;
    height: calc(100vh - var(--shell-header-height));
    min-height: 0;
    background: var(--color-paper);
  }

  .addon-target {
    height: 100%;
    min-height: 0;
    visibility: hidden;
  }

  @supports (height: 100dvh) {
    .addon-application {
      height: calc(100dvh - var(--shell-header-height));
    }
  }

  .addon-target.mounted {
    visibility: visible;
  }

  .addon-loading {
    position: absolute;
    z-index: 2;
    inset: 0;
    display: grid;
    align-content: center;
    justify-items: center;
    gap: 0.8rem;
    background: var(--color-paper);
    color: var(--color-ink-muted);
    font-size: 0.76rem;
  }

  .addon-loading span {
    width: 2.4rem;
    height: 2.4rem;
    border: 1px solid var(--color-rule-strong);
    border-top-color: var(--color-clay);
    animation: turn 0.9s linear infinite;
  }

  .addon-error {
    position: absolute;
    z-index: 3;
    inset: 0;
    display: grid;
    align-content: center;
    justify-items: start;
    gap: 0.7rem;
    padding: clamp(1.5rem, 7vw, 6rem);
    background: var(--color-paper);
  }

  .addon-error > span {
    width: 2.8rem;
    height: 2.8rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-danger);
    color: var(--color-danger);
  }

  .addon-error .eyebrow {
    margin: 0.7rem 0 0;
    color: var(--color-danger);
    font-size: 0.65rem;
    font-weight: 800;
    letter-spacing: 0.15em;
    text-transform: uppercase;
  }

  .addon-error h1,
  .addon-error p {
    margin: 0;
  }

  .addon-error h1 {
    font-family: var(--font-editorial);
    font-size: clamp(2rem, 5vw, 3.4rem);
    font-weight: 450;
  }

  .addon-error p:not(.eyebrow) {
    color: var(--color-ink-muted);
  }

  .addon-error a {
    margin-top: 0.5rem;
    color: var(--color-clay);
    font-size: 0.78rem;
    font-weight: 700;
    text-underline-offset: 0.25rem;
  }

  @keyframes turn {
    to {
      transform: rotate(360deg);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .addon-loading span {
      animation: none;
      border-top-color: var(--color-rule-strong);
      background: var(--color-clay-muted);
    }
  }
</style>
