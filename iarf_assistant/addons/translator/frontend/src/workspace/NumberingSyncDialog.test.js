import { cleanup, fireEvent, render, waitFor } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import NumberingSyncDialog from "./NumberingSyncDialog.svelte";

afterEach(cleanup);

function report(overrides = {}) {
  return {
    contract: "translator-numbering-sync-v1", preview_token: "a".repeat(64), revision: 0, can_undo: false,
    changes: [{ id: "list-a", root_index: 0, excerpt: "Résumé — Łódź", changes: [{ node_index: 0, before: { start: "1" }, after: { start: "5" } }] }],
    conflicts: [], ...overrides
  };
}

describe("numbering review", () => {
  it("requires selection, preserves the idempotency key on uncertain retry, and refreshes after success", async () => {
    const client = {
      numberingPreview: vi.fn().mockResolvedValue(report()),
      numberingApply: vi.fn().mockRejectedValueOnce(new Error("Connection interrupted")).mockResolvedValueOnce({ revision: 1 })
    };
    const onApplied = vi.fn();
    const screen = render(NumberingSyncDialog, { client, onApplied });
    await screen.findByText("Résumé — Łódź");
    expect(screen.getByRole("button", { name: "Apply selected (0)" }).disabled).toBe(true);
    await fireEvent.click(screen.getByRole("checkbox"));
    await fireEvent.click(screen.getByRole("button", { name: "Apply selected (1)" }));
    await screen.findByText("Connection interrupted");
    await fireEvent.click(screen.getByRole("button", { name: "Apply selected (1)" }));
    await waitFor(() => expect(onApplied).toHaveBeenCalledOnce());
    expect(client.numberingApply.mock.calls[0][0]).toEqual(client.numberingApply.mock.calls[1][0]);
    expect(client.numberingApply.mock.calls[0][0].selected_ids).toEqual(["list-a"]);
    await waitFor(() => expect(client.numberingPreview).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("button", { name: "Apply selected (0)" }).disabled).toBe(true);
  });

  it("keeps conflicts out of selection and allows an explicit undo with the current token", async () => {
    const client = {
      numberingPreview: vi.fn().mockResolvedValue(report({ changes: [], can_undo: true, conflicts: [{ root_index: 2, excerpt: "Ambiguous repeated list" }] })),
      numberingUndo: vi.fn().mockResolvedValue({ revision: 2 })
    };
    const screen = render(NumberingSyncDialog, { client });
    await screen.findByText("Lists needing manual review (1)");
    expect(screen.queryByRole("checkbox")).toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: "Undo latest synchronization" }));
    await waitFor(() => expect(client.numberingUndo).toHaveBeenCalledOnce());
    expect(client.numberingUndo.mock.calls[0][0]).toMatchObject({ preview_token: "a".repeat(64) });
    expect(client.numberingUndo.mock.calls[0][0]).not.toHaveProperty("selected_ids");
  });
});
