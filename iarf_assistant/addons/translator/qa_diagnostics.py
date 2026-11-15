"""Deterministic post-translation QA diagnostics for document-block projects.

QA is deliberately separate from target validation. Structural validation decides
whether a target may be persisted; this module reports review evidence and never
changes targets or human review decisions.
"""

from __future__ import annotations

import hashlib
import json
import statistics
import unicodedata
from collections import Counter, OrderedDict
from collections.abc import Mapping, Sequence
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from threading import RLock
from typing import Any, NamedTuple

import regex

from . import document_review
from .block_workspace_api import (
    BlockWorkspaceIntegrityError,
    _rehydrate_workspace_objects,
    load_block_source_plan,
)
from .working_glossary import snapshot_current_glossary_entries

QA_CONTRACT = "translator-qa-diagnostics-v1"
QA_RULESET = "translator-qa-rules-v2"
MAX_QA_LIMIT = 200
MAX_QA_FINDINGS_BY_SEVERITY = {
    "integrity": 6_000,
    "warning": 10_000,
    "advisory": 4_000,
}
MAX_QA_FINDINGS = sum(MAX_QA_FINDINGS_BY_SEVERITY.values())
MAX_SPELL_FINDINGS_PER_BLOCK = 6
MAX_JOINED_FINDINGS_PER_BLOCK = 12
MAX_SPLIT_FINDINGS_PER_BLOCK = 12
MAX_NUMBER_FINDINGS_PER_BLOCK = 12
MAX_PAIRING_FINDINGS_PER_BLOCK = 6
MAX_ALIGNMENT_FINDINGS_PER_BLOCK = 6
# Digits after a separator that make a thousands group (1,000 / 1.000 / 1 000).
_THOUSANDS_GROUP_DIGITS = 3
# A missing number up to this value may have been written out in words.
_SPELLED_OUT_MAX = 12
# A sentence group is flagged when its target/source length ratio falls below
# this share of the median ratio of the block's other groups. Groups shorter
# than the minimums are too noisy to judge or to serve as the reference.
_ALIGNMENT_SHORT_SHARE = 0.6
_ALIGNMENT_MIN_SOURCE_CHARS = 40
_ALIGNMENT_MIN_REFERENCE_CHARS = 12
MAX_SUGGESTIONS = 5

_CATEGORIES = (
    "integrity",
    "content",
    "references",
    "punctuation",
    "length",
    "terminology",
    "language",
    "tokens",
    "spelling",
)
_SEVERITIES = ("integrity", "warning", "advisory")
_WORD_RE = regex.compile(r"\p{L}(?:[\p{L}\p{M}'’\-]*\p{L})?")
_INDEX_TOKEN_RE = regex.compile(
    r"[\p{L}\p{N}](?:[\p{L}\p{N}\p{M}'’\-]*[\p{L}\p{N}])?"
)
_CITATION_RE = regex.compile(
    r"\[[^\]\n]{1,120},\s*(?:1[0-9]{3}|20[0-9]{2})[a-z]?\]"
)
_REPEAT_PUNCT_RE = regex.compile(
    r"([!?;,:])\1+|(?<!\.)\.{2}(?!\.)|(?<!\.)\.{4,}(?!\.)"
)
_CJK_RE = regex.compile(
    r"[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]"
)
_URL_RE = regex.compile(r"https?://\S+", regex.IGNORECASE)
_DOI_RE = regex.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", regex.IGNORECASE)
_NUMBER_RE = regex.compile(
    r"\p{Nd}+(?:[.,]\p{Nd}+|[ \u00a0\u2009\u202f'’]\p{Nd}{3}(?!\p{Nd}))*"
)
_DIGIT_RUN_RE = regex.compile(r"\p{Nd}+")
_BRACKET_PAIRS = {
    "(": ")",
    "[": "]",
    "{": "}",
    "（": "）",
    "［": "］",
    "【": "】",
    "「": "」",
    "『": "』",
    "《": "》",
    "〈": "〉",
}
_CLOSING_BRACKETS = {closer: opener for opener, closer in _BRACKET_PAIRS.items()}
_DOUBLE_QUOTES = frozenset('"“”„‟«»＂〝〞〟❝❞')

_LANGUAGE_DEFAULTS = {
    "en": ("en_US", "en_GB"),
    "de": ("de_DE", "de_AT", "de_CH"),
    "pl": ("pl_PL",),
    "nl": ("nl_NL", "nl_BE"),
    "fr": ("fr_FR",),
    "es": ("es_ES",),
    "it": ("it_IT",),
    "pt": ("pt_PT", "pt_BR"),
}


_QA_BLOCK_CACHE_MAX = 8_192
_QA_BLOCK_CACHE_MAX_FINDINGS_PER_BLOCK = 128
_QA_BLOCK_CACHE: OrderedDict[
    tuple[Any, ...], tuple[dict[str, Any], ...]
] = OrderedDict()
_QA_BLOCK_CACHE_LOCK = RLock()


def _qa_block_cache_get(key: tuple[Any, ...]) -> list[dict[str, Any]] | None:
    with _QA_BLOCK_CACHE_LOCK:
        cached = _QA_BLOCK_CACHE.get(key)
        if cached is None:
            return None
        _QA_BLOCK_CACHE.move_to_end(key)
        return [dict(item) for item in cached]


def _qa_block_cache_put(
    key: tuple[Any, ...], findings: Sequence[Mapping[str, Any]]
) -> None:
    if len(findings) > _QA_BLOCK_CACHE_MAX_FINDINGS_PER_BLOCK:
        return
    stored = tuple(dict(item) for item in findings)
    with _QA_BLOCK_CACHE_LOCK:
        _QA_BLOCK_CACHE[key] = stored
        _QA_BLOCK_CACHE.move_to_end(key)
        while len(_QA_BLOCK_CACHE) > _QA_BLOCK_CACHE_MAX:
            _QA_BLOCK_CACHE.popitem(last=False)


