"""Deterministic safety checks for normalized PDF page extraction HTML.

The extraction model is intentionally allowed to make small OCR and markup
corrections.  It is not allowed to turn a page into an empty, malformed, or
substantially reordered fragment, particularly during the optional evaluator
pass.  These checks operate on the normalized page contract so they can be
reused by later document-level validation.
"""

from __future__ import annotations

import html as html_lib
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import pairwise

from bs4 import BeautifulSoup, Tag

from carlina.core.semantic_diagrams import diagnose_mermaid_diagrams
from carlina.core.semantic_html import SAFE_INTEGER_RE

_SEMANTIC_TAGS = frozenset(
    {
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "p",
        "li",
        "blockquote",
        "figure",
        "table",
        "pre",
        "code",
        "div",
        "nav",
        "section",
        "span",
        "article",
        "img",
    }
)
_TERMINAL_FAILURE_REASON_TOKENS = frozenset(
    {
        "max_tokens",
        "max_token",
        "token_limit",
        "length",
        "recitation",
        "safety",
        "prohibited_content",
        "spii",
        "blocklist",
        "content_filter",
        "contentfilter",
        "blocked",
    }
)
_BLOCKED_CONTENT_PATTERN = re.compile(
    r"\b(?:content[_ -]?filter|safety[_ -]?(?:filter|block)|blocked by (?:the )?(?:safety|content)|"
    r"max[_ -]?tokens|maximum token(?:s| limit)?|finish[_ -]?reason\s*[:=]\s*length)\b",
    re.IGNORECASE,
)
_PROTECTED_EVALUATOR_STRUCTURES = (
    ("footnote body", ".footnote"),
    ("footnote marker", ".footnote_marker"),
    ("footnote reference", "a.footnote-ref"),
    ("figure", "figure"),
    ("image", "img"),
    ("figure caption", "figcaption"),
    ("table", "table"),
    ("table row", "tr"),
    ("list item", "li"),
    ("blockquote", "blockquote"),
    ("source TOC entry", "nav.source-toc .toc-entry"),
)
_TEMPLATE_TOKEN_RE = re.compile(r"\{\{[^{}]{1,256}\}\}")
_FOOTNOTE_MARKER_CLASSES = frozenset({"footnote_marker", "inline-footnote"})
EXTRACTION_NOTE_INPUT_POLICY_SETTING = "mcp_extraction_input_policy"
EXTRACTION_NOTE_INPUT_POLICY_VERSION = "footnotes-v1"
EXTRACTION_NOTE_INPUT_INSTRUCTIONS = (
    'For extraction input, mark a note reference only with a '
    '<span class="footnote_marker"> inside main_content or footnotes, and place a '
    '<div class="footnote"> body inside footnotes. Do not emit canonical output '
    'references such as <a class="footnote-ref">, role="doc-noteref" or '
    'role="doc-endnote", or epub:type="noteref" or epub:type="endnote". A plain '
    'local link to a note body belongs inside the marker span; its target may be on '
    'another page. Preserve unknown targets and continued note prose without '
    'inventing relationships. If preflight rejects a page, call carlina_work_get '
    'under the existing lease to reread the contract before resubmitting it.'
)
_GROUNDING_MIN_SOURCE_TOKENS = 40
_GROUNDING_MIN_BAG_OVERLAP = 0.35
_GROUNDING_MIN_SEQUENCE_OVERLAP = 0.15
_GROUNDING_MIN_SOURCE_COVERAGE = 0.72
_GROUNDING_MIN_OUTPUT_SOURCE_RATIO = 0.80
_GROUNDING_TAIL_TOKENS = 48
_GROUNDING_MIN_TAIL_SEQUENCE_OVERLAP = 0.10


@dataclass(frozen=True)
class SemanticValidationResult:
    valid: bool
    diagnostics: tuple[str, ...] = ()


