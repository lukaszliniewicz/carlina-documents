export interface RestartStatus {
  pending?: boolean;
  status?: string;
  reasons?: string[];
  completed_handoff_id?: string | null;
}

export interface RestartStatusReadOptions {
  cache?: RequestCache;
}

interface RestartReconnectOptions {
  handoffId: string;
  readStatus: () => Promise<RestartStatus>;
  onStatus?: (status: RestartStatus) => void;
  onSuccessor: () => void;
  onTimeout: () => void;
  schedule: (callback: () => void, delayMs: number) => number;
  cancel: (timer: number) => void;
  delayMs?: number;
  maxAttempts?: number;
}

/** Only the durable completion record for this request permits an auto-reload. */
export function isCompletedRestartHandoff(
  handoffId: string,
  status: RestartStatus,
): boolean {
  return Boolean(
    handoffId &&
      status.status === "completed" &&
      status.completed_handoff_id === handoffId,
  );
}

export function readRestartStatus(
  request: (
    path: string,
    init?: RestartStatusReadOptions,
  ) => Promise<RestartStatus>,
): Promise<RestartStatus> {
  return request("/api/system/restart-status", { cache: "no-store" });
}

/** Poll durable handoff state without treating another worker as a successor. */
export function startRestartReconnect({
  handoffId,
  readStatus,
  onStatus,
  onSuccessor,
  onTimeout,
  schedule,
  cancel,
  delayMs = 1_500,
  maxAttempts = 100,
}: RestartReconnectOptions): () => void {
  let attempts = 0;
  let stopped = false;
  let timer: number | undefined;

  const scheduleNext = () => {
    if (stopped) return;
    timer = schedule(() => void probe(), delayMs);
  };

  const probe = async () => {
    timer = undefined;
    if (stopped) return;
    attempts += 1;
    try {
      const status = await readStatus();
      if (stopped) return;
      onStatus?.(status);
      if (isCompletedRestartHandoff(handoffId, status)) {
        stopped = true;
        onSuccessor();
        return;
      }
    } catch {
      // The predecessor may continue serving briefly, or no process may be
      // serving yet. Only the matching durable completion finishes the handoff.
    }

    if (attempts >= maxAttempts) {
      stopped = true;
      onTimeout();
      return;
    }
    scheduleNext();
  };

  scheduleNext();
  return () => {
    stopped = true;
    if (timer !== undefined) cancel(timer);
  };
}
