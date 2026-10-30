export interface FrontendRouterWindow {
  location: {
    href: string;
    origin: string;
    pathname: string;
    search: string;
    hash: string;
    assign(url: string): void;
  };
  history: {
    pushState(data: unknown, unused: string, url?: string | URL | null): void;
    replaceState(data: unknown, unused: string, url?: string | URL | null): void;
  };
  addEventListener(type: "popstate", listener: () => void): void;
  removeEventListener(type: "popstate", listener: () => void): void;
  dispatchEvent?(event: Event): boolean;
}

export interface FrontendRouter {
  current(): string;
  navigate(
    href: string,
    options?: { replace?: boolean },
  ): "client" | "document";
  start(): void;
  stop(): void;
}

function normalizeRoutePath(pathname: string): string {
  if (!pathname.startsWith("/")) {
    return "/";
  }
  if (pathname.length > 1 && pathname.endsWith("/")) {
    return pathname.slice(0, -1);
  }
  return pathname;
}

export function relativeBrowserUrl(
  location: Pick<
    FrontendRouterWindow["location"],
    "pathname" | "search" | "hash"
  >,
): string {
  return `${location.pathname || "/"}${location.search || ""}${location.hash || ""}`;
}

export function isClientRoute(
  pathname: string,
  clientPaths: readonly string[],
): boolean {
  const normalized = normalizeRoutePath(pathname);
  return clientPaths.some((candidate) => {
    const clientPath = normalizeRoutePath(candidate);
    return (
      clientPath === normalized ||
      (clientPath !== "/" && normalized.startsWith(`${clientPath}/`))
    );
  });
}

export function shouldUseClientNavigation(event: MouseEvent): boolean {
  return (
    !event.defaultPrevented &&
    event.button === 0 &&
    !event.altKey &&
    !event.ctrlKey &&
    !event.metaKey &&
    !event.shiftKey
  );
}

export function createFrontendRouter(options: {
  window: FrontendRouterWindow;
  clientPaths: readonly string[];
  onRouteChange(route: string): void;
}): FrontendRouter {
  const { window, clientPaths, onRouteChange } = options;
  let started = false;
  let acceptedRoute = relativeBrowserUrl(window.location);
  let navigationSequence = 0;
  const canLeave = (href: string, resume: () => void) => {
    const current = new URL(acceptedRoute, window.location.origin);
    const target = new URL(href, window.location.origin);
    if (current.origin === target.origin && current.pathname === target.pathname && current.search === target.search) return true;
    return window.dispatchEvent?.(new CustomEvent("carlina:before-navigate", { cancelable: true, detail: { resume } })) !== false;
  };
  function navigate(href: string, navigationOptions: { replace?: boolean } = {}, confirmed = false, sequence = ++navigationSequence): "client" | "document" {
    const target = new URL(href, window.location.href);
    if (target.origin !== window.location.origin || !isClientRoute(target.pathname, clientPaths)) {
      // Full document navigation uses the browser's beforeunload protection.
      window.location.assign(target.href);
      return "document";
    }
    if (!confirmed && !canLeave(target.href, () => { if (sequence === navigationSequence) navigate(href, navigationOptions, true, sequence); })) return "client";
    const relative = `${target.pathname}${target.search}${target.hash}`;
    if (navigationOptions.replace) window.history.replaceState({}, "", relative);
    else if (relative !== relativeBrowserUrl(window.location)) window.history.pushState({}, "", relative);
    acceptedRoute = relative;
    onRouteChange(relative);
    return "client";
  }
  const notify = () => {
    const next = relativeBrowserUrl(window.location);
    const sequence = ++navigationSequence;
    if (!canLeave(next, () => { if (sequence === navigationSequence) navigate(next, { replace: true }, true, sequence); })) {
      // A popstate URL has already changed. Keep the mounted editor and restore
      // its URL when the user cancels leaving; a later Back can be tried again.
      window.history.pushState({}, "", acceptedRoute);
      return;
    }
    acceptedRoute = next;
    onRouteChange(next);
  };

  return {
    current() {
      return relativeBrowserUrl(window.location);
    },
    navigate,
    start() {
      if (started) {
        return;
      }
      window.addEventListener("popstate", notify);
      started = true;
    },
    stop() {
      if (!started) {
        return;
      }
      window.removeEventListener("popstate", notify);
      started = false;
    },
  };
}
