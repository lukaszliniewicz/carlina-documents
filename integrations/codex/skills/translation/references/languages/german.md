# German notes

Writing in German, mostly from English, with a final section on translating out of German. Many of the conventions come from software and documentation localisation; the typographic ones hold for any text. A client's style guide overrides them.

## Punctuation and typography

- **Use the en dash, never the em dash.** Set parenthetical breaks with a spaced en dash,
  number and page ranges without spaces, and use it as a minus sign. Example: 12–18;
  – 2.375,99 EUR.
- **Set quotations with „…“.** Put the opening mark low and the closing mark high, never with
  straight or English curly quotes. Example: „Anführungszeichen“.
- **Put no colon after a heading.** German procedural headings take no final punctuation; in
  lists, captions and callouts use a period only where the item is a full sentence.
  Example: So installieren Sie das Programm ABC.
- **Set a comma before an introduced zu-infinitive.** It is mandatory after um, ohne, statt,
  außer and als, after a noun, and where es, dazu or daran points forward. Example: Er ging,
  ohne sich zu verabschieden.

## Numbers, dates and units

- **Format numbers for the German market.** German takes a comma decimal and a period
  thousands separator (1.250.254,99); Swiss and Liechtenstein German reverses this with an
  apostrophe (1'250'254.99). Example: 199,95 €.
- **Put the currency symbol after the amount.** Use it with digits only, so a spelled-out
  number takes the currency word instead. Example: 300 $, but 3 Millionen Dollar.

## Capitalisation, spelling and word formation

- **Join short compounds, hyphenate the rest.** Write compounds of up to three components as
  one word; hyphenate longer ones, mixed German-English ones and any whose letters collide
  (Back-End). Hyphenate every element of an English term (Social-Media-Konto) but leave a
  product name intact (Unreal Engine Blueprints-Verweise).
- **Prefer the joined spelling.** Write sodass, mithilfe, aufgrund, infrage, fertigstellen,
  and separate only in a real consecutive clause (er arbeitete so, dass er krank wurde).
- **Inflect loan-verb participles with German endings.** Write der gelikte Beitrag, eine
  gefakte Nachricht, not gelikede or gefakede; the English form survives only uninflected
  (gelikt, relaxt).

## Grammar carried over from the source

- **Recast a non-agentive English subject.** A thing that holds or allows something rarely
  stands as the German subject; use a prepositional phrase with a können passive.
  Example: In Fach 1 können bis zu 125 Blatt eingelegt werden.
- **Resolve German gender from the text, not the stereotype.** Fix it from earlier sentences
  and keep it consistent; where nothing marks gender, do not default to masculine, and move
  the information rather than inventing it.
- **Repeat a shared element in coordination.** German marks case and agreement on each
  element, so do not copy an English ellipsis of a verb or article. Example: Das Laufwerk
  %1 ist kein Diskettenlaufwerk oder ist einem Netzlaufwerk zugeordnet.

## Words and phrases

- **Check a cognate's target range, but do not ban it.** The danger runs from the target
  into the source: Holidays is Feiertage, not Urlaub. Desillusionierung, however, was the
  right word in an academic text.
- **Check whose perspective a formula serves.** German Macht nichts is self-oriented and
  dismissive where 'never mind' comforts the other person.
- **Prefer the everyday German word.** Write Handy, benötigen, teilweise, einen Anruf
  tätigen; and be specific: 'a phone' beside PCs is Smartphone, not Telefon.
- **From Spanish: spell out an elliptical referent.** Where the German word is ambiguous,
  name what the ellipsis points at: caída del Imperio becomes Zusammenbruch des
  Napoleonischen Reiches, not Untergang des Empire.

## Address, politeness and tone

- **Do not let a possessive or contact signal go impersonal.** 'Your dividend certificates'
  must not become die Aktienzertifikate, nor 'as you know' bekanntlich.
- **Choose Sie or du by audience.** Address adults with Sie instead of third-person Benutzer;
  address children, teenagers and an AI assistant in prompts with du. Example: Sie
  bestimmen, wann neue Updates installiert werden sollen.
- **Neutralise gendered nouns by a fixed ladder.** Prefer an existing neutral noun, then a
  plural, then a pluralised present participle, then a Person construction, then rephrasing.
  Never use the gender star or similar characters.
- **Stop neutralising where it damages the German.** Keep Benutzer, Kunde, Partner; do not
  build neutral forms into compounds, use -schaft for one person, or give a generic referent
  a gendered pronoun.
- **Keep promotional copy addressed to the reader.** A shift to nominal constructions,
  third-person reference and impersonal endings drains the persuasive force. Example:
  'Find out for yourself' must not become Wohl bekomm's.

## Sentences and text organisation

- **Prefer the verbal clause to the nominal one.** Write Wenn Sie das Dokument öffnen,
  können Sie eventuelle Co-Autoren sehen rather than Beim Öffnen des Dokuments werden
  Ihnen … angezeigt.
