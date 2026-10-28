<script lang="ts">
  import { onMount, untrack, type Component } from "svelte";
  import {
    createUiComponentService,
    createFrontendRouter,
    findAddonApplication,
    shouldUseClientNavigation,
    type FrontendApiClient,
    type FrontendBootstrap,
    type FrontendNavigationItem,
    type FrontendRouter,
  } from "@carlina/sdk";
  import { Button, Dialog, Icon } from "@carlina/ui";
  import { routeLoaders } from "@carlina/core-routes";
  import type { CoreRoutePageProps as RoutePageProps } from "../lib/core-route-contract.ts";

  import CommandPalette from "./CommandPalette.svelte";
  import ApplicationSourceLink from "./ApplicationSourceLink.svelte";
  import Header from "./Header.svelte";
  import MobileNavigation from "./MobileNavigation.svelte";
  import NotificationCenter from "./NotificationCenter.svelte";
  import OverlayHost from "./OverlayHost.svelte";
  import Sidebar from "./Sidebar.svelte";
  import { composerInsertionFromComponentResult } from "../lib/component-results.ts";
  import { RESTART_REQUIRED_EVENT } from "../lib/restart-required.ts";
  import {
    readRestartStatus,
    startRestartReconnect,
    type RestartStatus,
  } from "../lib/restart-reconnect.ts";
  import { routePageKey } from "../lib/route-page.ts";
  import {
    frontendApplication,
    frontendChatEnabled,
    frontendClientPaths,
    frontendCoreRoutes,
  } from "../lib/application-profile.ts";

  interface Props {
    bootstrap: FrontendBootstrap;
    api: FrontendApiClient;
    initialPath: string;
  }

  interface QuickPicker {
    componentId: string;
    label: string;
    detail: string;
  }

  let {
    bootstrap: initialBootstrap,
    api,
    initialPath,
  }: Props = $props();

  let bootstrap = $state(untrack(() => initialBootstrap));
  let currentRoute = $state(untrack(() => initialPath || "/workspaces"));
  let Page = $state<Component<RoutePageProps> | null>(null);
  let routeError = $state("");
  let collapsed = $state(false);
  let commandOpen = $state(false);
  let notificationsOpen = $state(false);
  let restartOpen = $state(false);
  let restarting = $state(false);
  let restartError = $state("");
  let restartPending = $state(false);
  let shellNotice = $state("");
  let router: FrontendRouter | null = null;
  let shellNoticeTimer: number | undefined;
  let stopRestartReconnect: (() => void) | undefined;
  const application = $derived(frontendApplication(bootstrap));
  const chatEnabled = $derived(frontendChatEnabled(bootstrap));
  const restartEnabled = $derived(
    bootstrap.capabilities.runtime_restart ?? application.profile === "workbench",
  );
  const uiComponents = $derived(createUiComponentService(
    bootstrap,
    window.location.origin,
  ));
  const quickPickers = $derived.by<QuickPicker[]>(() => {
    if (!chatEnabled) return [];
    const availableComponents = new Set(
      bootstrap.ui_components
        .filter(
          (component) =>
            ["active", "degraded"].includes(component.status) &&
            component.compatible &&
            Boolean(component.module_url),
        )
        .map((component) => component.id),
    );
    const pickers = new Map<string, QuickPicker>();
    for (const contribution of bootstrap.ui_contributions) {
      const componentId = contribution.component_id;
      if (
        contribution.kind !== "picker" ||
        !componentId ||
        !availableComponents.has(componentId) ||
        !["active", "degraded"].includes(contribution.status) ||
        !contribution.entrypoint_kinds.some((kind) =>
          ["quick_access", "command_palette", "slash_command"].includes(kind),
        )
      ) {
        continue;
      }
      pickers.set(componentId, {
        componentId,
        label: contribution.title,
        detail: contribution.description || contribution.addon_id,
      });
    }
    return [...pickers.values()].sort((left, right) =>
      left.label.localeCompare(right.label),
    );
  });

  const clientPaths = $derived(
    clientPathsFor(bootstrap),
  );
  const pathname = $derived(
    new URL(currentRoute, "https://iarf.local").pathname.replace(/\/+$/, "") ||
      "/",
  );
  const addonMatch = $derived(findAddonApplication(bootstrap, pathname));
  const activePageKey = $derived(
    routePageKey(pathname, addonMatch, frontendCoreRoutes(bootstrap)),
  );
  const activeContribution = $derived(
    addonMatch
      ? bootstrap.ui_contributions.find(
          (item) =>
            item.addon_id === addonMatch.addon.id &&
            item.routes.some((route) => pathname.startsWith(route.replace(/\/+$/, ""))),
        )
      : undefined,
  );
  const activeItem = $derived(
    bootstrap.navigation.find(
      (item) => {
        const itemPath =
          new URL(item.href, "https://iarf.local").pathname.replace(/\/+$/, "") ||
          "/";
        return (
          itemPath === pathname ||
          (itemPath !== "/" && pathname.startsWith(`${itemPath}/`))
        );
      },
    ) ??
      (pathname === "/automations"
        ? bootstrap.navigation.find((item) => item.id === "workspaces")
        : undefined) ??
      (pathname === "/approvals" || pathname === "/runtime/processes"
        ? bootstrap.navigation.find((item) => item.id === "runtime")
        : undefined) ??
      (addonMatch
        ? {
            id: `addon:${addonMatch.addon.id}`,
            label: activeContribution?.title ?? addonMatch.addon.name,
            href: addonMatch.basePath,
            icon: activeContribution?.icon ?? "workspace",
            section: activeContribution?.section ?? "Workspaces",
            client_route: true,
          }
        : undefined),
  );

  $effect(() => {
    const pageKey = activePageKey;
    const loader = pageKey.startsWith("addon:")
      ? () => import("../routes/AddonApplicationPage.svelte")
      : pageKey === "/" && application.profile === "documents"
        ? () => import("../routes/DocumentsHomePage.svelte")
        : routeLoaders[pageKey];
    let current = true;
    Page = null;
    routeError = "";
    if (!loader) {
      routeError = "This destination is not registered in the application shell.";
      return;
    }
    void loader()
      .then((module) => {
        if (current) Page = module.default;
      })
      .catch((reason: unknown) => {
        if (current) {
          routeError =
            reason instanceof Error
              ? reason.message
              : "This view could not be loaded.";
        }
      });
    return () => {
      current = false;
    };
  });

  $effect(() => {
    if (typeof document !== "undefined") {
      document.title = `${activeItem?.label ?? "Workspace"} · ${bootstrap.product.short_name}`;
    }
  });

  onMount(() => {
    collapsed = localStorage.getItem("iarf:shell-sidebar") === "collapsed";
    installRouter(clientPaths);
    void loadRestartState();

    const handleRestartRequired = () => {
      restartPending = true;
      void loadRestartState();
    };

    const handleKeydown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        commandOpen = true;
      }
    };
    const handleOpenUiComponent = (event: Event) => {
      const detail = (
        event as CustomEvent<{
          componentId?: unknown;
          input?: unknown;
        }>
      ).detail;
      if (typeof detail?.componentId !== "string" || !detail.componentId) {
        return;
      }
      void openComponent(detail.componentId, detail.input ?? {});
    };
    window.addEventListener("keydown", handleKeydown);
    window.addEventListener(
      "iarf:open-ui-component",
      handleOpenUiComponent,
    );
    window.addEventListener(RESTART_REQUIRED_EVENT, handleRestartRequired);
    return () => {
      router?.stop();
      router = null;
      window.removeEventListener("keydown", handleKeydown);
      window.removeEventListener(
        "iarf:open-ui-component",
        handleOpenUiComponent,
      );
      window.removeEventListener(RESTART_REQUIRED_EVENT, handleRestartRequired);
      if (shellNoticeTimer) window.clearTimeout(shellNoticeTimer);
      stopRestartReconnect?.();
    };
  });

  async function loadRestartState(): Promise<RestartStatus | null> {
    if (!bootstrap.user.is_admin || !restartEnabled) return null;
    try {
      const state = await readRestartStatus((path, init) =>
        api.request<RestartStatus>(path, init),
      );
      restartPending = state.pending === true;
      return state;
    } catch {
      // A status indicator must never make ordinary navigation unavailable.
      return null;
    }
  }

  function clientPathsFor(payload: FrontendBootstrap): string[] {
    return frontendClientPaths(payload);
  }

  function installRouter(paths: string[]): void {
    router?.stop();
    router = createFrontendRouter({
      window,
      clientPaths: paths,
      onRouteChange(route) {
        currentRoute = route;
        queueMicrotask(() => {
          document.getElementById("main-content")?.focus();
        });
      },
    });
    router.start();
  }

  async function refreshBootstrap(): Promise<FrontendBootstrap> {
    const url = new URL("/api/frontend/bootstrap", window.location.origin);
    url.searchParams.set("path", currentRoute);
    const payload = await api.request<FrontendBootstrap>(
      `${url.pathname}${url.search}`,
    );
    bootstrap = payload;
    installRouter(clientPathsFor(payload));
    return payload;
  }

  function toggleSidebar() {
    collapsed = !collapsed;
    localStorage.setItem(
      "iarf:shell-sidebar",
      collapsed ? "collapsed" : "expanded",
    );
  }

  function navigateHref(
    href: string,
    clientRoute = false,
    options: { replace?: boolean } = {},
  ): "client" | "document" {
    if (clientRoute && router) {
      return router.navigate(href, options);
    }
    window.location.assign(new URL(href, window.location.href).href);
    return "document";
  }

  function activateNavigation(
    item: FrontendNavigationItem,
    event: MouseEvent,
  ) {
    if (!item.client_route || !shouldUseClientNavigation(event) || !router) {
      return;
    }
    event.preventDefault();
    router.navigate(item.href);
  }

  function activateHref(href: string, event: MouseEvent) {
    if (!shouldUseClientNavigation(event) || !router) return;
    event.preventDefault();
    router.navigate(href);
  }

  function showShellNotice(message: string): void {
    shellNotice = message;
    if (shellNoticeTimer) window.clearTimeout(shellNoticeTimer);
    shellNoticeTimer = window.setTimeout(() => {
      shellNotice = "";
    }, 5000);
  }

  function openRestartDialog(): void {
    restartError = "";
    restartOpen = true;
  }

  function closeRestartDialog(): void {
    if (restarting) return;
    restartOpen = false;
    restartError = "";
  }

  function reconnectAfterRestart(handoffId: string): void {
    stopRestartReconnect?.();
    stopRestartReconnect = startRestartReconnect({
      handoffId,
      readStatus: () => readRestartStatus((path, init) =>
        api.request<RestartStatus>(path, init),
      ),
      onStatus: (state) => {
        restartPending = state.pending === true;
      },
      onSuccessor: () => window.location.reload(),
      onTimeout: () => {
        restarting = false;
        showShellNotice(
          "The restart is taking longer than expected. Reload this page in a moment.",
        );
      },
      schedule: (callback, delayMs) => window.setTimeout(callback, delayMs),
      cancel: (timer) => window.clearTimeout(timer),
    });
  }

  async function restartServer(): Promise<void> {
    restarting = true;
    restartError = "";
    try {
      const response = await api.request<{
        success?: boolean;
        message?: string;
        handoff_id?: string;
      }>(
        "/api/system/restart",
        { method: "POST" },
      );
      restartOpen = false;
      restartPending = true;
      if (typeof response.handoff_id !== "string" || !response.handoff_id) {
        throw new Error("The server did not confirm a durable restart handoff.");
      }
      showShellNotice(
        "Server restart requested. This page will reconnect automatically.",
      );
      reconnectAfterRestart(response.handoff_id);
    } catch (reason) {
      restarting = false;
      restartError =
        reason instanceof Error
          ? reason.message
          : "The server restart could not be requested.";
    }
  }

  function queueComposerInsertion(text: string): void {
    localStorage.setItem("iarf:pending-composer-insertion", text);
    if (pathname === "/") {
      window.dispatchEvent(
        new CustomEvent("iarf:add-to-composer", {
          detail: { text },
        }),
      );
      return;
    }
    navigateHref("/", true);
  }

  async function openComponent(
    componentId: string,
    input: unknown = {},
  ): Promise<void> {
    try {
      const result = await uiComponents.open(componentId, input);
      const insertion = composerInsertionFromComponentResult(result);
      if (insertion) {
        queueComposerInsertion(insertion);
        showShellNotice("The selected material was added to the chat composer.");
      }
    } catch (reason) {
      showShellNotice(
        reason instanceof Error
          ? reason.message
          : "The reusable tool could not be opened.",
      );
    }
  }
