# Evaluation: assessing and scoring a translation

How to judge a translation you are not rewriting: quality review, comparing candidates,
scoring, acceptance checks, and evaluating another model's output. For improving a text, see
`revision.md`. The research behind this file is blunt about one thing: evaluators, human and
model, are unreliable in predictable ways, so most of the rules are there to constrain the
evaluator.

## Contents

1. The standard you judge against
2. Procedure
3. What counts as an error
4. Severity
5. Scores, metrics and comparisons
6. Your own biases as an evaluator
7. Reporting

## 1. The standard you judge against

- **An error is a failure to do what the job asked.** Establish the brief before marking
  anything. The same omission is an error in a full translation and correct in a summary; a
  version declared as an adaptation is judged as one. Where no brief exists, state the one
  you are assuming.
- **Accept the translator's strategy as the baseline** once you have reconstructed it (close
  or free, whose conventions, which readers), provided it fits the brief. Overturn it only
  for stated reasons, and then as one finding about the strategy, not as two hundred
  findings about its consequences.
- **Evaluate acceptability, not the best version you can imagine.** A rendering that meets
  the mandatory requirements passes. An edit to text that was already fit for purpose is
  unnecessary even when the new wording is better.
- **Name the grade you are testing for.** Good-enough quality is comprehensible and accurate;
  imperfect style does not fail it. Publication quality adds style, conventions and
  presentation. A deliberately literal study version is not marked down for reading badly.
- **Check only what the brief makes relevant,** but always at least one dimension of fidelity
  and one of the target text itself.
- **Know what the translator had.** A term that departs from a glossary is an error only if
  the glossary was supplied. Otherwise it is a query.

## 2. Procedure

1. **Read the source and state its main claims and purpose in your own words.** Record them.
   Severity is later decided against this list, not by feel.
2. **Target alone, start to finish,** as its reader: understandability, logic, naturalness,
   coherence across sentences, things that cannot be true.
3. **Source against target, unit by unit:** meaning, additions, omissions, shifts of force.
   Fluency and adequacy are separate judgements, and neither is inferred from the other. A
   passage judged fluent in step 2 gets the same scrutiny here.
4. **Completeness as its own check.** Marking words can only mark words that are present.
   Align the smallest groups that mean the same; leftover source words are candidate
   omissions, leftover target words candidate additions.
5. **A plausibility pass.** A comparison cannot catch a rendering that is faithful to the
   source and false in the world, or true where the source was written and false for the
   target readers. Check claims against calendar, geography, chronology and institutional
   facts.
6. **Describe before judging.** First record the differences and what kind they are. Only
   then give verdicts, each with the criterion it rests on.

If you can check only part of a text, sample by risk and say what was not examined. Always
include the title, the opening and the final sections: deadline pressure concentrates errors
at the end. If only one pass is possible, compare with the source. Choose passages that
demand reformulation (no direct equivalent, sentences that must be split or merged, implied
meaning, names, incomplete sentences); word-for-word handling there is the clearest signal of
weak work.

In a long document, find passages that recur verbatim in the source (boilerplate, warnings,
headings) and compare their renderings with each other.

## 3. What counts as an error

- **Presume the translator is right.** To mark a rendering wrong you must be able to show an
  alternative that is better overall, not merely better on the point you criticise. Before
  finalising a criticism, draft your own rendering of the passage and compare.
- **Minimal-edit test.** For each candidate error, write the smallest edit that fixes it
  alone. If the edited sentence is not better, the finding was noise. Drop it.
- **Forced or chosen?** A shift imposed by the target language or by a hard constraint
  (space, an obligatory category) is not an error. Record the loss as a constraint. Only
  shifts the translator chose are open to criticism.
- **Look for compensation first.** Before flagging a deletion, a vaguer rendering or an
  addition, search the neighbouring text for the element recreated elsewhere, and ask whether
  this reader needs it. Lost or altered modality is always a meaning shift. Where sections
  are designed to be read independently, compensation in another section does not count.
