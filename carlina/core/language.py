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
