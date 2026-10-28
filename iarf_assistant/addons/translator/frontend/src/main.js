import {
  FRONTEND_API_VERSION
} from "@carlina/sdk";
import {
  mount as mountSvelteComponent,
  unmount as unmountSvelteComponent
} from "svelte";

import ProjectsApp from "./projects/ProjectsApp.svelte";
import WorkspaceGate from "./workspace/WorkspaceGate.svelte";

export const apiVersion = FRONTEND_API_VERSION;

export function workspaceProjectId(currentPath) {
  const pathname = new URL(currentPath, window.location.origin).pathname;
  const match = pathname.match(
    /^\/addons\/translator\/workspace\/([^/?#]+)\/?$/
  );
  return match ? decodeURIComponent(match[1]) : null;
}

export function mount(target, context) {
  if (!(target instanceof Element)) {
    throw new TypeError("Translator requires a DOM Element target.");
  }
  target.replaceChildren();
  target.setAttribute("data-addon-id", context?.addonId || "translator");

  let projectId = workspaceProjectId(
    context?.currentPath || window.location.href
  );
  let component = mountView(projectId);
  let desiredProjectId = projectId;
  let transitionRunning = false;
  let transitionPromise = Promise.resolve();
  let disposed = false;

  function mountView(nextProjectId) {
    return nextProjectId
      ? mountSvelteComponent(WorkspaceGate, {
          target,
          props: {
            context,
            bootstrapData: {
              projectId: nextProjectId,
              guestMode: false,
              shareToken: ""
            }
          }
        })
      : mountSvelteComponent(ProjectsApp, {
          target,
          props: { context }
        });
  }

  function schedulePathTransition() {
    if (transitionRunning || disposed) return transitionPromise;
    transitionRunning = true;
    transitionPromise = (async () => {
      try {
        while (
          !disposed &&
          (component === null || projectId !== desiredProjectId)
        ) {
          const mounted = component;
          component = null;
          if (mounted) await unmountSvelteComponent(mounted);
          if (disposed) return;
          target.replaceChildren();
          projectId = desiredProjectId;
          component = mountView(projectId);
        }
      } finally {
        transitionRunning = false;
      }
    })();
    return transitionPromise;
  }

  return {
    updateCurrentPath(currentPath) {
      const nextProjectId = workspaceProjectId(currentPath);
      desiredProjectId = nextProjectId;
      if (nextProjectId === projectId && component !== null) {
        return transitionPromise;
      }
      return schedulePathTransition();
    },
    async unmount() {
      disposed = true;
      try {
        await transitionPromise;
      } finally {
        const mounted = component;
        component = null;
        try {
          if (mounted) await unmountSvelteComponent(mounted);
        } finally {
          target.replaceChildren();
          target.removeAttribute("data-addon-id");
        }
      }
    }
  };
}
