import { mount, unmount } from "svelte";

import {
  FRONTEND_API_VERSION,
  installLegacyFrontendAdapter,
  type FrontendApiClient,
  type FrontendBootstrap,
  type FrontendMountContext,
  type MountedFrontend,
} from "@carlina/sdk";

import App from "./App.svelte";
import "./app.css";

export interface CoreMountOptions {
  api?: FrontendApiClient;
  bootstrap?: FrontendBootstrap;
  bootstrapUrl?: string;
  initialPath?: string;
  label?: string;
}

export function mountCoreApplication(
  target: Element,
  options: CoreMountOptions = {},
): MountedFrontend {
  if (!(target instanceof Element)) {
    throw new TypeError("The core Svelte application requires a DOM Element target.");
  }
  if (target.hasAttribute("data-iarf-core-mounted")) {
    throw new Error("The core Svelte application is already mounted on this target.");
  }

  // Flask leaves a useful no-JavaScript/loading fallback inside the mount
  // target. Svelte appends by default, so clear that server-owned placeholder
  // before mounting to avoid leaving it over the live application.
  target.replaceChildren();
  const instance = mount(App, {
    target,
    props: options,
  });
  target.setAttribute("data-iarf-core-mounted", FRONTEND_API_VERSION);

  return {
    async unmount() {
      await unmount(instance);
      target.removeAttribute("data-iarf-core-mounted");
    },
  };
}

export const apiVersion = FRONTEND_API_VERSION;

export function mountCore(
  target: Element,
  context: FrontendMountContext,
): MountedFrontend {
  return mountCoreApplication(target, { api: context.api });
}

export async function unmountCore(mounted: MountedFrontend): Promise<void> {
  await mounted.unmount();
}

function mountDocumentApplication(): void {
  const target = document.querySelector("[data-iarf-core-root]");
  if (!(target instanceof Element) || target.hasAttribute("data-iarf-core-mounted")) {
    return;
  }
  mountCoreApplication(target, {
    bootstrapUrl:
      target.getAttribute("data-bootstrap-url") ?? "/api/frontend/bootstrap",
    initialPath: `${window.location.pathname}${window.location.search}`,
  });
}

if (typeof window !== "undefined") {
  installLegacyFrontendAdapter(window);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mountDocumentApplication, {
      once: true,
    });
  } else {
    mountDocumentApplication();
  }
}

export {
  FRONTEND_API_VERSION,
  mountCore as mount,
  unmountCore as unmount,
};
