import {
  FRONTEND_API_VERSION,
  type MountedFrontend,
  type UiComponentMountContext,
} from "@carlina/sdk";
import {
  mount as mountSvelteComponent,
  unmount as unmountSvelteComponent,
} from "svelte";

import SegmenterTester from "./SegmenterTester.svelte";
import "./segmenter-tester.css";

export const apiVersion = FRONTEND_API_VERSION;

export function mount(
  target: Element,
  context: UiComponentMountContext,
): MountedFrontend {
  target.setAttribute("data-addon-id", context.addonId);
  const component = mountSvelteComponent(SegmenterTester, {
    target,
    props: { context },
  });

  return {
    async unmount(): Promise<void> {
      await unmountSvelteComponent(component);
      target.removeAttribute("data-addon-id");
    },
  };
}
