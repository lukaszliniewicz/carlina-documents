import { cleanup, fireEvent, render, waitFor } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import RetranslationDialog from "./RetranslationDialog.svelte";

afterEach(cleanup);

describe("RetranslationDialog", () => {
  it("offers only saved dossiers covering every selected block", async () => {
    const onSubmit = vi.fn();
    const view = render(RetranslationDialog, {
      props: {
        open: true,
        selectionCount: 2,
        selectedBlockIds: ["block-2", "block-4"],
        accessRole: "owner",
        dossiers: [
          {
            dossier_id: "narrow-dossier",
            revision_id: "narrow-revision",
            version: 1,
            scope: { kind: "blocks", block_ids: ["block-2"] }
          },
          {
            dossier_id: "covering-dossier",
            revision_id: "covering-revision",
            version: 3,
            scope: { kind: "blocks", block_ids: ["block-2", "block-4"] },
            conclusion_provenance: "external_unverified"
          }
        ],
        onSubmit
      }
    });

    const dossierSelect = view.getByRole("combobox", { name: /dossier revision/i });
    expect([...dossierSelect.options].map((option) => option.value)).toEqual([
      "covering-revision"
    ]);

    await fireEvent.submit(view.container.querySelector("form"));
    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce());
    expect(onSubmit.mock.calls[0][0]).toMatchObject({
      research_choice: "reuse_dossier",
      dossier_revision_id: "covering-revision",
      execute_now: true
    });
  });
});
