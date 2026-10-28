import {
  FRONTEND_API_VERSION,
  type AddonMountContext,
  type MountedFrontend
} from "@carlina/sdk";
import {
  mount as mountSvelteComponent,
  unmount as unmountSvelteComponent
} from "svelte";

import App from "./App.svelte";
import "./app.css";
import { configureDocProcessorApi } from "./lib/api";

export const apiVersion = FRONTEND_API_VERSION;

export function mount(
  target: Element,
  context: AddonMountContext | null = null
): MountedFrontend {
  if (!(target instanceof Element)) {
    throw new TypeError("Document Processor requires a DOM Element target.");
  }
  const previousId = target.getAttribute("id");
  const hadShellClass = target.classList.contains("tw-addon-shell");
  target.replaceChildren();
  target.setAttribute("id", "doc-processor-root");
  target.classList.add("tw-addon-shell");
  target.setAttribute("data-addon-id", context?.addonId || "doc_processor");
  const restoreApi = configureDocProcessorApi(
    context?.api || null,
    context?.signal || null
  );
  const component = mountSvelteComponent(App, { target });

  return {
    async unmount(): Promise<void> {
      restoreApi();
      await unmountSvelteComponent(component);
      target.replaceChildren();
      target.removeAttribute("data-addon-id");
      if (previousId) target.setAttribute("id", previousId);
      else target.removeAttribute("id");
      if (!hadShellClass) target.classList.remove("tw-addon-shell");
    }
  };
}
