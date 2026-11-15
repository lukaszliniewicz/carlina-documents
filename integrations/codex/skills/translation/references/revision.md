# Revision: checking and improving a translation

Read this before revising any translation: your own draft, someone else's, or machine output.
For scoring or formally assessing a translation, see `evaluation.md`. For failure patterns
specific to machine and LLM drafts, see `machine-output.md`.

## Contents

1. The stance: restraint
2. Which passes, in which order
3. What to check that fluent reading hides
4. Making and verifying changes
5. Other people's material: drafts, memories, suggestions
6. Reporting

## 1. The stance: restraint

Studies of revisers agree on the main failure: too many changes, not too few. Unnecessary
edits outnumber necessary ones, each edit can introduce a new error, and revisers who state
that they avoid needless changes still make them. Models asked to "review" or "improve" a text
have the same bias, more strongly.

- **Ask "does this need changing?", never "can this be improved?".** Almost any sentence can
  be improved in someone's eyes. A different wording you would have chosen is not a defect.
- **Name the defect before changing anything.** A change needs a ground: a meaning error, an
  omission or addition, a rule of the language, a requirement of the brief, a glossary or
  authority, a passage the reader would misread. "It sounds better" is not a ground; revert
  such a change. If you cannot name the defect, leave the text.
- **Separate corrections from preferences.** Corrections go into the text. Preferences are
  offered as suggestions or dropped, according to what was asked. Under a light or
  error-fixing brief, the delivered text contains corrections only.
- **When two wordings are both acceptable, the existing one stands.** Take the first adequate
  alternative when a change is needed; do not go on generating options.
- **Before correcting something that looks wrong, construct the reading in which it is
  right.** If that reading fits the context, leave the passage. Introducing an error is worse
  than missing one. In someone else's translation, do not fix an early oddity at once: note
  it, read to the end, and return; the choice may rest on something that comes later.
- **Count only misreadings a reader would actually make.** Every sentence is ambiguous in
  isolation. A problem that requires ignoring the context is not a problem.
- **The override is comprehension.** If a passage cannot be understood on one reading at
  normal speed, or only by looking at the source, it must be changed, whatever the restraint.
- **Stop.** End revision when a pass produces only trivial or equivalent swaps. Repeated
  refinement passes drift away from the source and start to introduce errors. Report what
  remains instead of grinding on.
- **Be most restrained where you are weakest.** In a target language you command less well,
  fix only defects you can positively identify. In that situation more changes produce worse
  text.

Two cases where restraint means not revising at all:

- **A failed draft.** If the first stretch shows serious defects throughout, stop and
  retranslate. Patching a failed draft costs more and leaves it half-repaired.
- **A wrong strategy.** If the draft is formal where it should be informal, or free where it
  should be close, report that. Converting it wholesale is a second translation, not a
  revision, and needs a decision from whoever asked.

## 2. Which passes, in which order

**Two readings with different aims, never merged.**

1. *Target alone, source out of view, start to finish.* Read it as its reader will. Mark
   doubtful passages; do not fix yet. This is the only pass that catches defects living
   across sentences: reference, logic, list items that break the pattern, register slips,
   a paragraph that says nothing. Comparison pulls attention down to the sentence and hides
   them.
2. *Comparison with the source, sentence by sentence.* This is the only pass that can verify
   meaning. A target-only pass cannot detect a wrong content word that reads plausibly, and
   its clean result is not evidence of accuracy.

If only one pass is possible, choose by risk: when mistranslation or omission is likely
(difficult source, unknown translator, machine draft) or would do serious damage, compare with
the source; otherwise read the whole target without sampling. State which one you did.

**Do not judge a draft in the stance that produced it.** A drafter re-reading at once sees the
intended meaning, not the words. For a model the equivalent of a night's sleep is a fresh
context: a pass that has the source and the target but not the drafting conversation, or at
the very least a pass made after the whole draft is finished rather than segment by segment.
Do not let the drafter's notes or reasoning stand in for judging the text itself.

**Detect first, correct afterwards.** In the first pass list suspect places with a reason and
write no replacements. Composing a better wording stops the search for problems, and "noted
and left alone" is a normal outcome for many candidates.