def extraction_note_input_contract() -> dict[str, str]:
    """Return the compact, versioned extraction note-input contract."""

    return {
        "version": EXTRACTION_NOTE_INPUT_POLICY_VERSION,
        "instructions": EXTRACTION_NOTE_INPUT_INSTRUCTIONS,
    }


def extraction_note_input_enabled(settings: Mapping[str, object] | None = None) -> bool:
    """Gate the new rule for retained passive executions without its marker."""

    values = settings or {}
    if values.get("mcp_execution_mode") == "passive":
        return (
            values.get(EXTRACTION_NOTE_INPUT_POLICY_SETTING)
            == EXTRACTION_NOTE_INPUT_POLICY_VERSION
        )
    return True


def validate_extraction_note_input(content: str) -> SemanticValidationResult:
    """Validate note markup at model input, before extraction normalization.

    Diagnostics describe only the rejected shape. They never include document
    excerpts, labels, identifiers, or other model output.
    """

    if not isinstance(content, str):
        return SemanticValidationResult(
            False, ("footnote input shape: page HTML must be text",)
        )
    soup = BeautifulSoup(content, "html.parser")

    def class_tokens(tag: Tag) -> set[str]:
        raw_classes = tag.get("class") or []
        if isinstance(raw_classes, str):
            raw_classes = raw_classes.split()
        return {str(value).casefold() for value in raw_classes}

    def extraction_root(tag: Tag) -> str | None:
        root = tag.find_parent(["main_content", "footnotes"])
        return str(root.name).casefold() if root is not None else None

    def containing_marker(tag: Tag) -> Tag | None:
        parent = tag.parent
        while isinstance(parent, Tag):
            if parent.name == "span" and "footnote_marker" in class_tokens(parent):
                return parent
            parent = parent.parent
        return None

    tags = list(soup.find_all(True))
    note_bodies: dict[str, list[Tag]] = {}
    markers: list[Tag] = []
    for tag in tags:
        classes = class_tokens(tag)
        root = extraction_root(tag)
        if "footnote_marker" in classes:
            if tag.name != "span" or root not in {"main_content", "footnotes"}:
                return SemanticValidationResult(
                    False,
                    (
                        (
                            "footnote input shape: footnote_marker must be a span "
                            "inside main_content or footnotes"
                        ),
                    ),
                )
            markers.append(tag)
        if "footnote" in classes:
            if tag.name != "div" or root != "footnotes":
                return SemanticValidationResult(
                    False,
                    (
                        (
                            "footnote input shape: footnote body must be a div "
                            "inside footnotes"
                        ),
                    ),
                )
            identifier = str(tag.get("id") or "").strip()
            if identifier:
                note_bodies.setdefault(identifier, []).append(tag)

        if tag.name == "a" and "footnote-ref" in classes:
            return SemanticValidationResult(
                False,
                (
                    (
                        "footnote input shape: a.footnote-ref is output-only; "
                        "use span.footnote_marker"
                    ),
                ),
            )

        role_tokens = {
            token.casefold() for token in str(tag.get("role") or "").split()
        }
        epub_type_tokens = {
            token.casefold()
            for name, value in tag.attrs.items()
            if str(name).casefold() == "epub:type"
            for token in (
                value if isinstance(value, (list, tuple)) else str(value or "").split()
            )
        }
        if role_tokens.intersection({"doc-noteref", "doc-endnote"}) or epub_type_tokens.intersection(
            {"noteref", "endnote"}
        ):
            return SemanticValidationResult(
                False,
                (
                    (
                        "footnote input shape: doc-noteref/doc-endnote and "
                        "epub noteref/endnote markup are output-only; use "
                        "span.footnote_marker"
                    ),
                ),
            )

    id_targets: dict[str, list[Tag]] = {}
    for tag in tags:
        identifier = str(tag.get("id") or "").strip()
        if identifier:
            id_targets.setdefault(identifier, []).append(tag)

    for marker in markers:
        anchors = marker.find_all("a")
        if len(anchors) > 1:
            return SemanticValidationResult(
                False,
                (
                    (
                        "footnote input shape: a marker span may contain at most one "
                        "plain local note-body link"
                    ),
                ),
            )
        for anchor in anchors:
            href = str(anchor.get("href") or "").strip()
            if set(map(str, anchor.attrs)) != {"href"} or not href.startswith("#") or len(href) == 1:
                return SemanticValidationResult(
                    False,
                    (
                        (
                            "footnote input shape: marker links must be plain local "
                            "a[href='#body-id'] elements"
                        ),
                    ),
                )
            targets = id_targets.get(href[1:], [])
            if targets and (
                len(targets) != 1
                or targets[0].name != "div"
                or "footnote" not in class_tokens(targets[0])
                or extraction_root(targets[0]) != "footnotes"
            ):
                return SemanticValidationResult(
                    False,
                    (
                        (
                            "footnote input shape: a local marker link must target one "
                            "div.footnote body"
                        ),
                    ),
                )

    for anchor in (tag for tag in tags if tag.name == "a"):
        if containing_marker(anchor) is not None:
            continue
        href = str(anchor.get("href") or "").strip()
        if href.startswith("#") and href[1:] in note_bodies:
            return SemanticValidationResult(
                False,
                (
                    (
                        "footnote input shape: direct links to a known note body must "
                        "be inside span.footnote_marker"
                    ),
                ),
            )

    return SemanticValidationResult(True)


