/**
 * Monotonic request generation used to make late responses harmless.
 * A component can invalidate the generation during teardown or navigation,
 * and only the most recent request may commit state.
 */
export class LatestRequestGeneration {
  #generation = 0;

  next() {
    this.#generation += 1;
    return this.#generation;
  }

  invalidate() {
    this.#generation += 1;
  }

  isCurrent(generation) {
    return generation === this.#generation;
  }
}

export function createAbortController(parentSignal = null) {
  const controller = new AbortController();
  if (!parentSignal) return controller;
  if (parentSignal.aborted) {
    controller.abort(parentSignal.reason);
    return controller;
  }
  const abort = () => controller.abort(parentSignal.reason);
  parentSignal.addEventListener("abort", abort, { once: true });
  return controller;
}

