# Dutch notes

Writing in Dutch, mainly from English. Many of the conventions come from software and
documentation localisation and from subtitling; the typographic ones hold for any text. A
client's style guide overrides them.

## Punctuation and typography
- **Cut every en and em dash.** Replace an English dash, including a dash pair around an aside,
  with a comma or with brackets. Example: 'You choose the picture—and the gestures you use with
  it—to create a password' becomes 'U kiest de afbeelding (en de gebaren die u hierbij wilt
  gebruiken) om een wachtwoord te maken'.
- **Use curly quotation marks and the curly apostrophe.** Write ‘ ’ “ ” and ’, never the grave
  accent (U+0060) or the acute accent (U+00B4) as a quote or an apostrophe: they have other
  purposes, different shapes, and cause extra spacing when rendered.
- **Quote chapter, section and appendix names with single quotation marks.** Do not copy the
  source's double quotes, and leave file names and variables unquoted even where the English
  quotes them. Example: 'Wilt u de wijzigingen in Verkoop.xls opslaan?'

## Numbers, dates and units
- **Swap the separators.** Use the period for thousands and the comma for decimals, no
  separator in years, postal codes or phone numbers, and the 24-hour clock with 'uur' after it.
  Example: '1.000.000'; '3,5 inch'; 'van 16:00 tot 18:30 uur'.
- **Join numerals and acronyms to nouns with a hyphen.** Write 'HDMI-poort',
  '5GHz-processor', '230V-aansluiting', '35mm-dia', but keep 'vijfsterrenhotel' and
  'vijftigeurobiljet' solid, and use a space before a word group: '64 bitsprocessor',
  '50 eurobiljet', '1 aprilgrap'.

## Capitalisation, spelling and word formation
- **Keep the trema on capitals and lowercase months and days.** It marks a syllable break, so
  it survives all-caps words and headings. Example: 'TWEE KOPIEËN, GEÏNTEGREERD,
  GEÏNITIALISEERD, GEÜPLOAD, BELGIË'; 'januari, februari'; 'maandag, dinsdag'.
- **Do not carry English title case over.** Capitalise only the first word and names; only
  product names keep their source capitalisation: 'Download de mobiele Teams-app.', not
  'Download de Mobiele Teams-App.'
- **Write 'non-X' as 'niet-X' with a hyphen.** Include the hyphen even before an untranslated
  name: 'niet-aaneengesloten', 'niet-Windows-toepassing', 'niet-Microsoft'.
- **Insert a hyphen where a compound boundary clashes two vowels.** This covers a-a, a-e, a-i,
  a-u, e-e, e-i, e-u, i-e, o-e, o-i, o-o, o-u, u-i and u-u: 'gala-avond', 'vanille-ijs',
  'bureau-inhoud', 'college-uitstap'. Other pairs stay solid: 'cameraopstelling',
  'koffieautomaat', 'maximumprijs'; 'gummi-jas' but 'beijveren'.
- **Let pronunciation decide how an acronym compound is written.** Word-like lowercase acronyms
  go solid (aidsvirus, pincode, latrelatie, vipbehandeling); letter-by-letter or capitalised
  ones keep a hyphen or apostrophe (btw-tarief, pc-gebruiker, tv-kijker, API-sleutel,
  RAM-geheugen).

## Grammar carried over from the source
- **Use the Dutch present tense where English uses 'will'.** Reach for 'zal/zullen' plus
  infinitive only when the event genuinely lies in the future. Example: 'Nadat u het
  hulpprogramma hebt geïnstalleerd, wordt het pictogram op het bureaublad weergegeven.'
- **Put the finite verb first in a subordinate clause.** Write 'Nadat u de software hebt
  geïnstalleerd, dient u deze te configureren.', not 'Nadat u de software geïnstalleerd hebt'.
  A participle describing a state goes first: 'dat de Help-index uitgebreid was'.
- **Drop English 'your' instead of writing 'uw'.** Keep the possessive only where Dutch demands
  it, and drop it in text about body parts, settings, devices and actions; 'uw computer' is the
  pattern to avoid.
- **Put no article before an untranslated product or feature name.** Write 'Toolbar nu
  installeren', not 'De Toolbar nu installeren', but insert the article when an adjective
  precedes the name ('De verbeterde Microsoft Teams'). Translated feature names take a capital
  and no article: 'Ga naar Bureau-accessoires en start Rekenmachine.'
- **Restructure a generic reference to a person of unknown gender.** Use a plural, a role noun
  (lezer, klant) or an article, not a possessive; failing that, deze. Never write hij/zij or
  hem/haar, and never make singular they plural zij. Example: 'Als de gebruiker de juiste
  rechten bezit, kan deze de wachtwoorden van andere gebruikers instellen.'

