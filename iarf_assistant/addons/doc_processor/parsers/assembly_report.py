"""Record what page assembly did, and check it against the submitted pages.

Assembly joins accepted page fragments into one document. A batch can pass
validation while the join still changes structure, so assembly records its
decisions and completion compares the canonical document with the submitted
pages. The report holds counts, tag names and physical page numbers only, never
extracted text, so it can live in document settings under any content policy.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, cast

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString

from carlina.core.language import seam_whitespace_is_spurious

ASSEMBLY_REPORT_VERSION = 1
ASSEMBLY_REPORT_SETTING = "assembly_report"
MAX_REPORTED_BOUNDARIES = 50

# Inline and media elements that assembly never needs to remove. A drop in
# their count between the submitted pages and the canonical document means
# markup was lost (for example an element outside the document vocabulary).
# ``rp`` is absent: canonical ruby drops its bracket fallbacks by design.
AUDITED_ELEMENTS = frozenset(
    {
        "a",
        "abbr",
        "b",
        "cite",
        "code",
        "del",
        "em",
        "figure",
        "i",
        "img",
        "ins",
        "kbd",
        "mark",
        "q",
        "rt",
        "ruby",
        "s",
        "samp",
        "small",
        "strong",
        "sub",
        "sup",
        "time",
        "u",
        "var",
    }
)

# Note markers and labels are renumbered or generated during assembly, so
# their letters (``a``, ``注一``, ``n1``) are not compared.
_NOTE_LABEL_CLASSES = frozenset(
    {
        "footnote-backref",
        "footnote-marker",
        "footnote-ref",
        "footnote-unresolved",
        "footnote_marker",
        "inline-footnote",
    }
)

_ACTIVE: ContextVar[AssemblyReport | None] = ContextVar(
    "doc_processor_assembly_report", default=None
)


@dataclass
class AssemblyReport:
    """Mutable facts gathered while one document is assembled."""

    joins: Counter[str] = field(default_factory=Counter)
    table_rows: Counter[str] = field(default_factory=Counter)
    ambiguous_table_boundaries: list[tuple[int, int]] = field(default_factory=list)
    input_elements: Counter[str] = field(default_factory=Counter)
    input_letters: Counter[str] = field(default_factory=Counter)
    boundary: tuple[int, int] | None = None

    def observe_page(
        self,
        html: str,
        *,
        remove_source_toc: Callable[[BeautifulSoup], object] | None = None,
    ) -> None:
        """Count the audited elements and letters of one submitted page.

        ``remove_source_toc`` is the same removal assembly applies to an omitted
        printed table of contents, so omitted contents are not counted as lost.
        """

        soup = BeautifulSoup(html, "html.parser")
        if remove_source_toc is not None:
            remove_source_toc(soup)
        self.input_elements.update(_element_counts(soup))
        self.input_letters.update(_letter_counts(soup))

    def to_inputs(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot to finish after canonicalisation."""

        return {
            "version": ASSEMBLY_REPORT_VERSION,
            "joins": dict(self.joins),
            "table_rows": dict(self.table_rows),
            "ambiguous_table_boundaries": [
                list(pair)
                for pair in self.ambiguous_table_boundaries[:MAX_REPORTED_BOUNDARIES]
            ],
            "input_elements": dict(self.input_elements),
            "input_letters": dict(self.input_letters),
        }


@contextmanager
def collecting(report: AssemblyReport | None) -> Generator[None]:
    """Make ``report`` the destination for join and table decisions."""

    token = _ACTIVE.set(report)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def set_boundary(previous_page: int, page: int) -> None:
    """Attribute later decisions to one physical page boundary (1-based)."""

    report = _ACTIVE.get()
    if report is not None:
        report.boundary = (previous_page, page)


def record_join(kind: str) -> None:
    """Count one text join by the separator it used."""

    report = _ACTIVE.get()
    if report is not None:
        report.joins[kind] += 1


def record_table_row(kind: str) -> None:
    """Count one split table row by how it was resolved."""

    report = _ACTIVE.get()
    if report is None:
        return
    report.table_rows[kind] += 1
    if (
        kind == "ambiguous"
        and report.boundary is not None
        and report.boundary not in report.ambiguous_table_boundaries
    ):
        report.ambiguous_table_boundaries.append(report.boundary)


def _element_counts(soup: BeautifulSoup | Tag) -> Counter[str]:
    return Counter(
        str(tag.name).casefold()
        for tag in soup.find_all(True)
        if str(tag.name).casefold() in AUDITED_ELEMENTS
    )


def _letter_counts(soup: BeautifulSoup | Tag) -> Counter[str]:
    # Letters only: note labels, digits, punctuation and dehyphenation may
    # legitimately change during assembly, while words must not.
    letters: Counter[str] = Counter()
    for node in soup.find_all(string=True):
        if _inside_note_label(node):
            continue
        for character in unicodedata.normalize("NFC", str(node)):
            if unicodedata.category(character).startswith("L"):
                letters[character] += 1
    return letters


