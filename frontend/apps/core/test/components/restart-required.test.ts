import { describe, expect, it, vi } from "vitest";

import {
  announceRestartRequired,
  RESTART_REQUIRED_EVENT,
} from "../../src/lib/restart-required.ts";

describe("restart-required shell notification", () => {
  it("announces a completed lifecycle restart requirement immediately", () => {
    const listener = vi.fn();
    window.addEventListener(RESTART_REQUIRED_EVENT, listener, { once: true });

    announceRestartRequired();

    expect(listener).toHaveBeenCalledOnce();
  });
});
