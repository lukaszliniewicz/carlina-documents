"""Provider-neutral language-tag normalization shared by media applications."""

import re

_LANGUAGE_NAMES = {
    "ar": ("arabic", "ara"),
    "bg": ("bulgarian", "bul"),
    "br": ("breton", "bre"),
    "ca": ("catalan", "cat"),
    "cs": ("czech", "ces", "cze"),
    "da": ("danish", "dan"),
    "de": ("german", "deu", "ger"),
    "el": ("greek", "ell", "gre"),
    "en": ("english", "eng"),
    "es": ("spanish", "spa"),
    "et": ("estonian", "est"),
    "fa": ("persian", "fas", "per"),
    "fi": ("finnish", "fin"),
    "fil": ("filipino",),
    "fr": ("french", "fra", "fre"),
    "hi": ("hindi", "hin"),
    "hr": ("croatian", "hrv"),
    "he": ("hebrew", "heb"),
    "hu": ("hungarian", "hun"),
    "id": ("indonesian", "ind"),
    "it": ("italian", "ita"),
    "ja": ("japanese", "jpn"),
    "ko": ("korean", "kor"),
    "lt": ("lithuanian", "lit"),
    "lv": ("latvian", "lav"),
    "mk": ("macedonian", "mkd"),
    "ms": ("malay", "msa"),
    "mt": ("maltese", "mlt"),
    "nl": ("dutch", "nld", "dut"),
    "no": ("norwegian", "nor"),
    "pl": ("polish", "pol"),
    "pt": ("portuguese", "por"),
    "ro": ("romanian", "ron", "rum"),
    "ru": ("russian", "rus"),
    "sk": ("slovak", "slk", "slo"),
    "sl": ("slovenian", "slv"),
    "sq": ("albanian", "sqi", "alb"),
    "sv": ("swedish", "swe"),
    "th": ("thai", "tha"),
    "tr": ("turkish", "tur"),
    "uk": ("ukrainian", "ukr"),
    "vi": ("vietnamese", "vie"),
    "yue": ("cantonese",),
    "zh": ("chinese", "mandarin", "zho", "chi"),
}

_ALIASES = {
    alias: code
    for code, aliases in _LANGUAGE_NAMES.items()
    for alias in (code, *aliases)
}
_ALIASES.update(
    {
        "american english": "en-US",
        "english us": "en-US",
        "english-us": "en-US",
        "en-us": "en-US",
        "us english": "en-US",
        "british english": "en-GB",
        "english uk": "en-GB",
        "english-uk": "en-GB",
        "en-gb": "en-GB",
        "spanish latin america": "es-419",
        "es-419": "es-419",
        "portuguese brazil": "pt-BR",
        "pt-br": "pt-BR",
        "portuguese portugal": "pt-PT",
        "pt-pt": "pt-PT",
        "zh-cn": "zh-CN",
        "zh-tw": "zh-TW",
    }
)

_ISO3_TO_ISO2 = {
    alias: code
    for code, aliases in _LANGUAGE_NAMES.items()
    for alias in aliases
    if len(alias) == 3
}
_AUTO_VALUES = {"auto", "detect", "default", "none", "null"}


def normalize_language_tag(value: str | None) -> str | None:
    """Normalize common names and ISO spellings to a compact BCP-47-like tag."""

    raw = str(value or "").strip()
    if not raw:
        return None
    cleaned = re.sub(r"\s+", " ", raw).strip()
    lowered = cleaned.lower().replace("_", "-")
    if lowered in _AUTO_VALUES:
        return None
    if lowered in _ALIASES:
        return _ALIASES[lowered]

    candidate = lowered.replace(" ", "-")
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", candidate):
        return cleaned
    parts = candidate.split("-")
    normalized = [_ISO3_TO_ISO2.get(parts[0], parts[0])]
    normalized.extend(
        part.upper() if len(part) == 2 and part.isalpha() else part.lower()
        for part in parts[1:]
    )
    return "-".join(normalized)


__all__ = ["normalize_language_tag"]


# --- Joining text that a physical page or column break split ---------------
#
# A page or column break in the source is not a word boundary. Scripts that
# separate words with spaces need one restored when two fragments are joined;
# scripts that do not (Han, kana, Thai and similar) must be joined directly, or
# assembly invents a space inside a word. The decision uses only the characters
# on either side of the seam and the word touching it, so it needs no
# document-level language setting. Where only one side is written without
# spaces, the word at the seam decides: a Latin word set off by spaces in an
# English sentence keeps its space before a cited 漢字, while a Latin word run
# into Japanese text (``…はIARF`` + ``の会議``) joins directly.