def validate_model_response_content(
    content: str, finish_reason: object = ""
) -> SemanticValidationResult:
    """Reject empty, blocked, and explicitly truncated model responses."""
    normalized_finish_reason = _normalize_finish_reason(finish_reason)
    if any(
        token in normalized_finish_reason for token in _TERMINAL_FAILURE_REASON_TOKENS
    ):
        return SemanticValidationResult(
            False, (f"terminal failure finish_reason={normalized_finish_reason}",)
        )

    text = str(content or "").strip()
    if not text:
        return SemanticValidationResult(False, ("empty model response",))
    if _BLOCKED_CONTENT_PATTERN.search(text):
        return SemanticValidationResult(
            False, ("blocked or MAX_TOKENS-like model response",)
        )
    return SemanticValidationResult(True)


def validate_extraction_grounding(
    source_text: str | None,
    normalized_html: str,
    *,
    allow_expected_source_omissions: bool = False,
    allow_empty_output: bool = False,
) -> SemanticValidationResult:
    """Reject only catastrophic drift from a usable PDF text layer.

    Vision extraction remains authoritative for sparse/image-only pages.  When
    the PDF also exposes enough text, however, a response whose vocabulary and
    ordered spans both have negligible overlap is almost certainly content from
    another page or task.  The deliberately conservative paired thresholds
    tolerate dehyphenation, whitespace repair, running-matter removal, and
    ordinary OCR corrections.
    """

    source_tokens = _grounding_tokens(source_text)
    if len(source_tokens) < _GROUNDING_MIN_SOURCE_TOKENS:
        return SemanticValidationResult(True)

    rendered_text = BeautifulSoup(normalized_html or "", "html.parser").get_text(" ")
    output_tokens = _grounding_tokens(rendered_text)
    if not output_tokens:
        if allow_empty_output:
            return SemanticValidationResult(True)
        return SemanticValidationResult(
            False,
            ("source grounding mismatch (output contains no comparable text)",),
        )

    denominator = min(len(source_tokens), len(output_tokens))
    bag_overlap_count = sum((Counter(source_tokens) & Counter(output_tokens)).values())
    bag_overlap = bag_overlap_count / denominator
    source_sequences = Counter(zip(source_tokens, source_tokens[1:], source_tokens[2:]))
    output_sequences = Counter(zip(output_tokens, output_tokens[1:], output_tokens[2:]))
    sequence_denominator = min(
        sum(source_sequences.values()), sum(output_sequences.values())
    )
    sequence_overlap = (
        sum((source_sequences & output_sequences).values()) / sequence_denominator
        if sequence_denominator
        else 0.0
    )

    if (
        bag_overlap < _GROUNDING_MIN_BAG_OVERLAP
        and sequence_overlap < _GROUNDING_MIN_SEQUENCE_OVERLAP
    ):
        return SemanticValidationResult(
            False,
            (
                "source grounding mismatch "
                f"(bag={bag_overlap:.3f}, sequence={sequence_overlap:.3f})",
            ),
        )

    # The overlap checks above deliberately use the shorter side so that OCR
    # corrections and running-matter removal do not look like drift.  That also
    # means a perfectly grounded *prefix* can otherwise pass after the model
    # silently drops the final third of a page.  Detect only that conservative
    # failure shape: a materially short response, low source-side coverage, and
    # no meaningful agreement at the source tail.  Intentional footnote
    # omission can disable this completeness check while retaining drift
    # detection.
    if not allow_expected_source_omissions:
        source_coverage = bag_overlap_count / len(source_tokens)
        output_source_ratio = len(output_tokens) / len(source_tokens)
        source_tail = source_tokens[-_GROUNDING_TAIL_TOKENS:]
        output_tail = output_tokens[-_GROUNDING_TAIL_TOKENS:]
        source_tail_sequences = Counter(
            zip(source_tail, source_tail[1:], source_tail[2:])
        )
        output_tail_sequences = Counter(
            zip(output_tail, output_tail[1:], output_tail[2:])
        )
        tail_denominator = min(
            sum(source_tail_sequences.values()),
            sum(output_tail_sequences.values()),
        )
        tail_sequence_overlap = (
            sum((source_tail_sequences & output_tail_sequences).values())
            / tail_denominator
            if tail_denominator
            else 0.0
        )
        if (
            source_coverage < _GROUNDING_MIN_SOURCE_COVERAGE
            and output_source_ratio < _GROUNDING_MIN_OUTPUT_SOURCE_RATIO
            and tail_sequence_overlap < _GROUNDING_MIN_TAIL_SEQUENCE_OVERLAP
        ):
            return SemanticValidationResult(
                False,
                (
                    "source extraction appears incomplete "
                    f"(coverage={source_coverage:.3f}, "
                    f"length={output_source_ratio:.3f}, "
                    f"tail={tail_sequence_overlap:.3f})",
                ),
            )
    return SemanticValidationResult(True)