- **Balance explicitness by direction.** Into German, make connections explicit that English
  leaves to inference; out of German, drop links that have become redundant.
- **Do not import stance the source does not mark.** Extra connectives, direct address and
  modal particles (doch, ja, eben, schon) shift the interpersonal stance; the explicitness
  rule covers information links, not attitude.
- **Vary lexis into German, keep repetition into English.** German stigmatises repetition, so
  do not out-vary native norms.
- **Keep the reason attached to an instruction.** German procedural texts give reasons and
  define terms: do not delete them out of German, and do not add them into German unless the
  brief asks.
- **From German: restructure academic prose for English.** German runs non-linearly, with
  digressions and recapitulation after each return to the main line; give the English a
  single signposted line of argument.
- **Do not reproduce indirect directives in instructional text.** 'Could you' and 'I must ask
  you to' do not occur in manuals, where politeness would put the reader in an inferior role.

## Names, terms and abbreviations

- **Leave untranslated product names uninflected.** Use no article and avoid the genitive by
  writing von plus dative; German-language feature names behave as ordinary nouns.
  Example: Die Vorteile von Active Desktop; Öffnen Sie den Task-Manager.
- **Do not repeat an acronym's last word.** Write PIN, TCP/IP, HTML; give the German full form
  first on first mention and never invent a German abbreviation.
- **Keep French, Italian and Spanish honorifics that carry the setting.** Verify the form
  (Signor Rossi, not Signore Rossi) and keep the name order, so Son Heung-min becomes Herr Son.

## Software and technical texts

- **Use the infinitive for commands.** Write XYZ anzeigen, not Zeige XYZ, and avoid direct
  address; use Sie where address is needed and Du in social media. Keep umlauts and ß as real
  characters, never ae/oe/ue/ss.
- **Close a message with a period.** Add one where the English string has none, leave a
  one-word message bare (Druckerfehler), and replace exclamation marks: Zu viele Dateien.
- **Write so no article agrees with a placeholder.** Rephrase or use a fixed form: Möchten Sie
  "%s" (ungültig) ersetzen?, not den ungültigen %s.
- **Avoid separable verbs before a token.** Their prefix would land after an untouchable tag;
  choose Empfiehl or Erstelle, or restructure.
- **Let German usage select the preposition.** Write migrieren zu, kompatibel mit,
  aktualisieren auf, dem Konto hinzugefügt, im Internet, auf einer Website, and never strand a
  preposition.
- **Translate 'select' by what is selected.** Use auswählen by default, aktivieren for a check
  box, markieren for text or cells; 'click' is klicken auf with an object.
- **Start a negative statement with the object.** Write Die Datei kann nicht geöffnet werden,
  and compress 'an error occurred while trying to X' to Fehler beim X-en.

## Legal and official texts

- **From German: render dürfen as permission, not possibility.** Write 'is authorised to' or
  'authorised for use', and negated dürfen as 'must not'. Example: food additives authorised
  for use.

## Subtitles and dubbing

- **Do not translate film titles literally.** German release titles state the plot or the
  emotional premise, so flag the change as a titling decision; but disclose nothing the source
  title withholds. Example: 'Where are the Children?' became Grenzenloses Leid einer Mutter.
- **Keep ist and nicht in colloquial transcription.** They are pronounced is and nich; write
  them in full but reproduce other reductions as heard (hab, ne).
- **Do not shorten a line by shifting perfect to preterite.** Where the action is ongoing or
  has present effect, only the perfect is possible: keep Ich habe gelogen.

## Literary and religious texts

- **Build a du/Sie relationship map first.** One decision per pair of speakers, revised when
  the relationship or the directorial reading of intimacy and power changes; English leaves
  the choice invisible.
- **Render marked dialect by its social function.** Choose the target variety for comparable
  social geography, not proximity: Hiberno-English matched with Bavarian for shared provincial
  distinctiveness.
- **Expect German children's books to be more explicit and pedagogic.** German versions add
  intent, sentimentalise and typecast adults. Example: 'It was cold in the car' became Es war
  Schlafenszeit. Do not mirror that filter into English.
- **Do not translate the animal in an insult or endearment.** Choose the target's conventional
  expression with the same force: la vache! becomes Schwein!, 'duck' becomes Mäuschen.
- **Look a compound up as a whole term.** Parsing Rosenkranz as rose plus garland produced
  'rose garland' for a rosary.
- **Keep a calque's strangeness.** Where the source made a name read as foreign, translate it
  literally: Guerra de Brigantes becomes Brigantenkrieg, not Seeräuberkrieg.

## Translating out of German

- **Do not render bzw as 'respectively'.** It usually means 'or', in contracts 'and, if
  applicable'; and do not reduce grundsätzlich to 'in principle' where it emphasises.
- **Decide each particle's weight before dropping it.** Render the weight-bearing auch as
  'too'; give the nuance case a null equivalent rather than losing all of them.
- **Search the English for German words left standing.** Rare, archaic and compound words
  survive: 'From this dream, the Heimgesuchte woke up'.
