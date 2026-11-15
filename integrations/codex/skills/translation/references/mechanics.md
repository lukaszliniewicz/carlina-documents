# Mechanics, layout and genre conventions

Numbers, punctuation, strings that must survive untouched, structure on the page, and what a
genre requires in the target culture. Most of this is right-or-wrong rather than better-or-
worse, and most of it is lost by copying the source's surface. Read for anything with figures,
lists, tables, forms, markup or placeholders, and whenever the text belongs to a recognisable
genre (news, instructions, advertising, correspondence, labels).

Conventions of individual languages (quotation marks, dashes, separators, capitalisation) are
in `languages/`.

## Contents

1. Numbers, dates and units
2. Punctuation
3. Strings that must survive
4. Structure and layout
5. Genre conventions
6. The surface pass

## 1. Numbers, dates and units

Separate notation from value.

- **Notation follows the target locale and is not optional:** decimal and thousands
  separators, digit grouping, date order, clock notation, position of the currency symbol,
  spacing between number and unit, address and telephone formats. These are facts about the
  locale. Check them in a separate pass over every data element after the content pass.
- **Recompute, do not reformat.** 09.02.09 is a different day depending on where it was
  written. 4:40 PM is 16:40. Work out the value, then write it in the target notation, and
  re-read every converted value against the source.
- **Values are content.** Converting a currency or a unit system changes what the text says.
  Do it only when the brief asks; otherwise propose it. When you do convert, change numeral
  and unit together and check the arithmetic: a converted figure with the old unit and a new
  unit with the old figure each look plausible alone.
- **Keep the source's level of precision.** An exact figure stays exact. A round or "about"
  figure converts to a round figure with approximate wording, never to three decimal places.
- **Percentages, ratios and rates of change are recomputed, not mirrored.** A fall of 50% is
  not undone by a rise of 50%, percentage points are not percent, and a rate does not invert
  with its direction.
- **Scale words differ between communities.** "Billion" and its look-alikes can mean a
  thousand million or a million million. Settle which the author meant and make it
  unambiguous at first occurrence.
- **Counting spans.** Some communities count inclusively (the starting day is day one).
  Adjust intervals expressed in days, and "in a week", "in fifteen days" formulas, to how the
  target readers count.
- **Rates need their "per".** Where the source sets quantity and price side by side, a bare
  rendering can be read as a total.
- **Calendar and era.** Choose the calendar system by what the text is about and who reads
  it, and convert consistently.

## 2. Punctuation

- **Re-derive punctuation from the target's rules.** Quotation-mark style and nesting, the
  shape of the question mark, apostrophes, spacing before punctuation, where the full stop
  sits relative to quotation marks and brackets, dashes, capitalisation after a colon, list
  punctuation, title capitalisation. Check these hardest in a draft produced by overwriting
  the source, where the source's marks are already in place.
- **Comma placement follows different principles** in different languages (rhythm and sense
  in some, syntax in others). Do not copy it, and do not delete a stylistic mark without
  replacing its effect.
- **Decide what each pair of quotation marks is doing** before rendering it: quoting, irony,
  disagreement, emphasis, a word not yet accepted, a title. Render that function. Where the
  target's marks do not carry it, say it in words, switch to italics, or drop the marks.
- **Where target punctuation does little work,** do not rely on marks to separate the
  writer's voice from a quoted one or a report from a commitment. Carry the distinction in
  wording. Test: delete the commas, brackets, dashes and quotation marks and read the passage.
  If it falls apart, it was held together by imported punctuation.
- **Do not complete what the source left unfinished.** Headings, list items, subtitles,
  fragments and interrupted sentences take no added full stop or closing quotation mark.
- **Brackets and quotation marks must pair** in number and kind. Check every sentence.
- **After moving an expression, re-check punctuation at both ends:** the place it left may
  keep a stray comma and the place it went may need one.
- **For a long sentence, hear it.** If the intended reading needs a pause or stress that the
  written text does not mark, punctuate or recast so that the words cannot attach wrongly.
- **Nested speech.** Verify every "I", "you" and "he" against the speaker of its layer.
  Alternate mark styles, or turn an outer layer into indirect speech.
- **Never impose a punctuation or usage preference by replace-all.** Inspect each occurrence;
  an added or removed mark can change the meaning.

## 3. Strings that must survive

- **Placeholders, variables, tags, codes, identifiers, URLs, e-mail addresses and file names
  are reproduced exactly.** Never drop one, never replace one with a guessed value, never
  translate inside one. Count them in source and target after drafting.
- **Markers inserted to enforce boundaries are not reliably respected.** Segment tags and
  sentence markers get dropped and the sentences merged. Verify that every marker survived
  before treating the output as aligned.
- **Check that a block boundary is a real sentence end.** An abbreviation with a full stop, a
  decimal or a tag can cut a sentence in two. Translate the joined sentence as one unit, not
  each fragment separately.
- **Text damaged by extraction** (hard line breaks from a PDF, missing capitals and final
  punctuation) is normalised before translating or handing to another system. Each broken
  line otherwise becomes an isolated fragment.
- **After any machine pass** confirm that numerals are still numerals and not spelled out,
  list markers (a, b, c, bullets) survive, and letter case is preserved.
- **Correctly spelled wrong words** ("form" for "from", "adopting" for "adapting") pass every
  spelling check. They need their own read.

## 4. Structure and layout

- **Positional references are made stable.** "The table below", "overleaf", "see page 12",
  "the chart opposite" stop being true when layout and length change. Replace them with
  table, figure and section identifiers, and flag any you cannot stabilise.
