import { describe, expect, it } from "vitest";
import {
  beginDraftSave,
  createDraftState,
  failDraftSave,
  finishDraftSave,
  mergeDraftState,
  rebaseDraft,
  updateDraft
} from "./drafts.js";

describe("translator draft state", () => {
  it("preserves dirty drafts when polling receives server rows", () => {
    let state = mergeDraftState(createDraftState(), [{ id: 1, translated_text: "server", version: 2 }]);
    state = updateDraft(state, 1, "local edit");
    const merged = mergeDraftState(state, [{ id: 1, translated_text: "new server", version: 3 }]);
    expect(merged.entries["1"].value).toBe("local edit");
    expect(merged.entries["1"].server.translated_text).toBe("new server");
    expect(merged.entries["1"].dirty).toBe(true);
  });

  it("does not clear a newer edit when an older save resolves", () => {
    let state = mergeDraftState(createDraftState(), [{ id: 1, translated_text: "old", version: 2 }]);
    state = updateDraft(state, 1, "first");
    const begun = beginDraftSave(state, 1);
    state = updateDraft(begun.state, 1, "second");
    const result = finishDraftSave(state, begun.snapshot, { id: 1, translated_text: "first", version: 3 });
    expect(result.applied).toBe(false);
    expect(result.state.entries["1"].value).toBe("second");
    expect(result.state.entries["1"].dirty).toBe(true);
  });

  it("retains the draft and records a recoverable version conflict", () => {
    let state = mergeDraftState(createDraftState(), [{ id: 1, translated_text: "server", version: 2 }]);
    state = updateDraft(state, 1, "local");
    const begun = beginDraftSave(state, 1);
    const conflict = Object.assign(new Error("changed"), { status: 409, payload: { latest_segment: { id: 1, translated_text: "new server", version: 3 } } });
    const next = failDraftSave(begun.state, begun.snapshot, conflict);
    expect(next.entries["1"].value).toBe("local");
    expect(next.entries["1"].conflict.status).toBe(409);
    expect(next.entries["1"].inFlight).toBe(false);
    expect(next.entries["1"].server.version).toBe(3);
  });

  it("rebases a local draft onto the latest version without changing its value", () => {
    let state = mergeDraftState(createDraftState(), [{ id: 1, translated_text: "server", version: 2 }]);
    state = updateDraft(state, 1, "local");
    const begun = beginDraftSave(state, 1);
    state = failDraftSave(begun.state, begun.snapshot, Object.assign(new Error("changed"), {
      status: 409,
      payload: { latest_segment: { id: 1, translated_text: "other", version: 3 } }
    }));
    const rebased = rebaseDraft(state, 1);
    expect(rebased.entries["1"]).toMatchObject({ value: "local", version: 3, dirty: true, conflict: null });
  });

  it("ignores an older response after a newer save begins for the same segment", () => {
    let state = mergeDraftState(createDraftState(), [{ id: 1, translated_text: "server", version: 2 }]);
    state = updateDraft(state, 1, "local");
    const first = beginDraftSave(state, 1);
    const second = beginDraftSave(first.state, 1);
    const result = finishDraftSave(second.state, first.snapshot, { id: 1, translated_text: "old response", version: 3 });
    expect(result.applied).toBe(false);
    expect(result.state.entries["1"].inFlight).toBe(true);
    expect(result.state.entries["1"].value).toBe("local");
  });
});