def _inside_note_label(node: NavigableString) -> bool:
    for parent in node.parents:
        if parent.name == "sup":
            return True
        classes = parent.get("class")
        names = classes if isinstance(classes, list) else str(classes or "").split()
        if _NOTE_LABEL_CLASSES.intersection(names):
            return True
    return False


def _spurious_seam_spaces(soup: BeautifulSoup) -> int:
    count = 0
    for marker in soup.find_all("span", class_="pagebreak"):
        before = marker.previous_sibling
        after = marker.next_sibling
        if (
            isinstance(before, NavigableString)
            and isinstance(after, NavigableString)
            and seam_whitespace_is_spurious(str(before), str(after))
        ):
            count += 1
    return count


def finish_assembly_report(
    inputs: object, canonical_html: str
) -> dict[str, Any] | None:
    """Compare the canonical document with the submitted pages.

    Returns ``None`` when ``inputs`` is not a current snapshot, so retained
    executions and non-PDF documents keep their settings unchanged.
    """

    snapshot = _mapping(inputs)
    if snapshot is None or snapshot.get("version") != ASSEMBLY_REPORT_VERSION:
        return None
    soup = BeautifulSoup(canonical_html or "", "html.parser")
    output_elements = _element_counts(soup)
    output_letters = _letter_counts(soup)
    input_elements = Counter(dict(_counter_items(snapshot.get("input_elements"))))
    input_letters = Counter(dict(_counter_items(snapshot.get("input_letters"))))

    lost_elements = {
        name: count - output_elements.get(name, 0)
        for name, count in sorted(input_elements.items())
        if count > output_elements.get(name, 0)
    }
    missing_letters = sum((input_letters - output_letters).values())
    added_letters = sum((output_letters - input_letters).values())
    spurious_spaces = _spurious_seam_spaces(soup)
    table_rows = dict(_counter_items(snapshot.get("table_rows")))
    ambiguous = _boundary_pairs(snapshot.get("ambiguous_table_boundaries"))

    advisories: list[str] = []
    if lost_elements:
        advisories.append("inline_elements_lost")
    if table_rows.get("ambiguous"):
        advisories.append("table_row_continuation_ambiguous")
    if missing_letters or added_letters:
        advisories.append("text_changed_during_assembly")
    if spurious_spaces:
        advisories.append("space_inside_unspaced_text_at_page_join")
    return {
        "version": ASSEMBLY_REPORT_VERSION,
        "status": "advisories" if advisories else "consistent",
        "advisories": advisories,
        "joins": dict(_counter_items(snapshot.get("joins"))),
        "table_rows": table_rows,
        "ambiguous_table_boundaries": ambiguous,
        "lost_elements": lost_elements,
        "letters": {"missing": missing_letters, "added": added_letters},
        "spurious_seam_spaces": spurious_spaces,
    }


def _mapping(value: object) -> Mapping[object, object] | None:
    if not isinstance(value, Mapping):
        return None
    return cast("Mapping[object, object]", value)


def _counter_items(value: object) -> list[tuple[str, int]]:
    mapping = _mapping(value)
    if mapping is None:
        return []
    return [
        (key, count)
        for key, count in mapping.items()
        if isinstance(key, str) and type(count) is int and count >= 0
    ]


def _boundary_pairs(value: object) -> list[list[int]]:
    if not isinstance(value, list):
        return []
    pairs: list[list[int]] = []
    for item in cast("list[object]", value)[:MAX_REPORTED_BOUNDARIES]:
        if not isinstance(item, (list, tuple)):
            continue
        pair = cast("Sequence[object]", item)
        if len(pair) != 2:
            continue
        first, second = pair
        if type(first) is int and type(second) is int and first > 0 and second > 0:
            pairs.append([first, second])
    return pairs


def assembly_check_projection(report: object) -> dict[str, Any]:
    """Return the compact client view: status always, details only when set."""

    snapshot = _mapping(report)
    if snapshot is None:
        return {"status": "unavailable"}
    status = snapshot.get("status")
    projection: dict[str, Any] = {
        "status": status if status in {"consistent", "advisories"} else "unavailable"
    }
    advisories = snapshot.get("advisories")
    if isinstance(advisories, list) and advisories:
        projection["advisories"] = [
            str(item) for item in cast("list[object]", advisories)[:8]
        ]
    for key in ("lost_elements", "ambiguous_table_boundaries"):
        value = snapshot.get(key)
        if value:
            projection[key] = value
    letters = _mapping(snapshot.get("letters"))
    if letters is not None and any(letters.values()):
        projection["letters"] = dict(letters)
    spaces = snapshot.get("spurious_seam_spaces")
    if type(spaces) is int and spaces:
        projection["spurious_seam_spaces"] = spaces
    if projection.get("advisories"):
        projection["review"] = (
            "Check the listed page boundaries with carlina_document_review_boundaries "
            "and the page images before export."
        )
    return projection