**Reading order inside the comparison depends on the bias to avoid.**

- Checking a finished translation: read the target sentence first, then the source. Reading
  the source first primes you with the intended meaning and makes clumsy or ambiguous target
  wording invisible.
- Working from a draft that you will edit (machine output, a supplied translation): read the
  source first and form your own understanding before looking at the draft. Otherwise the
  draft's interpretation replaces the source's, which is how accuracy errors in fluent drafts
  survive.
- Work in whole sentences at least, never phrase by phrase.

**Order of concerns.** Content first (accuracy, completeness), then coherence and clarity,
then style and register, then grammar, spelling and punctuation, and last consistency of
terms, names, numbers and abbreviations. Surface fixes made before content fixes get redone.
While attending to one concern, note problems of another kind without fixing them, and never
let one correction grow into restyling the sentence.

**Start where drafting was hardest.** Passages where you hesitated, weighed alternatives or
settled for something vaguer or more generic than its surroundings are where the first
solution is least reliable.

## 3. What to check that fluent reading hides

**Fluency is a risk factor, not reassurance.** The smoother the draft, the more a meaning
error will survive, because revisers do not question what reads well. "It reads well, so I
left it" is a warning sign. Sample for accuracy by risk, not by where the text sounds odd.

**Completeness is a separate, mechanical check.** Accuracy checking does not find what is
missing. Count paragraphs, sentences and list items in source and target and account for
every difference (a mismatch may be a merge or split, not an omission). Then sweep what lies
outside the running text: headings, captions, labels in figures, table cells, footnotes,
headers and footers. A short closing clause with no counterpart is an omission, not
compression.

**A fixed list of small things that go wrong.** Target words with no support in the source;
source words not rendered; words left in the wrong language; changed numbers, units and
currencies; changed names of people, organisations and places; casing; punctuation and symbols
that differ from the source without reason; URLs, e-mail addresses, handles and codes that
were "translated"; acronyms expanded without support. Run this list after every round of
editing as well, because editing itself creates omissions, additions and terminology slips.

**Weighted positions.** Compare title, opening paragraph and closing lines first and weight
differences there above mid-text: they frame how everything else is read. At the end, check
each heading against its section, the numbering sequence and the table of contents, and check
summaries and contents last, after the body has stopped changing.

**Second readings created by target grammar.** After the meaning comparison finds nothing,
look for target sentences whose grammar admits another reading: what a modifier attaches to,
the scope of a negation, what a pronoun refers to. The drafter knows which reading was meant
and cannot see the other.

**Drift that only shows in aggregate.**

- *Terminology:* list each recurring term with every target form used for it across the whole
  document, then resolve the variants. Segment-by-segment checking does not reveal them.
- *Intensification:* added intensifiers, stronger evaluative synonyms and added adverbs with
  no counterpart in the source accumulate, especially during revision. List and review them.
- *Explicitation and flattening:* added agents, spelled-out circumstances, added connectives,
  dropped emphasis and normalised punctuation. Restore what the author left implicit when
  terseness or difficulty is part of the text.
- *First literal renderings:* the closest equivalent of a metaphor or idiom chosen on the
  first pass is an unexamined default. Re-test each one.
- *Recurrent choices:* judge a repeated rendering by what its total distribution implies
  (stance, emphasis, who is included), not instance by instance.

**Source showing through, and the opposite fault.** Two questions for any candidate wording:
can the source be recovered from it, and is it probable in comparable texts written in the
target? Correct towards whichever side is weaker.

- The first literal rendering is the cheapest one and comes pre-primed by the source. Give the
  heaviest scrutiny to what came easiest, and re-derive every target phrase that mirrors the
  shape of its source phrase from what that phrase does in context.
- Interference distorts frequencies both ways. Source-like patterns are overused; target
  constructions with no source counterpart (particles, impersonal forms, verbless sentences,
  diminutives, whatever the target has and the source lacks) go missing, and no sentence-level
  check notices their absence. Ask what a native writer would have used here that the source
  could never have suggested.
