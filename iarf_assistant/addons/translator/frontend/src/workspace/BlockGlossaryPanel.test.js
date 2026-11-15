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

  it("classifies terms, filters unverified ones and saves kind and verification", async () => {
    const onSave = vi.fn().mockResolvedValue(true);
    const usage = (kind, verification) => ({ sense: "", match_policy: "preferred", source_forms: ["x"], observations: [], provenance: "human-owner", kind, verification });
    const entries = [
      { concept_id: "c-1", active_version: 1, source_term: "Allgemeine Zeitung", target_term: "Allgemeine Zeitung", notes: null, scope: "document", updated_at: null, usage: usage("do_not_translate", "verified") },
      { concept_id: "c-2", active_version: 1, source_term: "Gemeinde", target_term: "parish", notes: null, scope: "document", updated_at: null, usage: usage("term", "unverified") }
    ];
    const view = render(BlockGlossaryPanel, { props: { entries, proposals: [], suggestions: [], loaded: true, accessRole: "owner", onSave } });

    expect(view.getByText("Do not translate · Verified")).toBeTruthy();
    expect(view.getByText("Unverified")).toBeTruthy();
    await fireEvent.click(view.getByLabelText("Show only unverified entries (1)"));
    expect(view.queryByText("Do not translate · Verified")).toBeNull();

    await fireEvent.click(view.getByRole("button", { name: "Edit" }));
    expect(view.getByLabelText("Verification").value).toBe("unverified");
    await fireEvent.change(view.getByLabelText("Kind"), { target: { value: "do_not_translate" } });
    expect(view.getByText(/uses the source term as its translation/)).toBeTruthy();
    expect(view.getByRole("button", { name: "Save for next run" }).disabled).toBe(true);
    await fireEvent.change(view.getByLabelText("Kind"), { target: { value: "name" } });
    await fireEvent.change(view.getByLabelText("Verification"), { target: { value: "verified" } });
    await fireEvent.click(view.getByRole("button", { name: "Save for next run" }));
    expect(onSave.mock.calls[0][0]).toEqual(expect.objectContaining({ concept_id: "c-2", kind: "name", verification: "verified" }));
  });
});
