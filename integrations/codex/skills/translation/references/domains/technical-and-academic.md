# Technical, software and academic texts

Manuals and instructions, product and interface text, and scientific or scholarly writing.
Three different jobs with one thing in common: the reader will act on, click on or cite what
you write, so precision outranks elegance and repetition is a virtue.

## Contents

1. Terms in technical text
2. Instructions and documentation
3. Software strings and localisation
4. Scientific and scholarly writing

## 1. Terms in technical text

General rules are in `names-terms-quotations.md`. Specific to technical work:

- **Never vary a term for style.** One concept, one target term, every time, however
  repetitive it looks. A keyword the source repeats is repeated; no synonyms, no pronoun in
  its place where that could be read as something else.
- **Check terms against the document's own system of categories.** Where the text uses two
  terms to draw a distinction, they need two renderings even if general usage merges them.
  Then check every pair of distinct source concepts that ended up with the same target term.
- **Synonym or not?** Several designations for one concept are true synonyms (swappable),
  variants tied to a register or situation (label them), or look-alikes naming neighbouring
  concepts. Where dictionaries disagree, check whether a standards body has ruled.
- **Do not move up or down the hierarchy.** A generic term for a specific one, or the
  reverse, changes the claim.
- **Compounds are not translated part by part.** Verify the established equivalent in
  target-language specialist sources.
- **Get the company the term keeps.** After finding the term, establish which verbs and
  adjectives go with it in the target register. Wrong collocations mark the text as
  non-specialist even when every term is right.
- **An odd-looking word order or collocation may be the fixed term.** Check before
  normalising it.
- **No established equivalent after a real search:** do not slip in a vague general word or
  an unmarked calque. Reproduce the source term marked as foreign, paraphrase, or coin a term
  and say that you did.
- **Not every technical-sounding word is a term.** Terms proposed by a model from raw text
  are candidates for general vocabulary until shown to designate a concept of the field.
  Read the contexts before entering an extracted string in a glossary.
- **One glossary entry per concept,** not per source word. A source word with several senses
  in the text gets several entries, each with its own target term.
- **Mandated terms the readers do not yet know:** deliver with the mandated terms and a
  short bridging list mapping the readers' familiar words to them.

## 2. Instructions and documentation

- **Use the target's device for instructing.** Languages signal a step differently
  (imperative, infinitive, impersonal or passive present). Do not carry the source's device
  across, and do not mix devices.
- **Where the source breaks its own genre** (a step phrased as a statement, a heading phrased
  as a question), write the form the text type calls for.
- **Safety information must not survive only as an implication.** Where the source relies on
  shared understanding to make a danger clear, state it, at the cost of extra words.
- **Carry out the steps as written.** Reading a procedure as its user, in order, with the
  diagrams, exposes mismatches between text and figures and the places where the user would
  be stuck.