- Over-monitoring is the opposite defect: normalising until the author's distinctive choices
  are gone. Test each oddity in the draft with one question: can the target reader recover a
  reason for it? If so it stays; if not, it is noise and is normalised.
- Explicitation runs by direction, not by habit. Some language pairs call for more to be
  spelled out one way and less the other (English into German against German into English).
  Check which way this pair runs for this genre before adding or removing anything.
- If drafts keep coming out unidiomatic, change the drafting, not the revision: write the
  target sentence for naturalness without looking back at the source, then repair completeness
  and accuracy in the comparison pass.
- Knowing these tendencies does not prevent them. They are caught by a separate check, not by
  good intentions during drafting.

**Context overrides local acceptability in both directions.** Wording that looks odd alone
but fits the document is not an error; wording that is fine in general but wrong for this
document is.

**Lost effect or compensation?** Before recording a lost effect or an unusual construction as
a defect, check whether the source has a comparable deviation that licenses it, and whether
the effect was recreated elsewhere in the passage.

## 4. Making and verifying changes

- **Smallest change that removes the defect.** One word, one moved phrase, one comma. Rewrite
  the sentence only if nothing smaller works, and never rewrite neighbouring text that is not
  itself defective.
- **Re-read the seam.** After every edit re-read the whole sentence and its neighbours: the
  new word's grammar and complements, agreement, pronoun reference, a word duplicated or
  dropped at the join, a repeated root. Errors are introduced exactly where attention was
  narrow.
- **A revision is a claim until verified.** Diff the versions. Confirm that every change you
  say you made is present, that each removed its defect, and that units with nothing wrong
  came back unchanged. Classify what the diff shows: correction, unnecessary change, new
  error. A span deleted and retyped identically, or a dense cluster of changes in one
  passage, means there was no criterion.
- **Check the source side after polishing.** A refined rendering can become more idiomatic
  while quietly losing a nuance the source carried.
- **Feedback from a model is not ground truth,** including your own earlier pass. Before
  acting on a reported error, verify it against the source. A plausible diagnosis of a
  problem that is not there damages correct text.
- **Anchoring on a draft.** After editing a draft, make one pass that judges each sentence as
  if the draft had never existed. Wording that survives only because it was already on the
  page is what an editor most often fails to change.
- **The label must not set the standard.** Apply the same scrutiny whether the text is
  announced as a human translation or as machine output; the label invites too much restraint
  in one case and too much rewriting in the other.
- **Post-editing levels are specifications, not degrees of care.** Light: comprehensible and
  accurate, nothing added or omitted, as much of the draft kept as possible, style left alone.
  Full: also style, terminology, conventions and formatting to the standard of a translation.
  Work from the draft either way unless it fails as a draft.

## 5. Other people's material

- **A translation supplied with the job is an uncorrected draft,** not a reference. Check it
  clause by clause and do not adopt its terminology on trust.
- **A memory or earlier translation proposes; it does not decide.** Check each match against
  the current source, especially numbers, dates and negations. Do not replace a good new
  wording with an older one merely because the older one exists.
- **A binding or published text is not yours to improve.** In an authenticated, adopted or
  already published translation, leave the established wording, mark the passage and put the
  proposed change in a note.
- **Suggestions from the author, client or a subject expert:** their authority covers the
  meaning, not target-language usage. Check whether the suggestion corrects an error or only
  replaces your word with a synonym they know better. Equally, do not dismiss a suggestion
  because you are confident in your version; assess it on its merits.
- **Same instrument, same formula.** For a doubtful formulaic expression in a statute, treaty,
  contract or standard, look for other places in the same document or corpus where the source
  expression was already translated.

## 6. Reporting

- Say what kind of revision was done: target-only or compared with the source, which parts of
  the text, and which concerns were in scope. "Reviewed" tells the reader nothing.
- For each substantive change or query give the location, the quoted span, what is wrong and
  why, and the correction. Authors and translators can check a quoted span with a reason;
  bare category labels are harder to use and less reliable. A plain flag is enough for an
  obvious slip.
- Where you are not highly certain, leave the text and raise a comment carrying the
  alternative, so that an optional improvement never overwrites the original.
- Report source defects, assumptions and unresolved doubts separately from corrections.