- **Verify before condemning.** Before calling a wording unidiomatic, non-existent or
  mistranslated, check the context that may license it, its technical and idiomatic senses,
  and usage in comparable target texts. A flag from an automatic checker is a hypothesis
  about the checker's dictionary.
- **One of several defensible variants is not an error.** Separate choices that make no
  sense in context from choices you would have made differently. On interpretive and
  stylistic matters the strongest available verdict is "less well motivated, because...".
- **Check the source before blaming the translator.** If the source has the same flaw,
  record a source defect.
- **An unconventional rendering is acceptable only if the reader can see a reason for it.**
  The translator's or model's own justification does not count; a plausible rationale often
  accompanies a wrong choice. An oddity explicable only by looking at the source is a defect.
- **A correct-looking equivalent does not prove the source was understood.** Verify the
  source meaning independently, especially at cognates and routine pairings.
- **Classify by what is wrong in the text, not by its presumed cause.** If a defect can only
  be seen by comparing with the source, it is an accuracy issue however awkward it also
  sounds. One defect often fits several categories: record the primary one with a reason and
  count it once. When unsure between sub-categories, use the parent.

## 4. Severity

- **By effect on the reader's use of the text,** not by how glaring the fault is and not by
  its category. Minor: usability untouched. Major: the reader is misled or has to work to
  recover the meaning. Critical: that part is unfit for use, or creates risk to safety,
  money, legal position or reputation.
- **By what it damages.** An error touching one of the main claims you recorded, or the
  text's overall structure or purpose, outranks a sentence-level meaning error, which
  outranks a local word choice. Getting audience, place, time or function wrong outranks any
  linguistic slip.
- **By position.** Titles, headings and repeated display strings weigh by what they govern
  and how often they recur. An error that changes how the following text is read weighs more.
- **Recoverability lowers it.** If the damaged statement is restated accurately a sentence or
  two later, the reader recovers; grade it lower.
- **Readers act on meaning errors without noticing them, and notice style errors without
  being harmed by them.** Rank by danger, not visibility. A fluency judgement never offsets
  an accuracy failure.
- **Omission is as serious as a changed meaning.** A dropped clause is not a stylistic slip.
- **Numbers, quantities, dates and units are critical** in financial, scientific, technical
  and legal texts. So is a wrong brand or product name anywhere.
- **Clusters count as one larger defect.** Several errors of one type in a passage (a run of
  wrong connectives) can amount to a major defect though each is minor. Conversely a
  recurring defect is one finding with its frequency and locations, not one per occurrence.
- **In doubt in a safety-critical text, grade upward.**
- **Surface errors are not trivial in a delivered text.** They are what the reader sees, and
  they cost credibility out of proportion to their content.
- **Assign severity per instance.** Then audit yourself: did your standard drift between the
  first passage and the last?

## 5. Scores, metrics and comparisons

- **Errors first, number last.** Never give an overall score and then look for reasons. List
  located errors with severities, then derive the score from the list (for example minor 1,
  major 5, critical 10, divided by length so that texts of different sizes compare). A score
  that cannot be traced to named errors is an impression.
- **A count is a record, not a verdict.** One meaning-changing slip can make a text unusable
  while twenty small awkwardnesses leave it serviceable. Start with a binary question (does
  any defect damage what the text has to do?) and add finer grades on top.
- **Never judge by overlap with a reference translation.** Overlap rewards the reference's
  wording and source-shaped literalness, and punishes a correct paraphrase. A fall in such a
  score after revision is not a fall in quality. A reference is evidence at most; published
  references misread things too.
- **A target-only reading is not a quality verdict.** It registers departures from normal
  usage and internal absurdities, and it prefers the frequent and stereotypical reading.
  Without the source, say that the assessment covers target-language quality only.
- **Back-translation proves little.** A literal, unreadable translation back-translates
  almost perfectly; a mismatch may be the return trip's fault; idiom and style cannot be
  judged from it at all. It has some use for hard referential content.
- **Agreement between systems or drafts rules out random error only.** Models trained on
  similar data share biases, and the cognate-literal rendering is easy for all of them. Use a
  second system as a check only if it is really different, and still verify against the
  source.