def _grounding_tokens(value: object) -> list[str]:
    text = html_lib.unescape(str(value or ""))
    text = unicodedata.normalize("NFKC", text).replace("\u00ad", "")
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    return [
        token
        for token in re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE)
        if len(token) >= 3
    ]


def _normalize_finish_reason(finish_reason: object) -> str:
    """Normalize SDK enums, provider prefixes, and OpenAI-compatible strings."""
    rendered = str(finish_reason or "").strip().casefold()
    return re.sub(r"[^a-z0-9]+", "_", rendered).strip("_")


def _is_footnote_marker(tag: Tag) -> bool:
    """Return whether ``tag`` is a marker the page merger understands.

    The repeated-ID exception is intentionally narrower than the downstream
    normalizer's broad HTML handling: it must describe exactly the span marker
    vocabulary consumed by ``normalize_footnote_links``.  A class on another
    element is not enough to make a duplicate identifier safe.
    """
    if tag.name != "span":
        return False
    classes = tag.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    return bool(_FOOTNOTE_MARKER_CLASSES.intersection(map(str, classes)))


def validate_normalized_page_output(html: str) -> SemanticValidationResult:
    """Validate one normalized ``main_content``/``footnotes`` page fragment."""
    if not isinstance(html, str) or not html.strip():
        return SemanticValidationResult(False, ("empty normalized output",))

    soup = BeautifulSoup(html, "html.parser")
    roots = [node for node in soup.contents if isinstance(node, Tag)]
    root_names = [node.name for node in roots]
    if root_names != ["main_content", "footnotes"]:
        return SemanticValidationResult(
            False, ("required roots must be one ordered main_content/footnotes pair",)
        )

    main, footnotes = roots
    if any(str(tag.name).rsplit(":", 1)[-1] == "svg" for tag in soup.find_all(True)) or any(
        str(image.get("src") or "").strip().casefold().startswith("data:image/svg+xml")
        for image in soup.find_all("img")
    ):
        return SemanticValidationResult(
            False,
            ("SVG extraction input is not retained by the semantic contract; use a supported Mermaid figure or the source image bbox.",),
        )
    id_occurrences: dict[str, list[Tag]] = {}
    for tag in soup.find_all(True):
        identifier = str(tag.get("id") or "").strip()
        if identifier:
            id_occurrences.setdefault(identifier, []).append(tag)

        href = str(tag.get("href") or "").strip()
        if href.lower().startswith(("javascript:", "vbscript:")):
            return SemanticValidationResult(False, ("unsafe link scheme",))

    for identifier, occurrences in id_occurrences.items():
        if len(occurrences) > 1 and not all(
            _is_footnote_marker(tag) for tag in occurrences
        ):
            return SemanticValidationResult(False, (f"duplicate id={identifier!r}",))

    for reference in soup.find_all("a", class_="footnote-ref"):
        href = str(reference.get("href") or "").strip()
        if not href.startswith("#") or len(href) == 1:
            return SemanticValidationResult(
                False, ("footnote reference lacks a local target",)
            )
        target = soup.find(id=href[1:])
        if target is None or "footnote" not in (target.get("class") or []):
            return SemanticValidationResult(
                False, ("footnote reference target is not a local footnote",)
            )

    source_toc_diagnostics = diagnose_source_toc_blocks(soup)
    if source_toc_diagnostics:
        return SemanticValidationResult(False, source_toc_diagnostics)

    diagram_diagnostics = diagnose_mermaid_diagrams(soup)
    if diagram_diagnostics:
        return SemanticValidationResult(False, diagram_diagnostics)

    list_diagnostics = diagnose_list_structure(soup)
    if list_diagnostics:
        return SemanticValidationResult(False, list_diagnostics)

    for root in (main, footnotes):
        loose_text = "".join(
            str(node).strip() for node in root.contents if not isinstance(node, Tag)
        ).strip()
        if loose_text:
            return SemanticValidationResult(
                False, (f"unstructured text directly inside {root.name}",)
            )

    semantic_nodes = list(_semantic_nodes((main, footnotes)))
    if not semantic_nodes:
        # An explicitly empty pair is legitimate for an omitted source TOC or a
        # deliberately blank page.  Empty *model responses* are rejected before
        # normalization by ``validate_model_response_content``.
        return SemanticValidationResult(True)
    return SemanticValidationResult(True)


