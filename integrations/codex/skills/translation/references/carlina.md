# Using this skill inside Carlina

Read this when the translation runs through Carlina's Translator (MCP tools named
`carlina_translator_*`, work claimed in block windows). It maps the craft rules in this skill
onto what Carlina provides and, more importantly, onto what it does not.

How to operate Carlina (creating projects, claiming and submitting work, recovery, exports)
is in the `carlina-document-workflows` skill. Where that skill, a tool schema or a claimed
packet differs from anything here, they win: this file describes the workflow as it stood in
November 2026 and packets can carry an older frozen contract.

## 1. Who does what

| Craft step in this skill | In Carlina |
|---|---|
| Purpose, readers, quality level | Target language, annotation profile and the translation brief (register, variety, overt or covert, form of address, purpose, readers) are project settings. Everything else is yours to establish and record in a research dossier. |
| Orientation read, style analysis, term harvest | Run by Carlina only in managed mode (pre-analysis, research). **In passive mode nothing does this unless you do.** |
| Style sheet and standing decisions | Research dossier, pinned when the run starts. |
| Glossary | Project glossary: human-locked rows are binding; rows you propose are advice to later packets. |
| State across blocks | Packet context: preceding excerpt, exact and sentence precedents, linked notes, glossary selection. |
| Flags, assumptions, source defects | **No field for them in the translation response.** See section 4. |
| Completeness, numbers, names, meaning | Numbers, bracket and quotation pairing and (with aligned sentences) short sentence groups are checked by QA; names and meaning are not. See section 5. |
| Two review readings | Review tools plus coverage records (`target_reading`, `bilingual`). |

## 2. Before the first window (passive mode)

A passive run starts translating at block one with no document brief, no style guide and no
terminology unless you supply them. Do the work of `long-documents.md`, section 1, with the
review tools (outline, search, fetch) before calling start:

1. Read enough of the whole document to state its genre, purpose, voice and the classes of
   problem it will produce.
2. Write the standing decisions down: form of address, register, tense of narration,
   handling of names, titles and quotations, spelling variant, transliteration, notes policy,
   how each class of problem is treated.
3. Harvest recurring terms and names and settle their equivalents.
4. Save 1 and 2 as a research dossier and pass its revision when starting, so that every
   window receives it. Conversation memory does not survive compaction or a change of worker,
   and a dossier that is saved but not pinned reaches no packet.
5. Save 3 as glossary entries before the run, with `sense` to separate concepts, the source
   forms you observed, and in `notes` what a later window needs to use the term correctly:
   grammatical gender and citation form in a gendered target language, register, when an
   alternative applies. Set `kind` for names, titles of works, fixed formulae and strings
   kept untranslated, and `verification` whenever you know it: `verified` when you checked
   the rendering against a reference edition or authority, `unverified` when you could not,
   `coined` when no established rendering exists and the one given is yours.

Three properties of the settings to know:

- **A custom prompt is delivered to the translating model as reference data, not as an
  instruction.** Do not rely on it to enforce style or terminology. Terms belong in the
  glossary; decisions belong in the dossier and in your own execution.
- **Set the brief when creating the project** (`translation_brief`): `register`,
  `target_variety` (`en-GB`, `pt-BR`), `translation_type` (`overt`: readers know it
  is a translation and keep its references; `covert`: it should read as if written for
  them), `form_of_address`, and short `purpose` and `readers`. The first
  four reach every packet as instructions; purpose and readers arrive as reference data only.
  Packets frozen before the brief existed carry none of it, and a field you leave out falls
  back to the default (neutral register, no variety). Anything the brief cannot express
  (spelling of names, quotation conventions, tense of narration) still belongs in the dossier.
- **The annotation profile is the notes policy.** `none` means no notes and no inline
  substitute for them; `restrained` allows at most one per block; `study` several. Decide it
  before the run from the brief ("in the text or in a note: one policy").

In managed mode Carlina produces a document brief, style guide and entity register. Treat
them as hypotheses from a first read (see `machine-output.md`, section 3) and correct them
before they steer a whole book.

## 3. Working a window

- **Check which context slots actually arrived.** When a packet does not fit its budget,
  whole optional slots are dropped, research evidence first, then memory, precedents, linked
  notes, the continuity excerpt, style brief and metadata. The source is never truncated, but
  the style brief you are counting on may not be there. In passive mode a newly prepared
  window names what was dropped in `request.dispatch.translator_context_omitted_slots`;
  recover what you need through the review tools before translating (a dropped continuity
  excerpt with `carlina_translator_context_get`, your pinned dossier from your own record).
  Older packets carry no such list: read the packet, do not assume.
- **Re-anchor at every boundary.** In self-contained mode the packet carries a bounded
  preceding excerpt: read its source and target before the first sentence of the window. In
  retained mode, after compaction or a transfer, fetch the preceding aligned groups with
  `carlina_translator_context_get` before continuing. The first sentence of a window is
  where reference, tense and address go wrong.
- **Precedents and memory are proposals.** Exact repeats, sentence precedents and memory
  matches come with similarity scores and lexical flags (changed tokens, negation, modals,
  numbers). Those are string evidence. Check each proposal in the new context as
  `long-documents.md`, section 4, describes.
