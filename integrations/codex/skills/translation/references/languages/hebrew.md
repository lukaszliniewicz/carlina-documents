# Hebrew notes

Into Hebrew, mostly from English: conventions taken from software localisation and subtitling style guides. A client's own style guide overrides them. Out of Hebrew: points from Bible translation handbooks, which concern Biblical Hebrew. Check Hebrew examples in a right-to-left aware viewer before copying them.

## Punctuation and typography

- **Put punctuation outside the closing quotation mark.** In Hebrew a mark that is not part of
  the quoted words goes after the closing mark, even where US English practice puts it
  inside.
- **Drop the comma before Hebrew 'and' inside a clause.** A comma may precede it only when it
  joins two complete sentences.
- **Write '&' as the Hebrew word for 'and' in prose.** Keep '&' only where it belongs to a tag,
  placeholder, shortcut or code string.

## Numbers, dates and units

- **Write numbers under ten as words.** Keep the digit only when the number is attached to a
  unit of measurement, where the source's form is followed: 'There are three ways to get to
  the Settings menu' becomes שלוש דרכים; 'the file size is 3 MB' keeps the 3.

## Capitalisation, spelling and word formation

- **Mark titles with quotation marks where English uses capitals.** Enclose the titles of
  chapters, topics and sections in Hebrew quotation marks: a section called 'Save changes' is
  written "שמירת שינויים" in Hebrew text.

## Grammar carried over from the source

- **Do not render 'you can …' as a pronoun plus modal.** Use the impersonal construction:
  באפשרותך or ניתן in the present, תוכל in the future, עליך for 'you have to'.
- **Drop repeated possessives, especially 'your', when the referent is clear.** Hebrew marks
  possession on the noun, so write המחשב rather than המחשב שלך when the owner is obviously the
  reader.
- **For an unknown or mixed gender, work down the ladder.** Neutral noun or verb, plural noun
  and verb, both genders spelled out, the passive, and last the slashed form כותב/ת. Never
  default to masculine singular: write משתמשים עם ההרשאות המתאימות rather than משתמש בעל
  ההרשאות.
- **Avoid gender marking in prompts by choosing person and tense.** Future first person or past
  second person is spelled alike for both genders: האם אוכל? replaces האם אני יכול/ה?; past forms such as
  רצית and עשית are also written alike.

## Words and phrases

- **Use the loanword the field speaks, not the purist coinage.** אינטרנט, not מרשתת. The
  Academy term may be the correct Hebrew, but it makes technical prose read as translated.
- **Choose על ידי or באמצעות by what follows it.** Use על ידי when the complement is an action
  name (a verbal noun) and באמצעות when it names the medium, tool or means: הקובץ הודפס על ידי
  המזכירה versus הדפס באמצעות מדפסת הרשת.

## Address, politeness and tone

- **Use the second person plural for the reader throughout.** Make every verb and pronoun
  agree. The masculine singular is not unmarked in Hebrew, and the plural imperative and future
  are short and serve every reader: בחרו, תכתבו.

## Names, terms and abbreviations

- **Leave acronyms in Latin script.** Do not transliterate them into Hebrew letters; replace an
  acronym with a Hebrew term only where an accepted equivalent exists, keeping the Latin
  acronym in parentheses: RAM, ROM and USB stay as they are; I/O may become קלט/פלט and VAT
  becomes מע"מ.
- **Prefix no definite article to an untranslated English name.** This covers product, feature
  and trademark names in Latin script: Windows and IntelliSense, not ה-Windows.

## Software and technical texts

- **Inflect the verb for the gender Hebrew assigns a name.** Company and organisation names
  take feminine agreement, application names masculine: מיקרוסופט משדרגת את הגרסה החדשה (company, feminine).
- **Give the procedure title the verbal noun, each step כדי.** Each step takes כדי plus the
  infinitive: Title: יצירת קבוצה של תוויות; operation: כדי ליצור קבוצה של תוויות.
- **Label buttons, menu items and headings with the verbal noun.** Use הדפסה not הדפס/להדפיס;
  שמירה not שמור/לשמור.

## Subtitles and dubbing

- **Reopen the quotation mark on every line of one quotation.** Close it only at the end of the
  last line or where dialogue breaks in, and do the same for the lyrics of one song: three
  successive events each opened with " and closed with " only on the last.
- **Put an ellipsis before an interrupting title, none after it.** Write 'לא ידעתי מה לומר…' /
  '(FN) - שיקגו, יוני 2013 -' / 'האנשים נכנסו לאולם' with no leading ellipsis, unlike the
  European guides.
- **Write currency names in words, never as symbols.** Write 'זה עלה 30 דולר' rather than
  '$30'; a symbol can also change position in right-to-left text.
- **Expand symbols and URLs into spoken words for narration.** This covers voice-over, dubbing,
  audio description and read-aloud. Skip 'http://', read www as 'double u' three times, dot as
  נקודה, slash as קו נטוי, hyphen as מקף מחבר: http://www.microsoft.com is voiced as דאבל יו
  דאבל יו דאבל יו נקודה מייקרוסופט נקודה קום.

## Literary and religious texts

- **Translate out of Hebrew: render body-part terms by their function.** Nose for anger or
  patience, hand for power, mouth for speech, eyes for attitude: 'long of nose' is 'slow to
  anger, patient', and blast or smoke from the nostrils is anger.
- **Translate out of Hebrew: do not take kinship words literally.** These encode settlement or
  quality: 'Daughters of X' are villages near X, 'mother' the central town, 'fathers'
  ancestors, 'sons of X' those sharing quality X: 'Kiriath Arba and its daughters' is
  'surrounding villages'; 'son of wickedness' is a wicked person.
- **Translate out of Hebrew: render 'called X' as 'is X'.** Do so where the context predicates
  identity or status rather than reporting a naming event: 'He is called the God of the whole
  earth' becomes 'he is the God of the whole earth'; 'be called Son of the Most High' becomes
  'is the Son'.
- **Translate out of Hebrew: do not collapse alma into virgin** in a fresh translation from
  the Hebrew. Render it 'young woman' and attribute the virgin reading to the interpretive
  tradition it comes from. (Scripture quoted from an existing version keeps that version's
  wording.)
- **From Biblical Hebrew, treat same-spelling cognates as false friends.** Verify the period
  meaning: tohu vavohu is not 'disarray', khasar-lev is 'stupid' not 'cruel', mekatrot
  le'elohim is not 'complain to god'.
- **From Biblical Hebrew, re-parse ancient word order and tense markers.** Do not project
  modern syntax onto them. The vav-reversive makes tenses ambivalent, so a misread order
  inverts the sense, and the Aramaic chapters of Ezra and Daniel are easily missed as foreign.

## Translating out of Hebrew

- **Translate the force of a directive, not its wording.** Hebrew commands by asking about
  possibility where English uses an inclusive suggestion: 'Perhaps we shall/should go on to
  another topic' becomes 'Let's go on to another topic'.
