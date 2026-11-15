# Polish notes

Writing in Polish, mostly from English, with two points on translating out of Polish. Many of
the conventions come from software and documentation localisation; the typographic ones hold
for any text. A client's style guide overrides them.

## Punctuation and typography

- **Quote in „ ” and nest inner quotes in » «.** Leave no spaces inside, put the full stop
  outside the closing mark even when the quoted material is a sentence, and open the quote
  once across several subtitles. Example: „Powiedział: »Nie teraz«”.
- **Choose the dash by function.** Unspaced hyphen in compounds and between a numeral and a
  word, spaced en dash for a minus sign or emphasis, unspaced en dash for ranges. Example:
  'Opis ten podano na ss. 236–238', '30-procentowy', 'ZAROBKI – WYDATKI = 2 000 000'.
- **Punctuate bulleted lists by grammar.** Items that continue the introductory sentence
  start lowercase and end with commas, only the last with a full stop; use semicolons if any
  item contains a comma; complete-sentence items take a capital and a full stop.
- **Keep the two ellipsis functions apart.** A bare '...' (no spaces inside, no space before,
  space after) marks a pause; an ellipsis for omitted quoted material goes in brackets, e.g.
  (...). Never use four dots. Example: 'Nie rób tego... To może być niebezpieczne.'
- **Put the full stop outside a parenthesis.** This holds even when the parenthesis is a
  whole sentence; only the bracketed material is bracketed. Example: 'W przypadku utraty
  hasła nie będzie można odzyskać danych. (Pamiętaj, że w hasłach jest rozróżniana wielkość
  liter).'

## Numbers, dates and units

- **Set a hard non-breaking space (U+00A0).** Use it wherever a break would be wrong: as the
  thousands separator (20 000, not 20,000 or 20.000), between a number and its unit (60 ml)
  and after one-letter words such as w, z, i. Example: w witrynie; 100 km/h.

## Capitalisation, spelling and word formation

- **Reduce English title case to Polish sentence case.** Capitalise only the first word of a
  title or of the name of a program, dialog box or check box, and write days, months, seasons
  and language names in lowercase. Example: 'Try Again' becomes 'Spróbuj ponownie'.
- **Continue in lowercase after a colon.** This covers a colon introducing a list, an
  explanation or a procedure; Polish capitalises only before a quoted sentence or similar.
  Example: 'W programie można zmienić następujące elementy: motyw, czcionkę, rozmiar
  czcionki, tło i obramowanie okna.'
- **Form abbreviations so they end in a consonant.** Use the hard consonant where the full
  form writes a soft one with 'i', and add a full stop unless the last letter also ends the
  full word. Example: zobacz → zob.; według → wg; między innymi → m.in.; godzina → godz.
  (not godź.).

## Grammar carried over from the source

- **Delete possessive pronouns and adjectives Polish does not need.** Drop your, my, their
  before body parts, files or settings, and keep them only where ownership is ambiguous or
  contrastive. Example: 'We recommend that you back up your files' becomes 'Zalecamy
  regularne tworzenie kopii zapasowych plików'.
- **Avoid endings that expose the reader's gender.** Use the present tense, an impersonal or
  passive construction, or a rephrasing without a gendered subject, and never third-person
  'Użytkownik'. Example: 'Zapomniałeś hasła?' becomes 'Nie pamiętasz hasła?'; 'który mógł
  być uprzednio zainstalowany', not 'który mogłeś uprzednio zainstalować'.
- **Put the noun first in a fixed two-word technical term.** The adjective follows, although
  ordinary Polish adjectives precede the noun. Example: hard drive → dysk twardy; personal
  computer → komputer osobisty; nonvolatile memory → pamięć trwała.

## Words and phrases

- **Replace nominalised, bureaucratic Polish with the plain verb or preposition.** Write
  można, not istnieje możliwość; przez or za pomocą, not za pośrednictwem; jednak or ale,
  not jednakże. These phrases turn concise English into Polish officialese.
- **Use person-first disability wording.** Write osoba z niepełnosprawnością, osoba bez
  niepełnosprawności, and avoid pity-marked terms (dotknięty chorobą, cierpiący na, kaleka)
  and 'normalna osoba'. Mention a disability only where needed. Example: 'handicapped'
  becomes 'osoba z niepełnosprawnością'.

## Address, politeness and tone