- **Advisory glossary terms are advice the draft may ignore.** After drafting a window, check
  that each supplied term appears where its source term occurs. Propose new or corrected
  terms before submitting, so that the next packet can carry them.
- **Restructuring is allowed inside a block and nowhere else.** Sentence alignment is N to M,
  so splitting and merging sentences within a block is expressible. Content cannot move
  between blocks, blocks cannot be merged or reordered, and every protected literal, inline
  token and field must be returned. Where the craft rules call for a larger rearrangement,
  translate within the structure and record the problem for review.
- **Headings and titles cannot wait until the end of a run.** Translate them when their
  window comes, list them, and revisit all of them in review once the body is done.
- **Foreign-language passages** follow Carlina's own policy in the packet (preserve the
  passage, translate the surrounding prose, with defined cases for existing translations and
  author-supplied glosses). That policy overrides the general advice in this skill.
- **One rendering, nothing else, in `content`.** No alternatives, no bracketed queries, no
  commentary, no markers for doubt.

## 4. Where doubts go

A reader note is addressed to the reader of the book, capped in number and tied to an anchor in
the target: it is not a place for a translator's doubt. Doubts go into Translator flags, one per
doubt, anchored to its block, with `carlina_translator_flag_create`:

- `assumption`: a reading the text does not settle, with the alternative rejected and why;
- `source_defect`: a misprint, gap or garbled passage in the source;
- `unverified`: a name, date, quotation or fact not yet checked;
- `departure`: a deliberate change of form or content, such as a passage translated closely
  because it could not be construed, or a joke replaced;
- `revisit`: a heading, term or passage to come back to once more of the text is done.

State the doubt and what would settle it; add a short excerpt when it helps find the place. Use a
stable idempotency key per doubt so a retry does not duplicate it. Flags can be raised while a
passive run is active; the block need not have a translation yet. Also:

- suspected defects in the extracted source (structure, missing text) go to the document issue
  tools as well, with the excerpt and candidate readings;
- term decisions and their reasons go into the glossary entry's notes, not a flag;
- after the run reaches a terminal state, work through `carlina_translator_flag_list`, resolve
  or dismiss each flag with a note (`carlina_translator_flag_update`, fenced on its
  `sequence`), and report whatever stays open to the user with block references.

Do not resolve a doubt silently because it was inconvenient to record.

## 5. After the last window

- **Know what the QA diagnostics cover.** They are deterministic checks of structure,
  citation keys, repeated punctuation, unpaired brackets and quotation marks, abnormal
  length ratio, glossary terms missing or diverging, spelling, joined and split tokens, runs
  of source-language words, and (ruleset `translator-qa-rules-v2` and later) numbers present
  on one side only. In aligned-sentence projects they also report a sentence group much
  shorter than the rest of its block; `summary.alignment_coverage` says whether that check
  ran, and when it is `unavailable` you compare sentence coverage yourself. They do not
  compare names with the source, do not see an omission inside a sentence group of normal
  length, and say nothing about negation, modality or meaning. A clean diagnostics run is
  not a review, and a finding is evidence to look at, not a verdict.
- **Dismiss a finding only after looking at it,** with `carlina_translator_qa_disposition_set`:
  `false_positive` when the check misread the text, `intended` when the translation departs on
  purpose, and a reason either way. A dismissal holds for that version of the block; if an edit
  brings the finding back, it shows again. Report `summary.dismissed_total` with a clean run,
  since a clean run after dismissals is a different claim from one without.
- **Do the checks they leave out** with review search and fetch: names against the source;
  numbers QA marks `possibly_spelled_out` or `possibly_shortened`; each recurring term with
  every form used for it; form of address; passages that recur verbatim in the source; the
  headings you listed; open `revisit` and `unverified` flags. If the ruleset is older than v2, compare
  numbers and dates yourself as well.
- **Record coverage honestly, by kind.** The target-alone reading of `revision.md` is
  `target_reading` (fetch with `view="target_only"`, which withholds the source); the
  comparison with the source is `bilingual`. A sample covers the anchors sampled. Neither
  diagnostics nor a heading check amounts to bilingual review of the book.
- **Edit only at an idle boundary,** through the version-fenced review tools, and apply the
  restraint rules of `revision.md`: `target_update` requires the defect class and a line on
  what was wrong, and both stay with the revision. If no class fits, the change is a
  preference: leave the text.
- **Settle the unverified glossary entries before export.** Read
  `carlina_translator_glossary_get(verification="unverified")`; check each rendering and
  mark it `verified`, or tell the user which renderings remain unconfirmed. Mention
  `coined` renderings too: a reader may need a note for them.
- **Report with the scope stated:** which coverage kinds, over which blocks, how many findings
  were dismissed, and which flags remain open.

## 6. Multi-volume and shared work

Terminology shared between volumes goes through shared glossaries, adopted at an idle
boundary. Before adopting a shared term, check that it is the same concept in this book
(`names-terms-quotations.md`, section 4), and contribute corrections back; frequency in a
shared glossary is not evidence of correctness.
