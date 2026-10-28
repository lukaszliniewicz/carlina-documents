import { cleanup, fireEvent, render } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import RetranslationRequestsDialog from "./RetranslationRequestsDialog.svelte";

afterEach(cleanup);

const awaitingRequest = {
  id: "request-1",
  version: 2,
  state: "requested",
  resolved_block_ids: ["block-2", "block-4"],
  annotation_profile: "study",
  research_choice: "await_external",
  rationale: "Explain the Hebrew section marker.",
  custom_prompt: "Preserve the author’s distinctions.",
  created_at: "2026-09-05T10:00:00Z"
};

describe("RetranslationRequestsDialog", () => {
  it("lets the owner attach only a dossier that covers the frozen request scope", async () => {
    const onAttach = vi.fn();
    const view = render(RetranslationRequestsDialog, {
      props: {
        open: true,
        accessRole: "owner",
        requests: [awaitingRequest],
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
            scope: { kind: "blocks", block_ids: ["block-2", "block-4"] }
          }
        ],
        onAttach
      }
    });

    const select = view.getByRole("combobox", { name: /research dossier covering/i });
    expect([...select.options].map((option) => option.value)).toEqual(["covering-revision"]);
    await fireEvent.click(view.getByRole("button", { name: "Attach evidence" }));
    expect(onAttach).toHaveBeenCalledWith(awaitingRequest, "covering-revision");
  });

  it("keeps reviewer requests visible but cannot execute them", () => {
    const view = render(RetranslationRequestsDialog, {
      props: {
        open: true,
        accessRole: "reviewer",
        requests: [{ ...awaitingRequest, research_choice: "fresh_document" }]
      }
    });

    expect(view.getByText(/Explain the Hebrew section marker/)).toBeTruthy();
    expect(view.queryByRole("button", { name: /start exact request/i })).toBeNull();
    expect(view.queryByRole("button", { name: /attach evidence/i })).toBeNull();
  });

  it("offers an exact ready request to the owner for execution", async () => {
    const onExecute = vi.fn();
    const request = {
      ...awaitingRequest,
      id: "request-2",
      research_choice: "reuse_dossier",
      dossier_revision_id: "covering-revision"
    };
    const view = render(RetranslationRequestsDialog, {
      props: { open: true, accessRole: "owner", requests: [request], onExecute }
    });

    await fireEvent.click(view.getByRole("button", { name: "Start exact request" }));
    expect(onExecute).toHaveBeenCalledWith(request);
  });
});
