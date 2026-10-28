/**
 * Draft merge/save primitives. They intentionally contain no Svelte code so
 * polling and optimistic-save races can be tested without a browser.
 */
export function createDraftState() {
  return { entries: {} };
}

export function mergeDraftState(state, segments) {
  const next = { entries: {} };
  const rows = Array.isArray(segments) ? segments : [];
  for (const segment of rows) {
    const id = String(segment?.id ?? "");
    if (!id) continue;
    const previous = state?.entries?.[id];
    const preserve = previous && (previous.dirty || previous.inFlight || previous.conflict);
    next.entries[id] = preserve
      ? { ...previous, server: segment }
      : {
          value: segment.translated_text || "",
          version: segment.version ?? null,
          generation: previous?.generation || 0,
          saveGeneration: previous?.saveGeneration || 0,
          dirty: false,
          inFlight: false,
          conflict: null,
          server: segment
        };
  }
  return next;
}

export function updateDraft(state, id, value) {
  const key = String(id);
  const previous = state.entries[key] || {
    value: "",
    version: null,
    generation: 0,
    saveGeneration: 0,
    dirty: false,
    inFlight: false,
    conflict: null,
    server: null
  };
  return {
    ...state,
    entries: {
      ...state.entries,
      [key]: {
        ...previous,
        value: String(value ?? ""),
        generation: previous.generation + 1,
        dirty: true,
        conflict: null
      }
    }
  };
}

export function beginDraftSave(state, id) {
  const key = String(id);
  const entry = state.entries[key];
  if (!entry) return { state, snapshot: null };
  const snapshot = {
    id: key,
    value: entry.value,
    expectedVersion: entry.version,
    generation: entry.generation,
    saveGeneration: entry.saveGeneration + 1
  };
  return {
    state: {
      ...state,
      entries: { ...state.entries, [key]: { ...entry, inFlight: true, saveGeneration: snapshot.saveGeneration } }
    },
    snapshot
  };
}

export function finishDraftSave(state, snapshot, serverSegment) {
  if (!snapshot) return { state, applied: false };
  const current = state.entries[snapshot.id];
  if (!current) return { state, applied: false };
  if (current.saveGeneration !== snapshot.saveGeneration) return { state, applied: false };
  const hasNewerEdit = current.generation !== snapshot.generation;
  const nextEntry = {
    ...current,
    server: serverSegment,
    version: serverSegment?.version ?? current.version,
    inFlight: false,
    dirty: hasNewerEdit,
    conflict: null
  };
  if (!hasNewerEdit) {
    nextEntry.value = serverSegment?.translated_text || "";
  }
  return {
    state: { ...state, entries: { ...state.entries, [snapshot.id]: nextEntry } },
    applied: !hasNewerEdit
  };
}

export function failDraftSave(state, snapshot, error) {
  if (!snapshot || !state.entries[snapshot.id]) return state;
  const current = state.entries[snapshot.id];
  if (current.saveGeneration !== snapshot.saveGeneration) return state;
  return {
    ...state,
    entries: {
      ...state.entries,
      [snapshot.id]: {
        ...current,
        inFlight: false,
        conflict: error?.status === 409 ? error : current.conflict,
        server: error?.status === 409
          ? (error.payload?.latest_segment || error.payload?.segment || current.server)
          : current.server
      }
    }
  };
}

export function rebaseDraft(state, id) {
  const key = String(id);
  const entry = state.entries[key];
  if (!entry?.server) return state;
  return {
    ...state,
    entries: {
      ...state.entries,
      [key]: {
        ...entry,
        version: entry.server.version ?? entry.version,
        generation: entry.generation + 1,
        dirty: true,
        inFlight: false,
        conflict: null
      }
    }
  };
}