- **Choose the person from the text's function.** Use second singular for instructions to the
  user, first singular when the user speaks ('Pamiętaj moje hasło'), first plural when the
  product speaks ('Zalecamy...'), never third-person 'użytkownik'. Omit the pronoun where
  the verb carries the person; where a pronoun addressing the reader is written, capitalise
  it (Ty, Twój, Twojego).
- **Write 'Pan(i)' or 'Państwo' where formal register needs it.** Give both past-tense endings
  (kupił(a)) rather than silently choosing masculine. Example: 'Gdzie Pan(i) kupił(a) tę
  książkę?'; informal 'Gdzie kupiłeś(-łaś) tę książkę?'.
- **Check the force of a request phrased as a question.** English uses the interrogative form
  for directives of every temper, including angry and sarcastic ones. In Polish an
  interrogative directive reads as cautious and very polite, so the same shape can reverse
  the tone.
- **Into English, hedge Polish advice; into Polish, add no force.** Render Polish advice by
  imperative as indirect English ('If I were you I would ...', 'Why don't you ...'), and keep
  English indirect advice indirect in Polish, without adding imperative force: politeness is
  tied to avoiding the imperative in English but not in Polish.

## Names, terms and abbreviations

- **Do not attach Polish case endings to English product names.** Insert a category noun in
  the required case before the name (system Windows, funkcja BitLocker) and leave the name
  uninflected. Never turn a trademark into a verb. Wrong: 'Wszystkie polecenia Worda',
  'Zamknij Windows'.
- **Expand a retained English acronym at first use.** Give the full English term and, where
  one exists, a Polish translation in parentheses; use established Polish acronyms (DW, UDW,
  NIP) only where they exist. Example: FDA (Food and Drug Administration, Agencja ds.
  Żywności i Leków); BCC → UDW.
- **Inflect acronyms only when they end in a consonant sound.** Otherwise leave them
  uninflected or add the lowercase ending after a hyphen (SMS-a); never inflect one
  pronounced with a final vowel (GUI, SAA). Example: 'po otrzymaniu wiadomości SMS'.
- **Leave trademarked product names in English.** Translate the prepositions linking them
  and localise feature names unless the feature is itself trademarked; fix each rendering
  once and apply it throughout.

## Software and technical texts

- **Check what fills a placeholder before placing it.** Establish whether it is %s (a string
  or name) or %d/%u (a number) and arrange the words around it so the sentence stays
  grammatical in the right case. Do not translate tag contents unless a comment marks them
  display text. Example: %u groups → 'Grupy: %u'.
- **Render English error formulas in the present tense.** Even when the source is past:
  Failed to, Cannot, Could not, Unable to become 'Nie można' plus an infinitive; Cannot find,
  Unable to locate become 'Nie można odnaleźć'. End with a full stop and use 'niestety', not
  'przepraszamy'.
- **Keep English key names untranslated.** Introduce them with 'klawisz' in the required case
  (naciśnij klawisz CTRL), except spacja, klawisz systemu Windows and the arrow keys
  (strzałka w górę), lowercase in generic references. The keyboard legend stays English.
- **Keep every meaning-bearing element when condensing a confirmation.** 'Are you sure you
  want...' becomes 'Czy na pewno chcesz...', not 'Czy chcesz...'. Treat a source question as
  a question in Polish, and phrase instructions in the imperative.

## Subtitles and dubbing

- **Set the dialogue dash with a space after it.** It stands on its own before the spoken line
  ('- I co?'); English and German use a hyphen with no following space. Example: '- Poczekaj.
  Daj mi chwilę.'
- **Choose the SDH base text from the kind of dubbing.** With lip-sync dubbing, build it from
  the dubbed audio and follow its wording; with voice-over (lector) dubbing, build it from
  the existing non-SDH subtitles, keep that translation, add speaker IDs and sound labels,
  and fix errors. The SDH must match what the audience hears.

## Translating out of Polish

- **Distrust machine output for older or specialised Polish.** Verify every segment instead
  of spot-checking; fluent output can pass isolated checks, so the failure stays invisible.
- **Re-read names and cultural references in context first.** Check every proper name,
  adjective derived from a name, and local cultural reference before accepting machine or LLM
  output. Engines resolve them by association and produce fluent nonsense: a surname from an
  adjective, an invented people from a regional culture. Example: 'kultura kociewska'
  rendered as 'Kittian culture'.