# Code point ranges whose text is written without spaces between words. Hangul
# is deliberately absent: Korean separates words with spaces.
_UNSPACED_RANGES: tuple[tuple[int, int], ...] = (
    (0x0E00, 0x0EFF),  # Thai, Lao
    (0x0F00, 0x0FFF),  # Tibetan
    (0x1000, 0x109F),  # Myanmar
    (0x1780, 0x17FF),  # Khmer
    (0x19E0, 0x19FF),  # Khmer symbols
    (0x2E80, 0x2FDF),  # CJK radicals, Kangxi radicals
    (0x2FF0, 0x2FFF),  # Ideographic description characters
    (0x3000, 0x303F),  # CJK symbols and punctuation
    (0x3040, 0x30FF),  # Hiragana, Katakana
    (0x3100, 0x312F),  # Bopomofo
    (0x3190, 0x31FF),  # Kanbun, Bopomofo extended, CJK strokes, Katakana ext.
    (0x3200, 0x33FF),  # Enclosed CJK letters, CJK compatibility
    (0x3400, 0x4DBF),  # CJK extension A
    (0x4E00, 0x9FFF),  # CJK unified ideographs
    (0xA000, 0xA4CF),  # Yi
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
    (0xFE30, 0xFE4F),  # CJK compatibility forms
    (0xFF00, 0xFF9F),  # Fullwidth forms, halfwidth katakana
    (0xFFE0, 0xFFEF),  # Fullwidth signs
    (0x1B000, 0x1B2FF),  # Kana supplement and extensions
    (0x20000, 0x3134F),  # CJK extensions B-G
)

# Closing punctuation that attaches to the preceding word in spaced scripts.
_ATTACHING_PUNCTUATION = frozenset({",", ".", ";", ":", ")", "]", "’", "'", "”"})

# Sentence-final punctuation, including the ideographic and fullwidth forms.
SENTENCE_FINAL_PUNCTUATION = frozenset(
    {".", "!", "?", '"', "'", "”", "。", "！", "？", "．", "」", "』"}
)


def is_unspaced_character(character: str) -> bool:
    """Return whether ``character`` belongs to a script written without spaces."""

    if len(character) != 1:
        return False
    codepoint = ord(character)
    return any(start <= codepoint <= end for start, end in _UNSPACED_RANGES)


_SEAM_SCAN_LIMIT = 64


def _seam_word_spaced(text: str, *, from_end: bool) -> bool:
    """Whether the word touching the seam is set off by a space on its far side.

    The word is the run of characters of one kind (written with or without
    spaces) that touches the seam. It counts as spaced when whitespace ends it
    within the scan limit, rather than the fragment edge or the other kind.
    """

    characters = reversed(text) if from_end else iter(text)
    first = next(characters, "")
    unspaced = is_unspaced_character(first)
    for count, character in enumerate(characters):
        if count >= _SEAM_SCAN_LIMIT:
            return False
        if character.isspace():
            return True
        if is_unspaced_character(character) != unspaced:
            return False
    return False


def _joins_without_space(left_text: str, right_text: str) -> bool:
    left_unspaced = is_unspaced_character(left_text[-1])
    right_unspaced = is_unspaced_character(right_text[0])
    if left_unspaced and right_unspaced:
        return True
    if not left_unspaced and not right_unspaced:
        return False
    # Mixed scripts at the seam: keep a space when either word is itself set
    # off by spaces, as in a spaced sentence that cites a CJK word.
    return not (
        _seam_word_spaced(left_text, from_end=True)
        or _seam_word_spaced(right_text, from_end=False)
    )


def join_separator(left: str, right: str) -> str:
    """Return the text to put between two fragments split by a physical break.

    Whitespace already present at the seam is the caller's to keep or strip;
    this function decides only what a clean join needs.
    """

    left_text = left.rstrip()
    right_text = right.lstrip()
    if not left_text or not right_text:
        return ""
    if right_text[0] in _ATTACHING_PUNCTUATION:
        return ""
    return "" if _joins_without_space(left_text, right_text) else " "


def seam_whitespace_is_spurious(left: str, right: str) -> bool:
    """Return whether whitespace at a seam is one a clean join would not have."""

    left_text = left.rstrip()
    right_text = right.lstrip()
    has_whitespace = len(left_text) != len(left) or len(right_text) != len(right)
    return bool(
        has_whitespace
        and left_text
        and right_text
        and _joins_without_space(left_text, right_text)
    )