def _glossary_fingerprint(entries: Sequence[Mapping[str, Any]]) -> str:
    payload = json.dumps(
        list(entries),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _dictionary_signature(
    *,
    requested_language: str,
    selected_language: str | None,
    dictionary: Any | None,
    enchant_module: Any | None,
) -> str:
    """Fingerprint public dictionary identity and backend versions, not word data."""

    try:
        pyenchant_version = package_version("pyenchant")
    except PackageNotFoundError, OSError, ValueError:
        pyenchant_version = None
    if enchant_module is not None:
        try:
            backend_version = str(enchant_module.get_enchant_version())
        except AttributeError, OSError, RuntimeError, TypeError, ValueError:
            backend_version = None
    else:
        backend_version = None

    provider = getattr(dictionary, "provider", None)
    payload = {
        "requested_language": str(requested_language or ""),
        "selected_language": selected_language,
        "available": dictionary is not None,
        "dictionary_tag": getattr(dictionary, "tag", None),
        "provider": getattr(provider, "name", None),
        "pyenchant_version": pyenchant_version,
        "enchant_library_version": backend_version,
    }
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class QaDiagnosticsError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _error(code: str) -> QaDiagnosticsError:
    return QaDiagnosticsError(code)


def _normal(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _term_exact_normal(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _term_normal(value: str) -> str:
    return " ".join(_normal(value).split())


def _spell_key(value: str) -> str:
    """Preserve case because Hunspell dictionaries can be case-sensitive."""

    return unicodedata.normalize("NFKC", value)


def _wordish(character: str) -> bool:
    if not character:
        return False
    return character == "_" or unicodedata.category(character)[0] in {"L", "M", "N"}


def _contains_normalized(haystack: str, needle: str) -> bool:
    """Match normalized terms without accepting substrings inside larger words."""

    if not needle:
        return False
    offset = 0
    while True:
        start = haystack.find(needle, offset)
        if start < 0:
            return False
        end = start + len(needle)
        left_ok = not _wordish(needle[0]) or start == 0 or not _wordish(haystack[start - 1])
        right_ok = (
            not _wordish(needle[-1])
            or end == len(haystack)
            or not _wordish(haystack[end])
        )
        if left_ok and right_ok:
            return True
        offset = start + 1


def _reference_spans(value: str) -> tuple[tuple[int, int], ...]:
    spans = [
        *(match.span() for match in _CITATION_RE.finditer(value)),
        *(match.span() for match in _URL_RE.finditer(value)),
        *(match.span() for match in _DOI_RE.finditer(value)),
    ]
    spans.sort()
    return tuple(spans)


def _inside_any_span(start: int, end: int, spans: Sequence[tuple[int, int]]) -> bool:
    return any(start >= left and end <= right for left, right in spans)


def _cjk_share(value: str) -> float:
    nonspace = [character for character in value if not character.isspace()]
    if not nonspace:
        return 0.0
    return sum(1 for character in nonspace if _CJK_RE.fullmatch(character)) / len(nonspace)


def _prepare_glossary_entries(
    entries: Sequence[Mapping[str, Any]],
) -> list[tuple[Mapping[str, Any], tuple[str, ...], tuple[str, ...]]]:
    """Normalize terminology once per QA pass, not once per block and term."""

    prepared: list[tuple[Mapping[str, Any], tuple[str, ...], tuple[str, ...]]] = []
    for entry in entries:
        source_values = [entry.get("source_term"), *(entry.get("source_forms") or [])]
        target_values = [entry.get("target_term"), *(entry.get("accepted_target_forms") or [])]
        source_forms = tuple(
            dict.fromkeys(
                _term_normal(value)
                for value in source_values
                if isinstance(value, str) and value.strip()
            )
        )
        target_forms = tuple(
            dict.fromkeys(
                _term_normal(value)
                for value in target_values
                if isinstance(value, str) and value.strip()
            )
        )
        if source_forms and target_forms:
            prepared.append((entry, source_forms, target_forms))
    return prepared


class _GlossaryIndex:
    """Index source terminology by observed block and cheap lexical anchors."""

    __slots__ = ("entries", "by_key", "by_block", "fallback")

    def __init__(self, entries: Sequence[Mapping[str, Any]]):
        self.entries = tuple(_prepare_glossary_entries(entries))
        by_key: dict[str, set[int]] = {}
        by_block: dict[str, set[int]] = {}
        fallback: set[int] = set()
        for index, (entry, source_forms, _target_forms) in enumerate(self.entries):
            indexed = False
            for form in source_forms:
                key = self._form_key(form)
                if key is None:
                    continue
                by_key.setdefault(key, set()).add(index)
                indexed = True
            observations = entry.get("observations")
            if isinstance(observations, Sequence) and not isinstance(
                observations, (str, bytes)
            ):
                for observation in observations:
                    if not isinstance(observation, Mapping):
                        continue
                    block_id = observation.get("block_id")
                    if isinstance(block_id, str) and block_id:
                        by_block.setdefault(block_id, set()).add(index)
                        indexed = True
            if not indexed:
                fallback.add(index)
        self.by_key = {key: tuple(sorted(values)) for key, values in by_key.items()}
        self.by_block = {
            block_id: tuple(sorted(values)) for block_id, values in by_block.items()
        }
        self.fallback = tuple(sorted(fallback))

    @staticmethod
    def _form_key(form: str) -> str | None:
        cjk = _CJK_RE.search(form)
        if cjk is not None:
            return f"c:{cjk.group()}"
        word = _INDEX_TOKEN_RE.search(form)
        if word is not None:
            return f"w:{_normal(word.group())}"
        return None

    def candidates(
        self, source_text: str, block_id: str
    ) -> tuple[tuple[Mapping[str, Any], tuple[str, ...], tuple[str, ...]], ...]:
        if not self.entries:
            return ()
        source_normal = _term_normal(source_text)
        indices = set(self.fallback)
        indices.update(self.by_block.get(block_id, ()))
        for match in _INDEX_TOKEN_RE.finditer(source_normal):
            indices.update(self.by_key.get(f"w:{_normal(match.group())}", ()))
        for match in _CJK_RE.finditer(source_normal):
            indices.update(self.by_key.get(f"c:{match.group()}", ()))
        return tuple(self.entries[index] for index in sorted(indices))


def _excerpt(value: str, start: int, end: int, radius: int = 80) -> dict[str, Any]:
    left = max(0, start - radius)
    right = min(len(value), end + radius)
    return {
        "span": [start - left, end - left],
        "text": value[left:right],
        "match": value[start:end],
    }


def _finding_id(
    source_plan_id: str,
    block_id: str,
    target_version: int | None,
    code: str,
    target_span: tuple[int, int] | None,
    source_span: tuple[int, int] | None,
    evidence: Mapping[str, Any],
) -> str:
    payload = json.dumps(
        {
            "ruleset": QA_RULESET,
            "source_plan_id": source_plan_id,
            "block_id": block_id,
            "target_version": target_version,
            "code": code,
            "target_span": target_span,
            "source_span": source_span,
            "evidence": evidence,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _finding(
    *,
    workspace: Any,
    pages: Mapping[str, list[int]],
    block: Any,
    target_version: int | None,
    code: str,
    category: str,
    severity: str,
    message: str,
    source_text: str,
    target_text: str,
    source_span: tuple[int, int] | None = None,
    target_span: tuple[int, int] | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    evidence_value = dict(evidence or {})
    item = {
        "id": _finding_id(
            workspace.record.id,
            block.id,
            target_version,
            code,
            target_span,
            source_span,
            evidence_value,
        ),
        "code": code,
        "category": category,
        "severity": severity,
        "message": message,
        "block_id": block.id,
        "order": block.order,
        "role": block.role,
        "source_pages": list(pages.get(block.id, [])),
        "target_version": target_version,
        "evidence": evidence_value,
        "_qa_location": {
            "source": (
                {"start": source_span[0], "end": source_span[1]}
                if source_span is not None
                else None
            ),
            "target": (
                {"start": target_span[0], "end": target_span[1]}
                if target_span is not None
                else None
            ),
        },
    }
    if source_span is not None:
        item["source"] = _excerpt(source_text, *source_span)
    if target_span is not None:
        item["target"] = _excerpt(target_text, *target_span)
    return item


class _EnchantDictionary:
    def __init__(self, language: str):
        self.requested_language = language
        self.language: str | None = None
        self._dictionary: Any | None = None
        self._checks: dict[str, bool] = {}
        self._suggestions: dict[str, tuple[str, ...]] = {}
        self.signature = _dictionary_signature(
            requested_language=language,
            selected_language=None,
            dictionary=None,
            enchant_module=None,
        )
        try:
            import enchant
        except Exception:
            return
        self.signature = _dictionary_signature(
            requested_language=language,
            selected_language=None,
            dictionary=None,
            enchant_module=enchant,
        )
        requested = str(language or "").replace("-", "_")
        prefix = requested.split("_", 1)[0].casefold()
        try:
            available = list(enchant.list_languages())
        except Exception:
            return
        candidates = [requested, *_LANGUAGE_DEFAULTS.get(prefix, ())]
        candidates.extend(
            item for item in available if item.split("_", 1)[0].casefold() == prefix
        )
        selected = next((item for item in candidates if item in available), None)
        if selected is None:
            return
        try:
            self._dictionary = enchant.Dict(selected)
        except Exception:
            return
        self.language = selected
        self.signature = _dictionary_signature(
            requested_language=language,
            selected_language=self.language,
            dictionary=self._dictionary,
            enchant_module=enchant,
        )

    @property
    def available(self) -> bool:
        return self._dictionary is not None

    def check(self, word: str) -> bool:
        key = _spell_key(word)
        if key not in self._checks:
            try:
                self._checks[key] = bool(self._dictionary and self._dictionary.check(word))
            except Exception:
                self._checks[key] = False
        return self._checks[key]

    def suggest(self, word: str) -> tuple[str, ...]:
        key = _spell_key(word)
        if key not in self._suggestions:
            try:
                values = self._dictionary.suggest(word) if self._dictionary else []
            except Exception:
                values = []
            self._suggestions[key] = tuple(
                item for item in values if isinstance(item, str) and item
            )[:MAX_SUGGESTIONS]
        return self._suggestions[key]


def _words(value: str) -> list[tuple[str, int, int]]:
    return [(match.group(), match.start(), match.end()) for match in _WORD_RE.finditer(value)]


def _glossary_words(entries: Sequence[Mapping[str, Any]]) -> set[str]:
    result: set[str] = set()
    for entry in entries:
        values = [entry.get("target_term")]
        values.extend(entry.get("accepted_target_forms") or [])
        for value in values:
            if not isinstance(value, str):
                continue
            for word, _start, _end in _words(value):
                result.add(_normal(word))
    return result


def _entry_applies(
    entry: Mapping[str, Any],
    source_normal: str,
    source_forms: Sequence[str],
    block_id: str,
) -> bool:
    observations = entry.get("observations")
    if isinstance(observations, Sequence) and not isinstance(observations, (str, bytes)):
        if any(
            isinstance(item, Mapping) and item.get("block_id") == block_id
            for item in observations
        ):
            return True
    return any(_contains_normalized(source_normal, form) for form in source_forms)


def _terminology_findings(
    *,
    workspace: Any,
    pages: Mapping[str, list[int]],
    block: Any,
    target_version: int | None,
    source_text: str,
    target_text: str,
    entries: Sequence[
        tuple[Mapping[str, Any], tuple[str, ...], tuple[str, ...]]
    ],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    source_normal = _term_normal(source_text)
    target_normal = _term_normal(target_text)
    target_exact = _term_exact_normal(target_text)
    for entry, source_forms, target_forms in entries:
        if not _entry_applies(entry, source_normal, source_forms, str(block.id)):
            continue
        target_term = entry.get("target_term")
        if not isinstance(target_term, str) or not target_term:
            continue
        accepted = [
            item
            for item in (entry.get("accepted_target_forms") or [])
            if isinstance(item, str) and item
        ]
        exact = entry.get("match_policy") == "exact"
        if exact:
            exact_forms = tuple(
                _term_exact_normal(item) for item in (target_term, *accepted)
            )
            matched = any(
                _contains_normalized(target_exact, form) for form in exact_forms
            )
        else:
            matched = any(
                _contains_normalized(target_normal, form) for form in target_forms
            )
        if matched:
            continue
        findings.append(
            _finding(
                workspace=workspace,
                pages=pages,
                block=block,
                target_version=target_version,
                code="glossary-exact-missing" if exact else "glossary-preferred-divergence",
                category="terminology",
                severity="warning" if exact else "advisory",
                message=(
                    "An exact glossary rendering is absent from the target."
                    if exact
                    else "The target does not contain the preferred glossary rendering or an accepted contextual variant."
                ),
                source_text=source_text,
                target_text=target_text,
                evidence={
                    "concept_id": entry.get("concept_id"),
                    "source_term": entry.get("source_term"),
                    "preferred_target": target_term,
                    "accepted_target_forms": accepted,
                    "match_policy": entry.get("match_policy", "preferred"),
                    "authority": entry.get("authority"),
                },
            )
        )
    return findings


def _citation_findings(
    *,
    workspace: Any,
    pages: Mapping[str, list[int]],
    block: Any,
    target_version: int | None,
    source_text: str,
    target_text: str,
) -> list[dict[str, Any]]:
    source_matches = list(_CITATION_RE.finditer(source_text))
    target_matches = list(_CITATION_RE.finditer(target_text))
    source_counter = Counter(match.group() for match in source_matches)
    target_counter = Counter(match.group() for match in target_matches)
    missing_counts = source_counter - target_counter
    extra_counts = target_counter - source_counter
    if not missing_counts and not extra_counts:
        return []

    def unmatched(
        matches: Sequence[regex.Match[str]], counts: Counter[str]
    ) -> list[regex.Match[str]]:
        remaining = counts.copy()
        result: list[regex.Match[str]] = []
        for match in matches:
            key = match.group()
            if remaining[key] > 0:
                result.append(match)
                remaining[key] -= 1
        return result

    missing = unmatched(source_matches, missing_counts)
    extras = unmatched(target_matches, extra_counts)
    paired_count = min(len(missing), len(extras))
    findings: list[dict[str, Any]] = []
    for index in range(paired_count):
        source_match = missing[index]
        target_match = extras[index]
        findings.append(
            _finding(
                workspace=workspace,
                pages=pages,
                block=block,
                target_version=target_version,
                code="citation-key-mutated-or-missing",
                category="references",
                severity="integrity",
                message="A source citation key appears to have been mutated in the target.",
                source_text=source_text,
                target_text=target_text,
                source_span=source_match.span(),
                target_span=target_match.span(),
                evidence={
                    "kind": "mutation",
                    "source_key": source_match.group(),
                    "target_key": target_match.group(),
                },
            )
        )
    for source_match in missing[paired_count:]:
        findings.append(
            _finding(
                workspace=workspace,
                pages=pages,
                block=block,
                target_version=target_version,
                code="citation-key-mutated-or-missing",
                category="references",
                severity="integrity",
                message="A source citation key is missing from the target.",
                source_text=source_text,
                target_text=target_text,
                source_span=source_match.span(),
                evidence={"kind": "missing", "source_key": source_match.group()},
            )
        )
    for target_match in extras[paired_count:]:
        findings.append(
            _finding(
                workspace=workspace,
                pages=pages,
                block=block,
                target_version=target_version,
                code="citation-key-added",
                category="references",
                severity="integrity",
                message="The target contains a citation key that is absent from the source.",
                source_text=source_text,
                target_text=target_text,
                target_span=target_match.span(),
                evidence={"kind": "added", "target_key": target_match.group()},
            )
        )
    return findings


def _punctuation_findings(
    *,
    workspace: Any,
    pages: Mapping[str, list[int]],
    block: Any,
    target_version: int | None,
    source_text: str,
    target_text: str,
) -> list[dict[str, Any]]:
    return [
        _finding(
            workspace=workspace,
            pages=pages,
            block=block,
            target_version=target_version,
            code="repeated-punctuation",
            category="punctuation",
            severity="warning",
            message="The target contains an unusual repeated punctuation sequence.",
            source_text=source_text,
            target_text=target_text,
            target_span=(match.start(), match.end()),
            evidence={"sequence": match.group()},
        )
        for match in list(_REPEAT_PUNCT_RE.finditer(target_text))[:12]
    ]


def _unbalanced_brackets(value: str) -> list[tuple[int, str, str]]:
    """Report brackets that never pair, ignoring reference-only text."""

    spans = _reference_spans(value)
    stack: list[tuple[int, str]] = []
    problems: list[tuple[int, str, str]] = []
    for index, character in enumerate(value):
        if character not in _BRACKET_PAIRS and character not in _CLOSING_BRACKETS:
            continue
        if _inside_any_span(index, index + 1, spans):
            continue
        if character in _BRACKET_PAIRS:
            stack.append((index, character))
            continue
        if stack and _BRACKET_PAIRS[stack[-1][1]] == character:
            stack.pop()
            continue
        problems.append((index, character, "unopened"))
    problems.extend((index, character, "unclosed") for index, character in stack)
    problems.sort()
    return problems


def _pairing_findings(
    *,
    workspace: Any,
    pages: Mapping[str, list[int]],
    block: Any,
    target_version: int | None,
    source_text: str,
    target_text: str,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if not _unbalanced_brackets(source_text):
        for index, character, problem in _unbalanced_brackets(target_text):
            findings.append(
                _finding(
                    workspace=workspace,
                    pages=pages,
                    block=block,
                    target_version=target_version,
                    code="pairing-mismatch",
                    category="punctuation",
                    severity="warning" if problem == "unclosed" else "advisory",
                    message=(
                        "The target has an unclosed bracket."
                        if problem == "unclosed"
                        else "The target has a closing bracket without an opening one."
                    ),
                    source_text=source_text,
                    target_text=target_text,
                    target_span=(index, index + 1),
                    evidence={"mark": character, "problem": problem},
                )
            )
    source_quotes = [
        character for character in source_text if character in _DOUBLE_QUOTES
    ]
    target_quotes = [
        (index, character)
        for index, character in enumerate(target_text)
        if character in _DOUBLE_QUOTES
    ]
    if len(source_quotes) % 2 == 0 and len(target_quotes) % 2 == 1:
        index, character = target_quotes[-1]
        findings.append(
            _finding(
                workspace=workspace,
                pages=pages,
                block=block,
                target_version=target_version,
                code="pairing-mismatch",
                category="punctuation",
                severity="advisory",
                message="The target has an unpaired quotation mark.",
                source_text=source_text,
                target_text=target_text,
                target_span=(index, index + 1),
                evidence={
                    "mark": character,
                    "problem": "odd_quote_count",
                    "source_count": len(source_quotes),
                    "target_count": len(target_quotes),
                },
            )
        )
    return findings[:MAX_PAIRING_FINDINGS_PER_BLOCK]


def _number_key(digits: str) -> str:
    """Return comparable ASCII digits without leading zeros."""

    ascii_digits = "".join(
        str(digit)
        for character in digits
        if (digit := unicodedata.digit(character, None)) is not None
    )
    return ascii_digits.lstrip("0") or "0"


def _number_items(value: str) -> list[tuple[str, str, tuple[int, int]]]:
    """Split printed numerals into comparable values: (text, key, span).

    A separator followed by exactly three digits groups thousands; any other
    separator ends one value and starts the next. Decimals, ranges and dotted
    dates are therefore compared part by part, whichever convention each
    language uses, and US and German date orders yield the same values.
    """

    spans = _reference_spans(value)
    items: list[tuple[str, str, tuple[int, int]]] = []
    for match in _NUMBER_RE.finditer(value):
        if _inside_any_span(match.start(), match.end(), spans):
            continue
        parts = list(_DIGIT_RUN_RE.finditer(value, match.start(), match.end()))
        start, end = parts[0].start(), parts[0].end()
        digits = parts[0].group()
        for part in parts[1:]:
            if len(part.group()) == _THOUSANDS_GROUP_DIGITS:
                digits += part.group()
                end = part.end()
                continue
            items.append((value[start:end], _number_key(digits), (start, end)))
            start, end, digits = part.start(), part.end(), part.group()
        items.append((value[start:end], _number_key(digits), (start, end)))
    return items


def _surplus_numbers(
    items: Sequence[tuple[str, str, tuple[int, int]]],
    other_side: Mapping[str, int],
) -> list[tuple[str, str, tuple[int, int]]]:
    """Pair occurrences with the other side's count first; return the rest."""

    paired: Counter[str] = Counter()
    result: list[tuple[str, str, tuple[int, int]]] = []
    for item in items:
        key = item[1]
        if paired[key] < other_side.get(key, 0):
            paired[key] += 1
            continue
        result.append(item)
    return result


def _number_findings(
    *,
    workspace: Any,
    pages: Mapping[str, list[int]],
    block: Any,
    target_version: int | None,
    source_text: str,
    target_text: str,
) -> list[dict[str, Any]]:
    source_items = _number_items(source_text)
    target_items = _number_items(target_text)
    source_counter = Counter(item[1] for item in source_items)
    target_counter = Counter(item[1] for item in target_items)
    if source_counter == target_counter:
        return []
    missing = _surplus_numbers(source_items, target_counter)
    added = _surplus_numbers(target_items, source_counter)
    changed_value = bool(missing) and bool(added)
    findings: list[dict[str, Any]] = []
    for matched_text, key, span in missing:
        spelled_out = (
            matched_text.isdigit()
            and len(matched_text) <= len(str(_SPELLED_OUT_MAX))
            and int(matched_text) <= _SPELLED_OUT_MAX
        )
        findings.append(
            _finding(
                workspace=workspace,
                pages=pages,
                block=block,
                target_version=target_version,
                code="number-mismatch",
                category="content",
                severity="advisory" if spelled_out or not changed_value else "warning",
                message="A number in the source does not appear in the target.",
                source_text=source_text,
                target_text=target_text,
                source_span=span,
                evidence={
                    "number": matched_text,
                    "normalized": key,
                    "side": "source_only",
                    "possibly_spelled_out": spelled_out,
                },
            )
        )
    for matched_text, key, span in added:
        findings.append(
            _finding(
                workspace=workspace,
                pages=pages,
                block=block,
                target_version=target_version,
                code="number-mismatch",
                category="content",
                severity="warning" if changed_value else "advisory",
                message="A number in the target does not appear in the source.",
                source_text=source_text,
                target_text=target_text,
                target_span=span,
                evidence={
                    "number": matched_text,
                    "normalized": key,
                    "side": "target_only",
                    "possibly_spelled_out": False,
                },
            )
        )
    return findings[:MAX_NUMBER_FINDINGS_PER_BLOCK]


def _length_findings(
    *,
    workspace: Any,
    pages: Mapping[str, list[int]],
    block: Any,
    target_version: int | None,
    source_text: str,
    target_text: str,
) -> list[dict[str, Any]]:
    source_chars = len("".join(source_text.split()))
    target_chars = len("".join(target_text.split()))
    if source_chars < 24 or target_chars == 0:
        return []
    source_cjk = _cjk_share(source_text)
    target_cjk = _cjk_share(target_text)
    lower, upper = 0.28, 3.5
    if source_cjk >= 0.25 and target_cjk < 0.10:
        lower, upper = 0.18, 8.0
    elif target_cjk >= 0.25 and source_cjk < 0.10:
        lower, upper = 0.12, 4.5
    elif source_cjk >= 0.25 and target_cjk >= 0.25:
        lower, upper = 0.20, 5.0
    ratio = target_chars / source_chars
    if lower <= ratio <= upper:
        return []
    return [
        _finding(
            workspace=workspace,
            pages=pages,
            block=block,
            target_version=target_version,
            code="abnormal-length-ratio",
            category="length",
            severity="warning",
            message="Source and target lengths differ unusually strongly.",
            source_text=source_text,
            target_text=target_text,
            evidence={
                "source_nonspace_chars": source_chars,
                "target_nonspace_chars": target_chars,
                "target_to_source_ratio": round(ratio, 3),
                "expected_ratio_range": [lower, upper],
                "source_cjk_share": round(source_cjk, 3),
                "target_cjk_share": round(target_cjk, 3),
            },
        )
    ]


def _sentence_texts(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    texts: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        text = item.get("text")
        if isinstance(text, str):
            texts.append(text)
    return texts


def _sentence_span(texts: Sequence[str], haystack: str) -> tuple[int, int] | None:
    if not texts:
        return None
    start = haystack.find(texts[0])
    if start < 0:
        return None
    last_start = haystack.find(texts[-1], start)
    if last_start < 0:
        return (start, start + len(texts[0]))
    return (start, last_start + len(texts[-1]))


class _GroupMeasure(NamedTuple):
    group: Mapping[str, Any]
    source_sentences: list[str]
    target_sentences: list[str]
    source_chars: int
    target_chars: int
    ratio: float | None


def _measure_group(group: Mapping[str, Any]) -> _GroupMeasure:
    source_sentences = _sentence_texts(group.get("source_sentences"))
    target_sentences = _sentence_texts(group.get("target_sentences"))
    source_chars = len("".join(" ".join(source_sentences).split()))
    target_chars = len("".join(" ".join(target_sentences).split()))
    return _GroupMeasure(
        group,
        source_sentences,
        target_sentences,
        source_chars,
        target_chars,
        target_chars / source_chars if source_chars > 0 else None,
    )


def _alignment_coverage_findings(
    *,
    workspace: Any,
    pages: Mapping[str, list[int]],
    block: Any,
    target_version: int | None,
    source_text: str,
    target_text: str,
    alignment: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    groups = alignment.get("groups") if isinstance(alignment, Mapping) else None
    if not isinstance(groups, list) or len(groups) < 2:
        return []
    measured = [_measure_group(group) for group in groups if isinstance(group, Mapping)]
    findings: list[dict[str, Any]] = []
    for index, measure in enumerate(measured):
        ratio = measure.ratio
        if ratio is None or measure.source_chars < _ALIGNMENT_MIN_SOURCE_CHARS:
            continue
        reference_ratios = [
            other.ratio
            for position, other in enumerate(measured)
            if position != index
            and other.ratio is not None
            and other.source_chars >= _ALIGNMENT_MIN_REFERENCE_CHARS
        ]
        if not reference_ratios:
            continue
        reference = statistics.median(reference_ratios)
        if ratio >= _ALIGNMENT_SHORT_SHARE * reference:
            continue
        findings.append(
            _finding(
                workspace=workspace,
                pages=pages,
                block=block,
                target_version=target_version,
                code="alignment-coverage",
                category="length",
                severity="advisory",
                message=(
                    "A sentence group is much shorter than the rest of its block;"
                    " check it for omitted content."
                ),
                source_text=source_text,
                target_text=target_text,
                source_span=_sentence_span(measure.source_sentences, source_text),
                target_span=_sentence_span(measure.target_sentences, target_text),
                evidence={
                    "group_order": measure.group.get("order"),
                    "kind": measure.group.get("kind"),
                    "source_sentence_count": len(measure.source_sentences),
                    "target_sentence_count": len(measure.target_sentences),
                    "source_nonspace_chars": measure.source_chars,
                    "target_nonspace_chars": measure.target_chars,
                    "ratio": round(ratio, 3),
                    "reference_ratio": round(reference, 3),
                },
            )
        )
        if len(findings) >= MAX_ALIGNMENT_FINDINGS_PER_BLOCK:
            break
    return findings


def _validation_findings(
    *,
    workspace: Any,
    pages: Mapping[str, list[int]],
    block: Any,
    target_version: int | None,
    source_text: str,
    target_text: str,
    target_projection: Mapping[str, Any],
) -> list[dict[str, Any]]:
    validation = target_projection.get("validation")
    if not isinstance(validation, Mapping):
        return []
    raw = validation.get("findings")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    codes = [
        item.get("code")
        for item in raw
        if isinstance(item, Mapping) and isinstance(item.get("code"), str)
    ]
    if not codes:
        return []
    return [
        _finding(
            workspace=workspace,
            pages=pages,
            block=block,
            target_version=target_version,
            code="target-structural-integrity",
            category="integrity",
            severity="integrity",
            message="The active target carries structural validation findings.",
            source_text=source_text,
            target_text=target_text,
            evidence={"validation_codes": codes[:32]},
        )
    ]


def _linguistic_findings(
    *,
    workspace: Any,
    pages: Mapping[str, list[int]],
    block: Any,
    target_version: int | None,
    source_text: str,
    target_text: str,
    target_dictionary: _EnchantDictionary,
    source_dictionary: _EnchantDictionary,
    accepted_words: set[str],
    include_spelling: bool = True,
    include_tokens: bool = True,
    include_language: bool = True,
) -> list[dict[str, Any]]:
    if not target_dictionary.available or not (
        include_spelling or include_tokens or include_language
    ):
        return []
    tokens = _words(target_text)
    ignored_spans = _reference_spans(target_text)
    findings: list[dict[str, Any]] = []
    spelling_count = 0
    joined_count = 0
    split_count = 0
    source_only: list[tuple[str, int, int]] = []
    token_state: list[tuple[str, int, int, str, bool]] = []
    for word, start, end in tokens:
        normalized = _normal(word)
        ignored = (
            len(normalized) < 2
            or normalized in accepted_words
            or _inside_any_span(start, end, ignored_spans)
        )
        valid = True if ignored else target_dictionary.check(word)
        token_state.append((word, start, end, normalized, valid))
        if ignored or valid:
            continue

        need_joined = include_tokens and joined_count < MAX_JOINED_FINDINGS_PER_BLOCK
        need_spelling = (
            include_spelling and spelling_count < MAX_SPELL_FINDINGS_PER_BLOCK
        )
        suggestions = (
            target_dictionary.suggest(word) if need_joined or need_spelling else ()
        )
        joined = None
        if need_joined:
            joined = next(
                (
                    suggestion
                    for suggestion in suggestions
                    if " " in suggestion
                    and all(
                        target_dictionary.check(part)
                        for part in suggestion.split()
                    )
                ),
                None,
            )
        if joined is not None:
            findings.append(
                _finding(
                    workspace=workspace,
                    pages=pages,
                    block=block,
                    target_version=target_version,
                    code="joined-token",
                    category="tokens",
                    severity="warning",
                    message="A target token may contain words that were accidentally joined.",
                    source_text=source_text,
                    target_text=target_text,
                    target_span=(start, end),
                    evidence={"suggested_split": joined},
                )
            )
            joined_count += 1
        elif need_spelling:
            findings.append(
                _finding(
                    workspace=workspace,
                    pages=pages,
                    block=block,
                    target_version=target_version,
                    code="spelling-suspect",
                    category="spelling",
                    severity="advisory",
                    message="The target dictionary does not recognize this token.",
                    source_text=source_text,
                    target_text=target_text,
                    target_span=(start, end),
                    evidence={"suggestions": list(suggestions)},
                )
            )
            spelling_count += 1
        if (
            include_language
            and source_dictionary.available
            and str(source_dictionary.language).split("_", 1)[0].casefold()
            != str(target_dictionary.language).split("_", 1)[0].casefold()
            and len(normalized) >= 3
            and source_dictionary.check(word)
        ):
            source_only.append((word, start, end))

    if include_tokens:
        for current, following in zip(token_state, token_state[1:]):
            if split_count >= MAX_SPLIT_FINDINGS_PER_BLOCK:
                break
            word, _start, end, normalized, valid = current
            next_word, next_start, next_end, next_normalized, next_valid = following
            if (
                normalized in accepted_words
                or next_normalized in accepted_words
                or _inside_any_span(_start, next_end, ignored_spans)
                or not target_text[end:next_start].isspace()
                or (valid and next_valid)
            ):
                continue
            combined = word + next_word
            if len(_normal(combined)) >= 5 and target_dictionary.check(combined):
                findings.append(
                    _finding(
                        workspace=workspace,
                        pages=pages,
                        block=block,
                        target_version=target_version,
                        code="split-token",
                        category="tokens",
                        severity="warning",
                        message="Two adjacent target tokens may be an accidentally split word.",
                        source_text=source_text,
                        target_text=target_text,
                        target_span=(_start, next_end),
                        evidence={"combined_form": combined},
                    )
                )
                split_count += 1

    if include_language and len(source_only) >= 2:
        groups: list[list[tuple[str, int, int]]] = []
        current_group: list[tuple[str, int, int]] = []
        for item in source_only:
            if current_group and item[1] - current_group[-1][2] > 3:
                if len(current_group) >= 2:
                    groups.append(current_group)
                current_group = []
            current_group.append(item)
        if len(current_group) >= 2:
            groups.append(current_group)
        for group in groups[:6]:
            findings.append(
                _finding(
                    workspace=workspace,
                    pages=pages,
                    block=block,
                    target_version=target_version,
                    code="source-language-leakage",
                    category="language",
                    severity="warning",
                    message="A run of target words looks source-language-only to the available dictionaries.",
                    source_text=source_text,
                    target_text=target_text,
                    target_span=(group[0][1], group[-1][2]),
                    evidence={
                        "source_dictionary": source_dictionary.language,
                        "target_dictionary": target_dictionary.language,
                        "tokens": [item[0] for item in group],
                    },
                )
            )
    return findings


def _diagnose_block(
    *,
    workspace: Any,
    pages: Mapping[str, list[int]],
    block: Any,
    target_projection: Mapping[str, Any],
    target_version: int | None,
    enabled_categories: set[str],
    linguistic_categories: set[str],
    glossary_index: _GlossaryIndex,
    accepted_words: set[str],
    target_dictionary: _EnchantDictionary,
    source_dictionary: _EnchantDictionary,
    include_spelling: bool,
) -> list[dict[str, Any]]:
    source_text = document_review._source_text(block)
    target_text = document_review._editor_text(target_projection.get("editor"))
    findings: list[dict[str, Any]] = []
    if "integrity" in enabled_categories:
        findings.extend(
            _validation_findings(
                workspace=workspace,
                pages=pages,
                block=block,
                target_version=target_version,
                source_text=source_text,
                target_text=target_text,
                target_projection=target_projection,
            )
        )
    if "references" in enabled_categories:
        findings.extend(
            _citation_findings(
                workspace=workspace,
                pages=pages,
                block=block,
                target_version=target_version,
                source_text=source_text,
                target_text=target_text,
            )
        )
    if "content" in enabled_categories:
        findings.extend(
            _number_findings(
                workspace=workspace,
                pages=pages,
                block=block,
                target_version=target_version,
                source_text=source_text,
                target_text=target_text,
            )
        )
    if "punctuation" in enabled_categories:
        findings.extend(
            _punctuation_findings(
                workspace=workspace,
                pages=pages,
                block=block,
                target_version=target_version,
                source_text=source_text,
                target_text=target_text,
            )
        )
        findings.extend(
            _pairing_findings(
                workspace=workspace,
                pages=pages,
                block=block,
                target_version=target_version,
                source_text=source_text,
                target_text=target_text,
            )
        )
    if "length" in enabled_categories:
        findings.extend(
            _length_findings(
                workspace=workspace,
                pages=pages,
                block=block,
                target_version=target_version,
                source_text=source_text,
                target_text=target_text,
            )
        )
        findings.extend(
            _alignment_coverage_findings(
                workspace=workspace,
                pages=pages,
                block=block,
                target_version=target_version,
                source_text=source_text,
                target_text=target_text,
                alignment=target_projection.get("alignment"),
            )
        )
    if "terminology" in enabled_categories:
        findings.extend(
            _terminology_findings(
                workspace=workspace,
                pages=pages,
                block=block,
                target_version=target_version,
                source_text=source_text,
                target_text=target_text,
                entries=glossary_index.candidates(source_text, str(block.id)),
            )
        )
    if linguistic_categories:
        findings.extend(
            _linguistic_findings(
                workspace=workspace,
                pages=pages,
                block=block,
                target_version=target_version,
                source_text=source_text,
                target_text=target_text,
                target_dictionary=target_dictionary,
                source_dictionary=source_dictionary,
                accepted_words=accepted_words,
                include_spelling=include_spelling
                and "spelling" in enabled_categories,
                include_tokens="tokens" in enabled_categories,
                include_language="language" in enabled_categories,
            )
        )
    return findings


_TOKEN_GROUP_CATEGORIES = frozenset({"tokens", "spelling", "terminology"})


def _finding_token(finding: Mapping[str, Any]) -> str | None:
    """Return the stable lexical anchor for findings that describe a token."""

    category = finding.get("category")
    if category not in _TOKEN_GROUP_CATEGORIES:
        return None
    evidence = finding.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    target = finding.get("target")
    target = target if isinstance(target, Mapping) else {}
    if category == "terminology":
        candidates = (
            finding.get("suspected_token"),
            evidence.get("suspected_token"),
            finding.get("source_term"),
            evidence.get("source_term"),
            finding.get("target_term"),
            evidence.get("target_term"),
            evidence.get("preferred_target"),
        )
    else:
        candidates = (
            finding.get("suspected_token"),
            evidence.get("suspected_token"),
            evidence.get("combined_form"),
            target.get("match"),
        )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _group_id(finding: Mapping[str, Any], token_key: str | None) -> str:
    if token_key is None:
        return str(finding["id"])
    payload = json.dumps(
        {
            "ruleset": QA_RULESET,
            "category": finding.get("category"),
            "code": finding.get("code"),
            "severity": finding.get("severity"),
            "token": token_key,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _group_findings(findings: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for finding in findings:
        raw_token = _finding_token(finding)
        token_key = _term_normal(raw_token) if raw_token is not None else None
        if not token_key:
            token_key = None
        identifier = _group_id(finding, token_key)
        group = groups.get(identifier)
        if group is None:
            display_token = raw_token[:256] if raw_token is not None else None
            group = {
                "id": identifier,
                "category": finding.get("category"),
                "code": finding.get("code"),
                "severity": finding.get("severity"),
                "token": display_token,
                "token_truncated": bool(raw_token and len(raw_token) > 256),
                "_token_key": token_key,
                "_findings": [],
            }
            groups[identifier] = group
        group["_findings"].append(finding)
    for group in groups.values():
        group["count"] = len(group["_findings"])
    return list(groups.values())


def _occurrence_record(
    finding: Mapping[str, Any], *, include_excerpts: bool
) -> dict[str, Any]:
    location = finding.get("_qa_location")
    location = location if isinstance(location, Mapping) else {}
    result: dict[str, Any] = {
        "id": finding.get("id"),
        "block_id": finding.get("block_id"),
        "order": finding.get("order"),
        "target_version": finding.get("target_version"),
        "location": {
            "source": location.get("source"),
            "target": location.get("target"),
        },
    }
    if include_excerpts:
        for key in ("source", "target"):
            excerpt = finding.get(key)
            if isinstance(excerpt, Mapping):
                result[key] = dict(excerpt)
    return result


def _finding_projection(
    finding: Mapping[str, Any], *, include_excerpts: bool
) -> dict[str, Any]:
    result = dict(finding)
    result.pop("_qa_location", None)
    if not include_excerpts:
        result.pop("source", None)
        result.pop("target", None)
    return result


def _group_projection(
    group: Mapping[str, Any], *, include_excerpts: bool, include_preview: bool
) -> dict[str, Any]:
    findings = group["_findings"]
    result = {
        "id": group["id"],
        "category": group["category"],
        "code": group["code"],
        "severity": group["severity"],
        "token": group["token"],
        "count": group["count"],
    }
    if group.get("token_truncated"):
        result["token_truncated"] = True
    if include_preview:
        result["preview_occurrences"] = [
            _occurrence_record(findings[0], include_excerpts=include_excerpts)
        ]
        result["preview_omitted"] = max(0, len(findings) - 1)
    return result


def _detector_signature(dictionary: Any, requested_language: str) -> str:
    signature = getattr(dictionary, "signature", None)
    if isinstance(signature, str) and signature:
        return signature
    # Keep compatibility with small dictionary adapters that predate the
    # signature field while still fencing their available identity.
    payload = {
        "requested_language": str(requested_language or ""),
        "language": getattr(dictionary, "language", None),
        "available": bool(getattr(dictionary, "available", False)),
    }
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def qa_diagnostics(
    project: Any,
    *,
    block_ids: Sequence[str] | None = None,
    categories: Sequence[str] = (),
    severities: Sequence[str] = (),
    cursor: str | None = None,
    limit: int = 100,
    include_spelling: bool = True,
    group_by: str = "finding",
    include_excerpts: bool = True,
    group_id: str | None = None,
) -> dict[str, Any]:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_QA_LIMIT
    ):
        raise _error("qa-limit-invalid")
    if not isinstance(group_by, str) or group_by not in {"finding", "token"}:
        raise _error("qa-grouping-invalid")
    if not isinstance(include_excerpts, bool):
        raise _error("qa-excerpts-flag-invalid")
    if group_id is not None and (
        not isinstance(group_id, str)
        or regex.fullmatch(r"[0-9a-f]{24}", group_id) is None
        or group_by != "token"
    ):
        raise _error("qa-group-id-invalid")
    for values, allowed, code in (
        (categories, _CATEGORIES, "qa-categories-invalid"),
        (severities, _SEVERITIES, "qa-severities-invalid"),
    ):
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise _error(code)
        if any(not isinstance(item, str) or item not in allowed for item in values):
            raise _error(code)
    canonical_categories = tuple(dict.fromkeys(categories))
    canonical_severities = tuple(dict.fromkeys(severities))
    if not isinstance(include_spelling, bool):
        raise _error("qa-spelling-flag-invalid")
    enabled_categories = set(canonical_categories) or set(_CATEGORIES)
    include_alignment = "length" in enabled_categories

    try:
        workspace = load_block_source_plan(project)
        try:
            pointers, targets = _rehydrate_workspace_objects(
                workspace, include_alignment=include_alignment
            )
            alignment_available = include_alignment
        except BlockWorkspaceIntegrityError:
            if not include_alignment:
                raise
            pointers, targets = _rehydrate_workspace_objects(workspace)
            alignment_available = False
    except BlockWorkspaceIntegrityError as exc:
        raise _error(getattr(exc, "code", "qa-source-invalid")) from exc
    blocks = list(workspace.plan.blocks)
    if block_ids is not None:
        if (
            isinstance(block_ids, (str, bytes))
            or not isinstance(block_ids, Sequence)
            or not block_ids
            or len(block_ids) > 512
            or any(not isinstance(item, str) or not item for item in block_ids)
            or len(set(block_ids)) != len(block_ids)
        ):
            raise _error("qa-block-ids-invalid")
        requested = set(block_ids)
        if not requested.issubset({block.id for block in blocks}):
            raise _error("qa-block-not-found")
        blocks = [block for block in blocks if block.id in requested]

    pages = document_review._source_page_map(workspace)
    linguistic_categories = enabled_categories & {"language", "tokens", "spelling"}
    needs_glossary = "terminology" in enabled_categories or bool(linguistic_categories)
    entries = (
        snapshot_current_glossary_entries(workspace.record) if needs_glossary else []
    )
    glossary_index = _GlossaryIndex(entries)
    accepted_words = _glossary_words(entries)
    target_dictionary = _EnchantDictionary(getattr(project, "target_lang", ""))
    source_dictionary = _EnchantDictionary(getattr(project, "source_lang", ""))
    glossary_key = _glossary_fingerprint(entries) if needs_glossary else ""
    detector_profile = (
        tuple(sorted(enabled_categories)),
        include_spelling,
        target_dictionary.language,
        source_dictionary.language,
        _detector_signature(target_dictionary, getattr(project, "target_lang", "")),
        _detector_signature(source_dictionary, getattr(project, "source_lang", "")),
        glossary_key,
    )

    findings_by_severity: dict[str, list[dict[str, Any]]] = {
        severity: [] for severity in _SEVERITIES
    }
    omitted_by_severity: Counter[str] = Counter()
    omitted_by_category: Counter[str] = Counter()
    for block in blocks:
        target_projection = targets.get(block.id)
        if target_projection is None:
            continue
        target_version = target_projection.get("active_version")
        if isinstance(target_version, bool) or not isinstance(target_version, int):
            target_version = None
        cache_key = (
            QA_RULESET,
            workspace.record.id,
            workspace.active_version,
            str(block.id),
            target_version,
            alignment_available,
            detector_profile,
        )
        block_findings = _qa_block_cache_get(cache_key)
        if block_findings is None:
            block_findings = _diagnose_block(
                workspace=workspace,
                pages=pages,
                block=block,
                target_projection=target_projection,
                target_version=target_version,
                enabled_categories=enabled_categories,
                linguistic_categories=linguistic_categories,
                glossary_index=glossary_index,
                accepted_words=accepted_words,
                target_dictionary=target_dictionary,
                source_dictionary=source_dictionary,
                include_spelling=include_spelling,
            )
            _qa_block_cache_put(cache_key, block_findings)
        for finding in block_findings:
            severity = str(finding["severity"])
            if canonical_severities and severity not in canonical_severities:
                continue
            bucket = findings_by_severity[severity]
            if len(bucket) >= MAX_QA_FINDINGS_BY_SEVERITY[severity]:
                omitted_by_severity[severity] += 1
                omitted_by_category[str(finding["category"])] += 1
                continue
            bucket.append(finding)

    findings = [
        finding
        for severity in _SEVERITIES
        for finding in findings_by_severity[severity]
    ]
    findings.sort(
        key=lambda item: (
            int(item["order"]),
            _SEVERITIES.index(item["severity"]),
            _CATEGORIES.index(item["category"]),
            str(item["code"]),
            str(item["id"]),
        )
    )
    target_versions = [
        (block.id, int(pointers[block.id].version))
        for block in blocks
        if block.id in pointers
    ]
    fingerprint = document_review._fingerprint(
        {
            "kind": "qa",
            "ruleset": QA_RULESET,
            "project_id": str(getattr(project, "id", "")),
            "source_plan_id": workspace.record.id,
            "source_plan_version": workspace.active_version,
            "target_versions": target_versions,
            "block_ids": [block.id for block in blocks],
            "categories": canonical_categories,
            "severities": canonical_severities,
            "include_spelling": include_spelling,
            "detector_profile": detector_profile,
            "group_by": group_by,
            "include_excerpts": include_excerpts,
            "group_id": group_id,
        }
    )

    page: list[dict[str, Any]] = []
    group_page: list[dict[str, Any]] = []
    occurrence_page: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    summary_findings: Sequence[Mapping[str, Any]] = findings
    pagination_total = len(findings)
    if group_by == "token":
        groups = _group_findings(findings)
        if group_id is not None:
            selected_group = next(
                (group for group in groups if group["id"] == group_id), None
            )
            if selected_group is None:
                raise _error("qa-group-not-found")
            occurrences = selected_group["_findings"]
            pagination_total = len(occurrences)
            offset = document_review._decode_cursor(cursor, fingerprint)
            selected_occurrences = occurrences[offset : offset + limit]
            occurrence_page = [
                _occurrence_record(item, include_excerpts=include_excerpts)
                for item in selected_occurrences
            ]
            next_offset = (
                offset + len(occurrence_page)
                if offset + len(occurrence_page) < len(occurrences)
                else None
            )
            group_page = [
                _group_projection(
                    selected_group,
                    include_excerpts=include_excerpts,
                    include_preview=False,
                )
            ]
        else:
            offset = document_review._decode_cursor(cursor, fingerprint)
            selected_groups = groups[offset : offset + limit]
            group_page = [
                _group_projection(
                    group,
                    include_excerpts=include_excerpts,
                    include_preview=True,
                )
                for group in selected_groups
            ]
            next_offset = (
                offset + len(group_page)
                if offset + len(group_page) < len(groups)
                else None
            )
            pagination_total = len(groups)
    else:
        offset = document_review._decode_cursor(cursor, fingerprint)
        selected_findings = findings[offset : offset + limit]
        page = [
            _finding_projection(item, include_excerpts=include_excerpts)
            for item in selected_findings
        ]
        next_offset = offset + len(page) if offset + len(page) < len(findings) else None

    severity_counts = Counter(item["severity"] for item in summary_findings)
    category_counts = Counter(item["category"] for item in summary_findings)
    if group_by == "token":
        total_groups = len(groups)
        unique_token_count = len(
            {group["_token_key"] for group in groups if group["_token_key"] is not None}
        )
        preview_omitted = sum(max(0, len(group["_findings"]) - 1) for group in groups)
    else:
        total_groups = len(findings)
        unique_token_count = len(
            {
                _term_normal(token)
                for item in findings
                if (token := _finding_token(item)) is not None and _term_normal(token)
            }
        )
        preview_omitted = 0
    result = {
        "contract": QA_CONTRACT,
        "ruleset": QA_RULESET,
        "project": document_review._safe_identity(project),
        "source_plan": {"id": workspace.record.id, "version": workspace.active_version},
        "capabilities": {
            "regex_engine": "regex",
            "finding_caps_by_severity": dict(MAX_QA_FINDINGS_BY_SEVERITY),
            "semantic_review": False,
            "spellcheck": {
                "available": target_dictionary.available,
                "requested_language": getattr(project, "target_lang", ""),
                "dictionary": target_dictionary.language,
                "backend": "enchant" if target_dictionary.available else None,
            },
            "source_language_dictionary": {
                "available": source_dictionary.available,
                "requested_language": getattr(project, "source_lang", ""),
                "dictionary": source_dictionary.language,
            },
        },
        "filters": {
            "block_ids": list(block_ids) if block_ids is not None else [],
            "categories": list(canonical_categories),
            "severities": list(canonical_severities),
            "include_spelling": include_spelling,
            "group_by": group_by,
            "include_excerpts": include_excerpts,
            "group_id": group_id,
        },
        "summary": {
            "total": len(summary_findings),
            "total_groups": total_groups,
            "grouping": group_by,
            "unique_token_count": unique_token_count,
            "preview_omitted_occurrences": preview_omitted,
            "omissions_scope": "scan",
            "alignment_coverage": (
                "checked"
                if alignment_available
                else "unavailable"
                if include_alignment
                else "disabled"
            ),
            "by_severity": {key: severity_counts[key] for key in _SEVERITIES},
            "by_category": {key: category_counts[key] for key in _CATEGORIES},
            "truncated": bool(omitted_by_severity),
            "omitted_total": sum(omitted_by_severity.values()),
            "omitted_by_severity": {
                key: omitted_by_severity[key] for key in _SEVERITIES
            },
            "omitted_by_category": {
                key: omitted_by_category[key] for key in _CATEGORIES
            },
            "blocks_scanned": len(blocks),
            "blocks_with_active_targets": sum(
                1 for block in blocks if block.id in targets
            ),
        },
        "items": page,
        "pagination": document_review._pagination(
            offset=offset,
            limit=limit,
            total=pagination_total,
            next_offset=next_offset,
            fingerprint=fingerprint,
        ),
        "disclaimer": (
            "QA findings are review evidence, not automatic target invalidation or a human review decision."
            + (
                " Some lower-priority findings were omitted at the documented safety cap; narrow the category or block filters to inspect them."
                if omitted_by_severity
                else ""
            )
            + " Only deterministic checks are available; translation meaning has not been verified."
        ),
    }
    if group_by == "token":
        result["groups"] = group_page
    if group_id is not None:
        result["occurrences"] = occurrence_page
    return document_review._bounded_result(result)


__all__ = [
    "MAX_QA_FINDINGS",
    "MAX_QA_LIMIT",
    "QA_CONTRACT",
    "QA_RULESET",
    "QaDiagnosticsError",
    "qa_diagnostics",
]
