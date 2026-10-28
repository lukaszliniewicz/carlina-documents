import type { FrontendAddonManagerItem } from "@carlina/sdk";

import type { AddonActionConfirmation } from "./addon-manager.ts";
import type { AddonLifecycleCommand } from "./addon-manager-api.ts";

export type AddonLifecycleAction =
  | "activate"
  | "deactivate"
  | "uninstall"
  | "purge"
  | "stop-workers"
  | "restart-workers"
  | "pause-job"
  | "resume-job"
  | "run-job"
  | "restart-server";

export interface AddonPendingAction extends AddonActionConfirmation {
  kind: AddonLifecycleAction;
  command: AddonLifecycleCommand;
  addon: FrontendAddonManagerItem | null;
}

export function createAddonLifecycleAction(
  kind: "activate" | "deactivate" | "uninstall" | "purge",
  addon: FrontendAddonManagerItem,
): AddonPendingAction {
  const reviewNotices = (addon.dependency_notices ?? []).filter(
    (notice) => notice.review_required !== false,
  );
  const dependencyReviewSummary = reviewNotices.length
    ? ` This installs optional dependencies after explicit license review: ${reviewNotices
        .map(
          (notice) =>
            `${notice.packages.join(", ")} (${notice.license})`,
        )
        .join("; ")}.`
    : "";
  const definitions = {
    activate: {
      title: `Activate ${addon.name}?`,
      description:
        "Dependencies and frontend assets will be prepared, then the live registry will be refreshed. A managed server restart completes route registration." +
        dependencyReviewSummary,
      confirmLabel: "Activate addon",
      danger: false,
    },
    deactivate: {
      title: `Deactivate ${addon.name}?`,
      description:
        "Its navigation, tools, scheduled jobs, workers, and services will become unavailable. In-flight background work may be interrupted.",
      confirmLabel: "Deactivate addon",
      danger: true,
    },
    uninstall: {
      title: `Remove ${addon.name} code?`,
      description:
        "This removes the installed package and runtime resources. Add-on data, database schema, lifecycle history, and settings are preserved for reinstall or a later explicit purge.",
      confirmLabel: "Remove code, preserve data",
      danger: true,
    },
    purge: {
      title: `Purge ${addon.name} data?`,
      description:
        "This creates a safety backup, then permanently removes add-on-owned data and declared schema. Reinstalling the code will not restore that data automatically.",
      confirmLabel: "Create backup and purge data",
      danger: true,
    },
  } as const;
  const command: AddonLifecycleCommand =
    kind === "purge"
      ? {
          kind,
          addonId: addon.id,
          confirmation: `PURGE ${addon.id}`,
        }
      : kind === "activate"
        ? {
            kind,
            addonId: addon.id,
            dependencyReview: reviewNotices.map((notice) => notice.id),
          }
        : { kind, addonId: addon.id };
  return {
    kind,
    ...definitions[kind],
    command,
    addon,
    requiresIdentifier:
      ["uninstall", "purge"].includes(kind) ||
      (kind === "activate" && reviewNotices.length > 0),
    confirmationPhrase: kind === "purge" ? `PURGE ${addon.id}` : addon.id,
  };
}

export function createAddonWorkerAction(
  kind: "stop-workers" | "restart-workers",
  addon: FrontendAddonManagerItem,
): AddonPendingAction {
  const stop = kind === "stop-workers";
  return {
    kind,
    title: `${stop ? "Stop" : "Restart"} ${addon.name} workers?`,
    description: stop
      ? "All tracked worker processes for this addon will be stopped. In-flight tasks may need to be retried."
      : "Tracked workers will be stopped and started again using the registered entrypoint and desired worker count.",
    confirmLabel: stop ? "Stop workers" : "Restart workers",
    command: { kind, addonId: addon.id },
    addon,
    danger: stop,
  };
}

export function createAddonJobAction(
  kind: "pause-job" | "resume-job" | "run-job",
  addon: FrontendAddonManagerItem,
  jobId: string,
): AddonPendingAction {
  const verb = {
    "pause-job": "Pause",
    "resume-job": "Resume",
    "run-job": "Run",
  }[kind];
  return {
    kind,
    title: `${verb} scheduled job?`,
    description: `${verb} “${jobId}” for ${addon.name}. The scheduler will update immediately.`,
    confirmLabel: kind === "run-job" ? "Run now" : `${verb} job`,
    command: { kind, addonId: addon.id, jobId },
    addon,
  };
}

export function createServerRestartAction(): AddonPendingAction {
  return {
    kind: "restart-server",
    title: "Restart the application server?",
    description:
      "The browser will disconnect briefly while the launcher hands the application to a fresh process. Unsaved work in other tabs may be lost.",
    confirmLabel: "Restart server",
    command: { kind: "restart-server" },
    addon: null,
    danger: true,
  };
}
