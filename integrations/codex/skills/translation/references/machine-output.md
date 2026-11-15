# Machine and LLM output

How machine-generated translation fails, how to edit it, what self-refinement can and cannot
do, and what to put in a brief for another model. Read when post-editing or reviewing machine
output, when your own draft will not be checked by anyone else, and when you delegate
translation to another model. The failure modes in section 1 are yours as well.

## Contents

1. Characteristic failures
2. Editing a machine draft
3. Refinement and model feedback
4. Briefing another model

## 1. Characteristic failures

The research this section rests on does not agree on proportions (some studies find LLM
errors mostly in fluency and usage, others stress omission and invented content), so check
both sides. What is consistent:

**Fluency hides the errors.**

- A smooth draft is the high-risk case. Reviewers, human or model, skip or wave through text
  that reads well, and vigilance falls further as average quality rises. Check a fixed sample
  in full against the source instead of checking only what looks doubtful.
- **Read asking what is missing, not what is wrong.** Current systems drop small
  meaning-bearing elements inside otherwise flawless sentences: a modal, a hedge, a negation,
  a subject, a qualifier. Nothing looks anomalous.
- An exact match from a memory, a glossary hit or a rendering carried over from an earlier
  block is not safe either. Similarity scores measure string overlap (an antonym can outscore
  a synonym). Re-read the whole proposal in the new context, including figures, punctuation
  and tags; people fix the highlighted difference and miss the second small one.

**Typical content errors.**

- **Words with no counterpart in the source:** plausible invented terms, wrong names of
  institutions, a foreign name replaced by a similar-looking one, explanatory additions,
  images that crept in during a polishing pass.
- **Rare items silently lost:** words and compounds that occur once or twice in the document,
  coinages, unusual inflections. List them and confirm each has a counterpart. A smoother
  sentence that paraphrases one away is not acceptable.
- **Words carried over untranslated,** especially rare, archaic or compound words. Each must
  be a deliberate retention or it is a lost translation.
- **The wrong field's sense.** A general word with a strong sense in some domain is rendered
  with the sense the model has seen most, not the one this text's subject requires.
- **Literal exactly where the source is idiomatic.** Output is freer than the source overall
  and turns word-for-word at metaphors, colloquialisms and unusual expressions, the places
  where a literal rendering cannot be recovered by the reader.
- **Third-language intrusions.** Characters or words belonging to neither language, a switch
  of language mid-sentence, name forms that betray a pivot language. Most likely in pairs the
  model handles less well.

**Typical shape errors.**

- **The narrow default.** Asked only to "translate", a model carries source text conventions
  into the target and prefers bland mid-register wording and the most common norm. Rare,
  innovative or marked renderings are under-used and non-standard source features get
  normalised to the majority form.
- **Reduced variety.** Fewer distinct words and forms than natural writing, common nouns where
  the source named things precisely.
- **Unrequested rewriting.** Added explicitness and ease-of-reading changes, condensation,
  simplification, summarising, a change of style. None of these is quality; all are departures
  to be undone unless asked for.

**Typical behaviour errors.**

- **Answering instead of translating.** Questions, instructions and prompt-like strings in the
  source are content. Check the output for added answers, explanations or closing commentary,
  and for wording of your own instructions that has leaked into it.
- **Filling gaps by invention.** A model answers even when it has nothing to answer with: the
  "official" translation of a title, a name it cannot verify, a citation. Mark such items as
  unresolved instead.
- **Refusing, omitting or slanting sensitive material.** Do not return a block empty, do not
  drop or soften the offending passage, and do not pass altered output off as faithful. If you
  will not translate something, say so explicitly where the user will see it. Reproduce
  non-linguistic material (URLs, code, tokens) in place.
- **Degeneration.** The same content word recurring within a few lines, vague abstractions
  standing in for concrete detail, clauses with no source counterpart, a target much longer
  than its source. These are generative failures: re-translate the block from the source, with
  a shorter span, instead of patching the fluent text.
- **Recurring errors.** The same mistake recurs across a document and a corrected one does not
  stay corrected. After fixing one instance, search the whole target for the pattern.

**Where to trust yourself least.** Do not carry your confidence across languages. Into or out
of a language with a small digital footprint, with older stages of a language, and with dense
specialist vocabulary, first-draft fluency is unreliable: verify every segment, and say so in
the confidence you express.

## 2. Editing a machine draft

- **Triage each segment before touching it:** acceptable (leave it alone), near miss (edit to
  the level asked), failed (discard and translate from the source). Re-translate when roughly
  a quarter or more of the words would have to change, or when the sentence goes wrong early:
  generation is incremental, so an early bad choice contaminates what follows, and a poor
  draft on the page blocks better solutions.