</script>

<div class="app-shell" class:sidebar-collapsed={collapsed}>
  <Sidebar
    navigation={bootstrap.navigation}
    product={bootstrap.product}
    user={bootstrap.user}
    {api}
    {chatEnabled}
    applicationLabel={application.label}
    homeHref={application.home_url}
    currentPath={currentRoute}
    {collapsed}
    buildId={bootstrap.build.core.id}
    onnavigate={activateNavigation}
    onnavigatehref={activateHref}
    ontoggle={toggleSidebar}
  />

  <div class="shell-stage">
    <Header
      {activeItem}
      product={bootstrap.product}
      user={bootstrap.user}
      pickers={quickPickers}
      onpicker={(componentId) => void openComponent(componentId)}
      oncommand={() => (commandOpen = true)}
      onnotifications={() => (notificationsOpen = true)}
      onrestart={openRestartDialog}
      {restartEnabled}
      notificationsEnabled={application.profile === "workbench"}
    />

    <ApplicationSourceLink {api} documents={application.profile === "documents"} />

    {#if restartPending && bootstrap.user.is_admin && restartEnabled}
      <section class="restart-pending" role="status">
        <div>
          <strong>Restart required</strong>
          <span> Add-on changes are staged and remain pending until you restart the server.</span>
        </div>
        <Button variant="outline" onclick={openRestartDialog}>Restart server</Button>
      </section>
    {/if}

    <main id="main-content" tabindex="-1">
      {#if Page}
        <Page
          {api}
          {bootstrap}
          route={currentRoute}
          navigate={navigateHref}
          {refreshBootstrap}
        />
      {:else if routeError}
        <section class="route-message">
          <span><Icon name="activity" size={22} /></span>
          <h1>View unavailable</h1>
          <p>{routeError}</p>
          <a href="/">Return to the workspace</a>
        </section>
      {:else}
        <section class="route-loading" aria-label="Loading view" aria-busy="true">
          <div class="route-loading__heading"></div>
          <div class="route-loading__copy"></div>
          <div class="route-loading__grid">
            <span></span><span></span><span></span>
          </div>
        </section>
      {/if}
    </main>
  </div>

  <MobileNavigation
    navigation={bootstrap.navigation}
    currentPath={currentRoute}
    onnavigate={activateNavigation}
  />

  <OverlayHost>
    <CommandPalette
      open={commandOpen}
      {bootstrap}
      navigation={bootstrap.navigation}
      onclose={() => (commandOpen = false)}
      onopencomponent={(componentId) => void openComponent(componentId)}
      onselect={navigateHref}
    />
    <NotificationCenter
      open={notificationsOpen}
      {bootstrap}
      onclose={() => (notificationsOpen = false)}
    />
    <Dialog
      open={restartOpen}
      title="Restart the application server?"
      description="The application will be unavailable briefly while the managed launcher hands it to a fresh process."
      onclose={closeRestartDialog}
      size="sm"
    >
      <p class="restart-copy">
        Active requests may be interrupted. This page will wait for the new
        process and reconnect automatically when it is ready.
      </p>
      {#if restartError}
        <p class="restart-error" role="alert">{restartError}</p>
      {/if}
      {#snippet footer()}
        <Button
          variant="quiet"
          disabled={restarting}
          onclick={closeRestartDialog}
        >
          Cancel
        </Button>
        <Button
          variant="danger"
          disabled={restarting}
          onclick={() => void restartServer()}
        >
          {restarting ? "Restarting…" : "Restart server"}
        </Button>
      {/snippet}
    </Dialog>
  </OverlayHost>

  {#if shellNotice}
    <p class="shell-notice" role="status">{shellNotice}</p>
  {/if}
</div>

<style>
  .app-shell {
    min-height: 100vh;
  }

  .shell-stage {
    min-height: 100vh;
    margin-left: var(--shell-sidebar-width);
    transition: margin-left var(--shell-transition);
  }

  .sidebar-collapsed .shell-stage {
    margin-left: var(--shell-sidebar-rail-width);
  }

  main {
    min-height: calc(100vh - var(--shell-header-height));
    outline: none;
  }

  .route-message {
    min-height: calc(100vh - var(--shell-header-height));
    display: grid;
    align-content: center;
    justify-items: start;
    gap: 0.75rem;
    padding: clamp(1.5rem, 7vw, 6rem);
  }

  .route-message > span {
    width: 2.8rem;
    height: 2.8rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-danger);
    color: var(--color-danger);
  }

  .route-message h1 {
    margin: 0;
    font-family: var(--font-editorial);
    font-size: 2.4rem;
    font-weight: 500;
  }

  .route-message p {
    margin: 0;
    color: var(--color-ink-muted);
  }

  .route-message a {
    margin-top: 0.5rem;
    color: var(--color-clay);
    font-size: 0.78rem;
    font-weight: 700;
    text-underline-offset: 0.25rem;
  }

  .route-loading {
    display: grid;
    gap: 1rem;
    padding: clamp(1.5rem, 5vw, 4rem);
  }

  .route-loading__heading,
  .route-loading__copy,
  .route-loading__grid span {
    background: linear-gradient(
      90deg,
      var(--color-surface-muted),
      var(--color-surface),
      var(--color-surface-muted)
    );
    background-size: 200% 100%;
    animation: loading 1.6s ease-in-out infinite;
  }

  .shell-notice {
    position: fixed;
    z-index: 160;
    right: 1rem;
    bottom: 1rem;
    width: min(24rem, calc(100vw - 2rem));
    margin: 0;
    border: 1px solid var(--color-rule-strong);
    border-left: 3px solid var(--color-clay);
    padding: 0.8rem 1rem;
    background: var(--color-surface);
    color: var(--color-ink-soft);
    box-shadow: 0 0.8rem 2.5rem
      color-mix(in srgb, var(--color-ink) 14%, transparent);
    font-size: 0.72rem;
    line-height: 1.45;
  }

  .restart-copy,
  .restart-error {
    margin: 0;
    font-size: 0.78rem;
    line-height: 1.55;
  }

  .restart-pending {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    margin: 0.75rem 1rem 0;
    padding: 0.75rem 1rem;
    border: 1px solid var(--color-warning-border, #a77b2b);
    background: var(--color-warning-surface, #fff7df);
  }

  .restart-copy {
    color: var(--color-ink-soft);
  }

  .restart-error {
    margin-top: 0.8rem;
    border-left: 2px solid var(--color-danger);
    padding: 0.6rem 0.75rem;
    background: var(--color-danger-muted);
    color: var(--color-danger);
  }

  .route-loading__heading {
    width: min(32rem, 75%);
    height: 4rem;
  }

  .route-loading__copy {
    width: min(40rem, 90%);
    height: 1rem;
  }

  .route-loading__grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 1px;
    margin-top: 2rem;
    background: var(--color-rule);
  }

  .route-loading__grid span {
    min-height: 12rem;
  }

  @keyframes loading {
    to {
      background-position: -200% 0;
    }
  }

  @media (max-width: 64rem) {
    .shell-stage,
    .sidebar-collapsed .shell-stage {
      margin-left: 0;
    }
  }

  @media (max-width: 64rem) {
    main {
      padding-bottom: 3.75rem;
    }
  }

  @media (max-width: 42rem) {
    .route-loading__grid {
      grid-template-columns: 1fr;
    }
  }
</style>
