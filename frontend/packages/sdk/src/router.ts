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
  const notify = () => onRouteChange(relativeBrowserUrl(window.location));

  return {
    current() {
      return relativeBrowserUrl(window.location);
    },
    navigate(href, navigationOptions = {}) {
      const target = new URL(href, window.location.href);
      if (
        target.origin !== window.location.origin ||
        !isClientRoute(target.pathname, clientPaths)
      ) {
        window.location.assign(target.href);
        return "document";
      }

      const relative = `${target.pathname}${target.search}${target.hash}`;
      if (navigationOptions.replace) {
        window.history.replaceState({}, "", relative);
      } else if (relative !== relativeBrowserUrl(window.location)) {
        window.history.pushState({}, "", relative);
      }
      onRouteChange(relative);
      return "client";
    },
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