- **Read the source first and form your own version** before looking at the draft; then keep
  the machine's words where they match or beat yours.
- **Spend scrutiny where repair is expensive:** idiomatic expressions, word order problems
  that cross phrase boundaries, untranslated words, wrong part of speech. Word-form slips and
  synonym choices are cheap.
- **Check error classes separately** (omission, addition, lexical choice, word order,
  collocation, grammar). Each has a different fix and some are invisible to a fluency read.
- **Similarity to the draft is not a goal, and neither is difference.** Under a light brief
  keep everything that is acceptable; never keep an error to reduce edits; and where nothing
  depends on a choice, accept the draft's option instead of varying for its own sake.
- **Machine-transcribed sources** (speech recognition followed by translation): fix the
  transcription first, since translation errors may come from misheard words.
- **Automatic flags are candidates.** A flagged term may legitimately appear as a pronoun, an
  inflected form or a synonym. Report verified errors and unconfirmed flags separately.

Levels of post-editing and the general rules of restraint are in `revision.md`.

## 3. Refinement and model feedback

- **One round, unless the feedback is verified.** Repeating an estimate-and-correct loop
  degrades output: a weak estimate invents errors and the next round "fixes" them. An
  unprompted judgement call over-reports serious errors (mistranslation, addition,
  untranslated text). Verify each reported error against the source before acting on it.
- **When refinement stops helping, improve the diagnosis, not the number of rounds:** more
  context, terminology, reference material, a human answer.
- **Revise against specific findings about this text.** A bare quality score is the weakest
  guidance. Naming the passages with a severity is nearly as useful as full annotation.
  Generic "translate, then self-correct against a checklist" routines have lowered quality in
  literary work.
- **Route effort by draft quality.** Weak and out-of-domain blocks gain most from a second
  pass; a strong draft gains little and can regress. A stronger model also tends to declare
  its own output acceptable, so do not read "no issues found" as verification.
- **A naturalness pass works; a naturalness instruction does not.** Adding "make it sound
  native" to the translation request changes little. A separate pass that revises the finished
  draft for natural target phrasing does help. Constrain that pass to small edits of the
  existing draft: each pass that moves further from the source raises the chance of invented
  content, so re-check for additions afterwards.
- **A document-context pass fixes only what context can fix:** pronouns, ambiguous words,
  ellipsis. It does not repair word-for-word rendering or look-alike word choices.
- **Research and pre-analysis output is a list of hypotheses.** Expect false alarms on phrases
  a plain rendering handles well, and invented cultural explanations. Do the research over the
  whole document, not isolated segments; many apparent problems disappear with context.
- **For an ambiguous form, do not trust the first answer,** yours or another model's. The most
  frequent reading comes first; alternatives appear only when the question is put again or
  checked against a dictionary or parallel text.

## 4. Briefing another model

When you hand translation or review to another model or an MT system:

- **State the purpose, readers and target conventions,** then inspect the first output for
  source conventions carried over. Output does not adapt to a brief it was not given.
- **Never send a very short unit alone.** Headings, labels, table cells and list items of up
  to about five words vary most in machine output. Pass the surrounding text, or at least what
  the item labels.
- **Examples:** only real source-target pairs, in the requested direction, few and good. One
  example can be worse than none, quality matters more than similarity or number, and the
  examples nearest the text to be translated weigh most.
- **Term hints:** only for rare or domain-specific words, each paired with its source term (a
  list of target words alone lowers quality), in the order the terms occur in the text. A long
  term list can reduce quality when good example sentences are already present.
- **Name the script and variant** when the target language has more than one.
- **Check templates reused across language pairs** for a stale language name; the model will
  obey it.
- **Do not stack instructions.** A role, a description of the source and a list of quality
  criteria together can cancel each other. Evidence on personas is mixed: change one thing at
  a time and keep what measurably helps.
- **Span size.** A whole paragraph in one call gives fewer errors than sentence by sentence,
  including for who is speaking and what pronouns refer to. Long blocks drop details, so run
  an omission check on them, and shorten the span if repetition or bloat appears.
- **Output channel.** If the result feeds a pipeline, the translation field contains one
  rendering and nothing else: no preface, no alternatives, no commentary. Doubts, assumptions
  and source defects go in whatever separate channel the workflow provides.
- **Weak non-English pairs.** Compare a direct translation with a two-step one through a
  well-resourced language instead of polishing a poor direct result, and check the pivot
  version for the pivot's idiom.
- **Preparing a source for a machine.** Unpack nested subordinate clauses, make each pronoun's
  referent explicit, restore dropped subjects and punctuation. Make relations explicit
  rather than merely shortening. Keep this machine-facing version separate from the text
  people read.