def diagnose_list_structure(soup: BeautifulSoup) -> tuple[str, ...]:
    """Reject malformed list evidence; never infer or renumber source items."""
    codes: set[str] = set()
    for node in soup.find_all(["ol", "ul", "li"]):
        if node.name in {"ol", "ul"}:
            if any(
                child.name != "li" if isinstance(child, Tag) else bool(str(child).strip())
                for child in node.children
            ):
                codes.add("list_invalid_child")
            if node.name == "ol":
                if node.has_attr("start") and not SAFE_INTEGER_RE.fullmatch(str(node["start"])):
                    codes.add("list_invalid_start")
                if node.has_attr("type") and node["type"] not in {"1", "a", "A", "i", "I"}:
                    codes.add("list_invalid_type")
        elif not isinstance(node.parent, Tag) or node.parent.name not in {"ol", "ul"}:
            codes.add("list_item_without_list")
        elif node.has_attr("value") and (
            node.parent.name != "ol" or not SAFE_INTEGER_RE.fullmatch(str(node["value"]))
        ):
            codes.add("list_invalid_value")
    return tuple(sorted(codes))


def diagnose_source_toc_blocks(soup: BeautifulSoup) -> tuple[str, ...]:
    """Validate the inert source-TOC shape without inspecting source text."""

    diagnostics: list[str] = []

    def record(code: str) -> None:
        if code not in diagnostics:
            diagnostics.append(code)

    def class_tokens(tag: Tag) -> set[str]:
        value = tag.get("class") or []
        if isinstance(value, str):
            return set(value.split())
        return {str(item) for item in value}

    for nav in soup.find_all("nav", class_="source-toc"):
        if nav.find("a") is not None:
            record("source_toc_contains_link")
        if nav.find(["ol", "ul"]) is not None:
            record("source_toc_contains_list")

        direct_entries = [
            child
            for child in nav.find_all(recursive=False)
            if isinstance(child, Tag)
            and child.name == "div"
            and "toc-entry" in class_tokens(child)
        ]
        if not direct_entries:
            record("source_toc_missing_entries")

        direct_tags = [child for child in nav.children if isinstance(child, Tag)]
        direct_titles = [
            child
            for child in direct_tags
            if child.name in {"h1", "h2", "h3", "h4", "h5", "h6"}
            and "source-toc-title" in class_tokens(child)
        ]
        if len(direct_titles) > 1 or any(
            not title.get_text(" ", strip=True) for title in direct_titles
        ):
            record("source_toc_invalid_title")
        if direct_titles and direct_tags[0] is not direct_titles[0]:
            record("source_toc_invalid_title_position")
        if len(direct_tags) != len(direct_entries) + len(direct_titles):
            record("source_toc_unexpected_child")

        for entry in direct_entries:
            classes = class_tokens(entry)
            depth_classes = {
                class_name
                for class_name in classes
                if re.fullmatch(r"toc-depth-[1-6]", str(class_name))
            }
            if len(depth_classes) != 1:
                record("source_toc_invalid_depth")

            titles = entry.find_all("span", class_="toc-entry-title", recursive=False)
            if len(titles) != 1 or not titles[0].get_text(" ", strip=True):
                record("source_toc_missing_title")

            labels = entry.find_all("span", class_="toc-page-label", recursive=False)
            if len(labels) > 1 or any(
                not label.get_text(" ", strip=True) for label in labels
            ):
                record("source_toc_invalid_page_label")

            allowed_children = set(titles) | set(labels)
            if any(
                isinstance(child, Tag) and child not in allowed_children
                for child in entry.children
            ):
                record("source_toc_unexpected_entry_child")

    return tuple(diagnostics)