- **Pairwise comparison: run it in both orders.** A verdict that flips when the candidates
  swap position is a tie. Between two good translations a single preference is close to
  noise even from experts; annotate errors against the source instead.
- **Blind the labels.** Do not let "human", "machine", a model's name or a translator's
  credentials reach the judgement.
- **Automatic scores are valid at the level they were validated.** A metric or model judge
  that ranks systems well can be poor at the level of a single segment. It does not approve,
  reject or rank an individual translation.
- **Claims of parity with human translation:** check whether sentences were judged in
  isolation and whether form was assessed at all before believing them.
- **Ease is not evidence.** That the work felt easy, that the result reads well, that nobody
  complained, that a reader said "yes, clear": none of these shows that a solution is
  acceptable. Readers cannot detect omissions and mistranslations.

## 6. Your own biases as an evaluator

- **You will find style problems.** Model reviewers skew towards wording and awkwardness.
  Look for mistranslation, omission and addition first and treat your style complaints as
  low-confidence.
- **Your list is probably inflated, mostly with minor items.** Audit those first. Severity
  weighting does not cancel the inflation.
- **You will tend to be bland.** A review that names no specific defect, cites examples only
  from the opening, or discusses the target without quoting the source is incomplete. Force
  decisions: located defects, a consequence for the reader, a ranked choice.
- **"Cohesion and coherence are fine" from a model means nothing.** Model feedback
  concentrates on words and sentences; discourse-level problems are the ones left unreported.
  Check them explicitly.
- **Preferring the safer, more literal candidate.** A version that avoids small accuracy
  risks may be poorer in voice, imagery and rhythm. Ask that question before ranking it
  higher. A diffuse impression that the translation is tamer than the source is a real
  finding: collect the individual toned-down items and report the pattern. Intensity is
  typically reduced in translation and almost never increased, so an increase needs
  explaining too.
- **Where shifts hide.** Concrete nouns and words with an obvious equivalent are stable.
  Evaluative modifiers, adverbials, metaphor and indirect evaluation are where flattening
  happens.
- **Stance is read from patterns.** Record every shift of one type across the text (agents
  removed, actives made passive, terms drifting) and report a trend only where they add up.
  To decide whether one rendering is a shift at all, ask what other renderings the source
  allowed.
- **No finding is not no problem.** Say which classes of problem your check did not cover. A
  checker's silence is read as approval for exactly the things it cannot see.
- **Your labels are less stable than your detections.** The same annotator re-marking the
  same text places errors differently. Rely on whether a problem exists and how severe it
  is; do not build decisions on the exact category or span.

## 7. Reporting

- **Anchor every finding:** the location, the quoted span with enough context, what the
  rendering does to the reader (narrows the meaning, reverses the stance, drops a condition),
  and a better rendering tested against the source. Drop judgements that cannot be anchored.
  "The register is appropriate" and "cohesion is good" are read and discarded.
- **State the damaged element in the label:** "major: the condition for eligibility is
  lost", not "mistranslation".
- **Separate what you are sure of from what you suspect,** and drop weak suspicions. Spurious
  findings are what make a reviewer useless. Put findings that rest on nuance, irony or
  affect as interpretations with your alternative, not as errors.
- **Mark an omission at the point in the target where the content belongs.**
- **Group findings by what they damage** (the information, the relationship with the reader,
  the structure) and say how badly each is affected. Do not let a tally of word-level errors
  stand in for a judgement of the whole, in either direction.
- **Record what was solved well** in the passages known to be hard. A report listing only
  faults misstates the quality.
- **For every place where the target says markedly more or less than the source, name the
  cause** (language requirement, genre convention, space, instruction) or mark it unexplained.
- **Corrective action is separate from correction.** After an instance is fixed: name the
  class of error, sweep the document for other instances, and say what change to the
  instructions or glossary would prevent it.
- **State the scope:** which passes, which parts, with or without the source, which classes
  of problem were not checked.