## Words and phrases
- **Render sequencing 'then' as 'vervolgens', not 'dan'.** Leave 'als' unpaired: avoid the
  'als … dan …' correlative. Example: 'Voer Setup vervolgens opnieuw uit.', not
  'Voer Setup dan opnieuw uit.'

## Address, politeness and tone
- **Drop 'please' and 'sorry' in functional text.** Do not write 'alstublieft', 'a.u.b.' or
  'Sorry': state the action directly, and use 'Een ogenblik geduld.' as the waiting formula.
  Example: 'Sluit Setup af en maak ruimte vrij op de schijf. Voer Setup vervolgens
  opnieuw uit.'

## Sentences and text organisation
- **Expect the Dutch to run twenty to thirty percent longer.** Do not compensate by dropping
  articles and prepositions or by imitating terse source style; shorten only against a known
  space limit.
  Example: 'Er is een probleem opgetreden bij het converteren van een vergelijking.'
- **Open an instruction with the action.** Give the imperative first and move the condition to
  the end. Example: 'Klik op de knop Volgende als u het product hebt geselecteerd.', not
  'Wanneer u het product geselecteerd hebt, klikt u op de knop Volgende.'

## Names, terms and abbreviations
- **Expand an acronym at every occurrence in fragments.** Software strings and
  interface text have no first appearance, so write 'CRC (Cyclic Redundancy Check)' each time
  in a message, and in running text follow it with 'CRC-foutcontrole'.

## Software and technical texts
- **Do not make a program name an agent.** Do not make it the subject of an action verb:
  recast so the affected object is the subject, use a passive, or drop the name. Example: 'De
  gegevens kunnen niet worden weergegeven in Microsoft Excel.', not 'Microsoft Excel kan de
  gegevens niet weergeven.'
- **Put no inflected article or agreeing adjective before a placeholder.** The gender of the
  substituted noun is unknown, so reword and drop the article:
  'Geselecteerde %s kan niet worden gelezen.' Check the concrete wording, since some nouns may
  still need a different form.
- **Add no sentence-final period when something will be appended.** A Dutch fragment normally
  closes with a period, but before an appended variable it would fall mid-sentence.
- **Prefer a compound to a repeated prepositional phrase.** Write 'De bestandsnaam is verkeerd
  gespeld of de bestandsextensie ontbreekt.', not 'De naam van het bestand is verkeerd gespeld
  of de extensie van het bestand ontbreekt.' Keep the phrase where a compound would be opaque.
- **Do not carry an English compound adjective over.** Unpack it instead of writing a
  hyphenated compound: 'user-initiated' becomes 'geïnitieerd door de gebruiker', 'cloud-based'
  becomes 'op de cloud gebaseerde', 'copy-protected disk' becomes 'tegen kopiëren beveiligde
  schijf'.
- **Write status and progress messages as an infinitive phrase.** End it with suspension
  points, not 'Bezig met het …' or the passive: write 'Tijdelijke bestanden verwijderen...'.
  Where the English adds 'please wait', give the substance first and then the waiting formula.
- **Write short notices as full clauses.** Include article, finite verb and existential 'er is'
  or 'er zijn': 'Er is geen netwerkadapter gevonden.', not 'Geen netwerkadapter gevonden.'

## Legal and official texts
- **From French: keep a cassation judgment to one sentence.** Introduce each ground with its
  fixed connective: 'Overwegende dat ...' for 'Attendu que ...'. Make word order natural inside
  the sentence rather than splitting it into paragraphs.
- **From French: do not reproduce participial post-modifiers as Dutch participles.** Dutch
  uses the construction far less, and the result reads as French. Convert to a relative
  clause or a lexicalised term.

## Subtitles and dubbing
- **Mark a split sentence with an ellipsis on both sides.** Close the first subtitle and open
  the next with one, unlike English and German practice: 'In Nederland gebruiken we puntjes …'
  / '… als de ondertitel wordt gesplitst.'
- **Put the hyphen before the second speaker's line only.** Never before the first, and never
  delete the first speaker's short line ('-Ja.' / '-Kom je?'): re-time the event to the second
  speaker's audio and translate both lines.
- **Leave filler and short standalone utterances out of single-speaker events.** Backchannels
  such as huh, yeah and well can go, and so can hearable items such as Ja, Nee, Hoi, Hallo,
  Help, but keep the events timed to that audio so the subtitle does not drift.
- **Show stress on a pronoun by spelling it in full.** Write m'n and z'n when unstressed and
  mijn, zijn when stressed, not italics: 'Hij pakte z'n biezen.' versus 'Omdat mijn auto niet
  wilde starten…'.
- **Do not carry English honorifics into a film that is not English.** Use the original term
  (keep 'Señor') or the Dutch one; keep Mr and Mrs for English originals only. 'dr. Jones' is
  an academic title, 'dokter Jones' a physician.
