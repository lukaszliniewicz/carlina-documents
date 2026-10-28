import { cleanup, fireEvent, render } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import BlockOperationState from "./BlockOperationState.svelte";

afterEach(() => cleanup());

describe("BlockOperationState", () => {
  it("renders partial completion as a warning with a deliberate new-operation hint", () => {
    const view = render(BlockOperationState, {
      props: {
        operation: {
          id: "operation-1",
          state: "partial",
          progress: 50,
          message: "Translation paused at window 2; completed windows were preserved.",
          block_failure: { recovery_action: "translate_remaining" }
        }
      }
    });

    const state = view.getByTestId("block-operation-state");
    expect(state.className).toContain("partial");
    expect(state.textContent).toContain("partially complete");
    expect(view.getByTestId("block-operation-guidance").textContent).toContain("Start a new translation");
  });

  it("keeps cancellation visibly active and prevents duplicate stop requests", async () => {
    const onStop = vi.fn();
    const running = render(BlockOperationState, {
      props: {
        operation: { id: "operation-1", state: "running", progress: 50 },
        canStop: true,
        onStop
      }
    });
    await fireEvent.click(running.getByRole("button", { name: "Stop workflow" }));
    expect(onStop).toHaveBeenCalledOnce();
    running.unmount();

    const stopping = render(BlockOperationState, {
      props: {
        operation: {
          id: "operation-1",
          state: "cancel_requested",
          progress: 50,
          message: "Cancellation requested; waiting for a safe worker boundary."
        },
        canStop: true
      }
    });
    expect(stopping.getByText("Stopping block workflow")).toBeTruthy();
    expect(stopping.getByRole("button", { name: "Stopping..." }).disabled).toBe(true);
    expect(stopping.getByTestId("block-operation-state").className).toContain("busy");
  });
});