- **Do not reorder, alphabetise or renumber anything until the meaning comparison is
  finished.** A list that was alphabetical in the source is re-sorted last, and only if its
  order was alphabetical on purpose.
- **Forms are translated as forms:** same fields, same order, same numbering, blanks left
  blank, instructions for ticking and deleting kept, abbreviations in labels expanded. Keep
  the original name of the form or document in brackets where the reader must match the
  translation to the original.
- **A hedge travels with its claim.** When a hedged statement becomes a bullet point or a
  heading, the hedging frame ("we judge that", "reports suggest") goes into the item. It must
  not stay behind in the lead-in or vanish.
- **Headings are checked against their siblings.** A heading has no context of its own. A
  literal rendering broader than the source word may no longer tell the reader which of the
  parallel sections this is. Specify it, within the pattern and length of the others.
- **Length is a budget.** Where the target must fit the source's space and also carry added
  explanation, the room has to come from somewhere: cut redundancy, even if the target genre
  would tolerate a fuller style. Do not get it by dropping articles and prepositions or
  inventing abbreviations.
- **Non-verbal elements are part of the text.** Images, colours, icons, gestures and layout
  are checked for whether they work for the new readers. Flag those that do not. Before an
  image is mirrored to fit a layout, check asymmetric details: which hand, which way a
  garment wraps, the direction of a gesture or of reading.
- **Text used as decoration** (a foreign language chosen for how it looks) is not translated
  into meaningful words.
- **Credits, imprints and "text by" lines** are updated, not reproduced: the wording of the
  translated edition has a different author.
- **Meaning-bearing emphasis** (bold, italics, underlining) is carried over to the words that
  now bear that meaning, which may sit elsewhere in the sentence.
- **After translating visually structured content,** look at the result as someone who cannot
  read the source: do sequence, numbering, arrows and labels still make sense?

## 5. Genre conventions

- **Does the genre exist for the target readers?** If the source's genre has no counterpart
  there, a translation that passes for an original is not possible. Say so and deliver a
  declared version or an openly foreign text; do not imitate a genre the readers do not have.
- **Conventional or meaningful?** Where the source simply follows its genre, its phrasing
  carries no special effect and is replaced by the target genre's own conventions. Where it
  departs from its genre, the departure is the meaning and is carried over. Establish which
  before deciding how close to stay.
- **Declare whose conventions you follow** when the two cultures' versions of a genre differ
  sharply, and follow them throughout. The finished text is then checked against the target
  culture's idea of that genre as well as against the source: a manual that does not
  instruct, or a children's book no child can follow, has failed however accurate its
  sentences.
- **Take conventions from the right sub-genre.** A book blurb is partly advertising, an
  abstract is not the article. Establish what the particular item is for before choosing
  forms of address and sentence patterns.
- **Small set-form genres** (mottos, slogans, oaths, proverbs, courtroom and ceremonial
  formulas, closings and greetings in letters) are rendered with the target's own set form,
  even where its wording says something quite different.
- **Instructions and procedures.**
  - Use the verb form the target convention uses for steps (imperative, infinitive,
    impersonal), whatever the source happens to use.
  - Give each slot the form its function needs: titles, conditions and steps are built
    differently, and one sentence pattern carried through all of them is wrong.
  - Check that the conditions are stated before the steps and can be rendered
    unambiguously: who qualifies, what triggers the procedure, what limits apply. Flag
    conditions hidden after the steps or expressed only as a negative heading.
- **News.**
  - Identify the voice (report, correspondent's analysis, commentary) from how much explicit
    judgement the source contains, and match that density. Commentary translated as report
    attributes an opinion to the publication; the reverse invents one.
  - A verbless or imperative headline can assert an obligation or a pending action where the
    article reports something finished. Check what the target form implies.
  - Time words ("yesterday", "this week") are anchored to the writer's moment in some
    traditions and the reader's in others.
  - When a speech or press release is re-reported, the reporting genre's detachment replaces
    the speaker's emphasis.
  - If shortening is asked for, hard news is cut from the end.
  - Rewriting for a media client (new headline and lead, reordering, added background) is a
    different product from translation and needs to be the brief.
- **Promotional and tourist texts.**
  - Check how the target genre conveys enthusiasm (feeling, identity, superlatives, direct
    invitation) before deciding how much to keep. Copying the source's manner can read as
    flat or as overblown.
  - Source imagery and sentence architecture that survive intact because they can be
    rendered fluently are usually wrong for the genre. Rebuild the passage around how the
    target reader is addressed and moved.
  - Brand and product names are repeated, not replaced by pronouns. The repetition is doing
    work.
- **Labels and packaging** have required elements in each market (directions, warnings,
  quantities in the expected units, responsible company). Translate the wording and flag
  required elements that are missing; keep internationally standardised names standard.
- **Illustrated books and comics.** The images are part of the text. Do not add wording that
  contradicts them or settles what they leave open, and do not mirror artwork to change the
  reading direction.
- **A change of medium** (leaflet to website, guidebook to app) changes what content is
  selected, not only the style.

## 6. The surface pass

Run it last, on the text in its final produced form, after content revision has stopped. It
covers what only exists there: placeholders and tags, line breaks and hyphenation, truncation
in fixed-width fields, numbering, cross-references, punctuation that depends on layout, and
the data elements in section 1. Surface fixes made earlier get undone by later edits.
Punctuation and spacing slips weigh far less than other errors, unless one makes a sentence
ungrammatical or changes its meaning; then it is a full error.