def validate_evaluator_replacement(
    original: str, candidate: str
) -> SemanticValidationResult:
    """Validate a candidate evaluator rewrite against a valid original page."""
    candidate_validation = validate_normalized_page_output(candidate)
    if not candidate_validation.valid:
        return candidate_validation

    original_soup = BeautifulSoup(original, "html.parser")
    candidate_soup = BeautifulSoup(candidate, "html.parser")
    for label, selector in _PROTECTED_EVALUATOR_STRUCTURES:
        original_count = len(original_soup.select(selector))
        candidate_count = len(candidate_soup.select(selector))
        if candidate_count < original_count:
            return SemanticValidationResult(
                False, (f"evaluator removed protected {label} structure",)
            )

    original_template_tokens = _TEMPLATE_TOKEN_RE.findall(original)
    candidate_template_tokens = _TEMPLATE_TOKEN_RE.findall(candidate)
    if any(
        candidate_template_tokens.count(token) < original_template_tokens.count(token)
        for token in set(original_template_tokens)
    ):
        return SemanticValidationResult(
            False, ("evaluator removed protected template token",)
        )

    original_notes = original_soup.select(".footnote")
    candidate_notes = candidate_soup.select(".footnote")
    candidate_notes_by_id = {
        str(note.get("id") or "").strip(): note
        for note in candidate_notes
        if str(note.get("id") or "").strip()
    }
    for index, original_note in enumerate(original_notes):
        identifier = str(original_note.get("id") or "").strip()
        candidate_note = candidate_notes_by_id.get(identifier) if identifier else None
        if candidate_note is None and index < len(candidate_notes):
            candidate_note = candidate_notes[index]
        if candidate_note is None:
            return SemanticValidationResult(False, ("footnote topology changed",))
        original_note_text = " ".join(original_note.get_text(" ", strip=True).split())
        candidate_note_text = " ".join(candidate_note.get_text(" ", strip=True).split())
        if not candidate_note_text:
            return SemanticValidationResult(False, ("evaluator emptied footnote body",))
        if len(original_note_text) >= 24 and len(candidate_note_text) < max(
            8, int(len(original_note_text) * 0.65)
        ):
            return SemanticValidationResult(False, ("catastrophic footnote-text loss",))

    for selector in (".footnote[id]", ".footnote_marker[id]", "a.footnote-ref[id]"):
        original_ids = {
            str(tag.get("id") or "").strip()
            for tag in original_soup.select(selector)
            if str(tag.get("id") or "").strip()
        }
        candidate_ids = {
            str(tag.get("id") or "").strip()
            for tag in candidate_soup.select(selector)
            if str(tag.get("id") or "").strip()
        }
        if not original_ids.issubset(candidate_ids):
            return SemanticValidationResult(False, ("footnote topology changed",))

    original_text = _visible_text(original)
    candidate_text = _visible_text(candidate)
    if len(original_text) >= 240 and len(candidate_text) < max(
        80, int(len(original_text) * 0.65)
    ):
        return SemanticValidationResult(False, ("catastrophic visible-text loss",))

    original_nodes = list(_semantic_nodes_from_html(original))
    candidate_nodes = list(_semantic_nodes_from_html(candidate))
    if len(original_nodes) >= 4 and len(candidate_nodes) < max(
        2, (len(original_nodes) + 1) // 2
    ):
        return SemanticValidationResult(
            False, ("catastrophic semantic-structure loss",)
        )

    order_diagnostic = _detect_order_scramble(original_nodes, candidate_text)
    if order_diagnostic:
        return SemanticValidationResult(False, (order_diagnostic,))
    return SemanticValidationResult(True)


