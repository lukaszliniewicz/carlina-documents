import { cleanup, fireEvent, render } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import BlockGlossaryPanel from "./BlockGlossaryPanel.svelte";
afterEach(cleanup);

describe("BlockGlossaryPanel", () => {
  it("opens the exact locked term selected from shared update review", async () => {
    const view = render(BlockGlossaryPanel, { props: {
      loaded: true, accessRole: "owner", focusConceptId: "locked-community",
      entries: [{ concept_id: "locked-community", active_version: 2, source_term: "community", target_term: "wspólnota", notes: "Local choice" }]
    } });
    expect((await view.findByRole("textbox", { name: "Source term" })).value).toBe("community");
  });
  it("edits accepted contextual target variants as one rendering per line", async () => {
    const onSave = vi.fn().mockResolvedValue(true);
    const entry = {
      concept_id: "concept-1",
      active_version: 1,
      source_term: "freireligiös",
      target_term: "free-religious",
      notes: "Historical movement",
      scope: "document",
      updated_at: null,
      usage: {
        sense: "",
        match_policy: "preferred",
        source_forms: ["freireligiös"],
        accepted_target_forms: ["Free Religious"],
        observations: [],
        provenance: "human-owner"
      }
    };
    const view = render(BlockGlossaryPanel, {
      props: {
        entries: [entry],
        proposals: [],
        suggestions: [],
        loaded: true,
        accessRole: "owner",
        onSave
      }
    });

    expect(view.getByText("Accepted target variants: Free Religious")).toBeTruthy();
    await fireEvent.click(view.getByRole("button", { name: "Edit" }));

    const variants = view.getByRole("textbox", {
      name: /Accepted contextual variants/
    });
    expect(variants.value).toBe("Free Religious");
    await fireEvent.input(variants, {
      target: { value: "Free Religious\nFree-Religious Society" }
    });
    await fireEvent.click(view.getByRole("button", { name: "Save for next run" }));

    expect(onSave).toHaveBeenCalledOnce();
    expect(onSave.mock.calls[0][0]).toEqual(expect.objectContaining({
      concept_id: "concept-1",
      source_term: "freireligiös",
      target_term: "free-religious",
      accepted_target_forms: ["Free Religious", "Free-Religious Society"]
    }));
  });
});
