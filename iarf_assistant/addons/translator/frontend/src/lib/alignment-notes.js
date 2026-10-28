export function noteElementId(blockId, noteId) {
  return `reader-note-${blockId}-${noteId}`;
}

export function alignmentNotePositions(groups, notes) {
  const positions = { source: new Map(), target: new Map(), unplaced: [] };
  const texts = {};
  for (const side of ['source', 'target']) {
    let text = '';
    const spans = groups.flatMap((group) => group[`${side}_sentences`]).map((sentence) => {
      if (text) text += ' ';
      const start = text.length;
      text += sentence.text;
      return { order: sentence.order, start, end: text.length };
    });
    texts[side] = { text, spans };
  }
  notes.filter((note) => note.state !== 'retired').forEach((note, index) => {
    const item = { note, number: index + 1 };
    const side = note.anchor?.kind;
    const source = texts[side];
    const phrase = note.anchor?.text;
    if (note.state !== 'active' || !source || !phrase) {
      positions.unplaced.push(item);
      return;
    }
    let start = -1;
    for (let occurrence = 0; occurrence <= (note.anchor.occurrence_index || 0); occurrence++) {
      start = source.text.indexOf(phrase, start + 1);
      if (start < 0) break;
    }
    const end = start + phrase.length;
    const span = start < 0 ? null : source.spans.find((span) => end > span.start && end <= span.end);
    if (!span) { positions.unplaced.push(item); return; }
    const entries = positions[side].get(span.order) || [];
    entries.push({ ...item, offset: end - span.start });
    positions[side].set(span.order, entries);
  });
  return positions;
}

export function sentenceNoteParts(text, entries = []) {
  let offset = 0;
  const parts = [...entries].sort((a, b) => a.offset - b.offset).map((entry) => {
    const part = { text: text.slice(offset, entry.offset), entry };
    offset = entry.offset;
    return part;
  });
  parts.push({ text: text.slice(offset), entry: null });
  return parts;
}