- **Two readings of an instruction or specification** ("the file structure", "no smoking
  seats are available") are never settled silently. Ask, or choose the executable reading
  and flag it.
- **Numbers keep their precision.** Never add or drop a decimal place or a trailing zero.
  A converted measurement is rounded to the precision the original justifies.
- **A figure that contradicts common knowledge** (an impossible distance or capacity) is
  corrected and reported, or flagged, never quietly reproduced.
- **A source so defective that a faithful rendering would be unusable** (garbled or badly
  translated instructions): do not quietly translate the defects and do not quietly rewrite.
  Ask what is wanted.
- **In purely informative documentation stay in the background** and keep close to the
  source's structure. Where a passage is someone's own statement, how it is said matters
  again.
- **For expert readers, delete explanations you added for a general reader.**
  Over-explaining tells specialists the text is not for them.

## 3. Software strings and localisation

- **An isolated string has no part of speech.** "Start", "Open", "Archive", "Empty" may be a
  verb, a noun, an adjective, a command or a state. Do not guess. Resolve it from what the
  control acts on, a screenshot, the string identifier or the developer's note, and translate
  the whole proposition the label stands for. If none of that is available, flag it.
- **Source strings may be defective.** Developers are often not native writers. When a label
  is ambiguous or does not describe what the control does, translate the function and report
  the string.
- **References to the interface are character-for-character identical to the interface.**
  When running text mentions a button, menu item or heading, look up how that label was
  translated. A plausible fresh translation sends the user looking for something that does
  not exist.
- **Placeholders, variables and control codes** are reproduced exactly, without added spaces
  and without reordering repeated positional specifiers, unless the format provides for
  reordering. Verify them mechanically before any judgement of the wording.
- **Concatenation and plural assumptions.** A string assembled from fragments, or code that
  forms plurals the English way, cannot be translated correctly into many languages. Do not
  force it. Flag it as needing a change in the code or resource format, and until that
  happens deliver a wording that is right for every value (typically a label, a colon and
  the number). A form that is right only for some numbers never goes in the deliverable,
  not even with the correct alternatives listed in a note.
- **Space.** Assume the target has roughly the space the source occupied plus a little, or
  the stated limit. Shorten by rephrasing. Never end with an ellipsis to hide overflow and
  never cut a sentence short: the interface truncates on its own, and half a restructured
  sentence may mean something else.
- **Shortcut keys and commands are not localised by default.** Change one only where the
  target keyboard cannot produce it or the platform's own convention differs.
- **Do-not-translate marks and notes attached to a segment are instructions.** Read them.
- **Escaping and encoding** follow the file format (entities in markup-based resource
  files, the encoding the format requires). A raw ampersand or quotation mark can break the
  build.
- **Units.** Localisation normally includes converting to the audience's system. Where the
  number is supplied at run time and only the unit is fixed text, you cannot convert: flag
  it.
- **A lead-in and its list items stay grammatically parallel,** punctuated by item type
  according to the target's rules.
- **Web content has peripheral text:** page title, descriptions and keywords, menus, link
  text, alternative text for images, form labels, error messages. They are part of the job.
- **A coined spelling or slogan that runs through text, images and links** is one string and
  one decision.
- **Roles and titles the target would force into one gender:** prefer a single form that
  covers everyone where the language has one.
- **An established localisation of a well-known title** may have famous quirks its audience
  expects. Check before normalising them.

## 4. Scientific and scholarly writing

- **Hedging is the claim.** "May", "probably", "often", "in some cases", "we suggest": same
  strength in the target, with whatever device is natural there (a verb can become an
  adverb or an impersonal form). Never upgrade a hedged proposal to a finding.
- **Evaluative force at matching strength.** Keep the writer's intensifiers and stark
  wording; scholarly register in the target is not a licence to tone them down.
- **Test every connective against the relation in the source.** "Therefore" must be a
  consequence, not a sequence or an addition. Concessive and contrastive words are a budget:
  too many make an argument unreadable, so do not add them.
- **Do not make a difficult original easy.** Where the source is dense or demanding, the
  translation is correspondingly demanding. Fluency is a virtue only where the source has it.
- **Do not add what the reader is left to infer,** and never a restricting specification the
  source does not assert, even where the argument implies it.
- **Keep the type of impersonal device,** not just the proposition. Passive, nominalisation
  and agentless construction each hide the agent in a different way.
- **Dense noun groups:** unpack them into clauses to recover who does what, check that
  reading against the source, then decide how much to re-pack for the target.
- **Translate by rhetorical move.** In a research article identify what each stretch does
  (states the field, finds the gap, announces the study, reports, interprets) and use the
  target's conventional wording for that move. Keep the large-scale organisation; adapt
  inside paragraphs.
- **A term the text creates by contrast.** When two near-synonyms are set against each other,
  the target must show a contrast even if both would normally get the same word.
- **A text about language, translation or its own rhetoric** must be translated consistently
  with what it argues, and its examples must still illustrate the point.
- **An unusual collocation that the author marks** (quotation marks, a comment) keeps its
  marking.
- **Bibliographic references:** cite the edition that exists in the target language where
  there is one, verified, and keep the original reference where the reader may need it.
- **Reported views** are marked by the target's own system (mood, tense shift, evidential
  wording), so that the author's claims and the claims being reported stay distinct.
