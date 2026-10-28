/// <reference lib="webworker" />

import type { FrontendRunGraph } from "@carlina/sdk";

import { layoutRunGraph } from "./run-layout.ts";

declare const self: DedicatedWorkerGlobalScope;

self.onmessage = async (
  event: MessageEvent<{ graph: FrontendRunGraph }>,
) => {
  try {
    const positions = await layoutRunGraph(event.data.graph);
    self.postMessage({ positions });
  } catch (error) {
    self.postMessage({
      error:
        error instanceof Error
          ? error.message
          : "The graph layout could not be calculated.",
    });
  }
};

export {};