def _semantic_nodes(roots: Iterable[Tag]) -> Iterable[Tag]:
    for root in roots:
        for tag in root.find_all(_SEMANTIC_TAGS):
            if (
                tag.name == "div"
                and not tag.get_text(" ", strip=True)
                and not tag.find("img")
            ):
                continue
            yield tag


def _semantic_nodes_from_html(html: str) -> Iterable[Tag]:
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main_content")
    footnotes = soup.find("footnotes")
    return _semantic_nodes(root for root in (main, footnotes) if root is not None)


def _visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return " ".join(soup.get_text(" ", strip=True).split())


def _detect_order_scramble(
    original_nodes: list[Tag], candidate_text: str
) -> str | None:
    """Detect clear inversions only; tolerate normal OCR corrections and re-tagging."""
    candidate_folded = " ".join(candidate_text.casefold().split())
    positions: list[int] = []
    for node in original_nodes:
        anchor = " ".join(node.get_text(" ", strip=True).casefold().split())
        if len(anchor) < 16:
            continue
        position = candidate_folded.find(anchor[:80])
        if position >= 0:
            positions.append(position)

    if len(positions) < 4:
        return None
    inversions = sum(later < earlier for earlier, later in pairwise(positions))
    if inversions >= 2 and inversions * 2 >= len(positions) - 1:
        return "catastrophic reading-order scramble"
    return None
