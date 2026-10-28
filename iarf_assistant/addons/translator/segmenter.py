"""Loss-conscious DOM segmentation for Translator source snapshots.

The segmenter deliberately works on a *copy* of a source snapshot. It must
never discard comments or executable/non-prose nodes merely because they are
not translation candidates: those nodes remain in the skeleton verbatim.
"""

from __future__ import annotations

import logging
import re
import uuid

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from iarf_assistant.addons.translator import segment_output as _segment_output
from iarf_assistant.addons.translator.models import TranslationSegment

logger = logging.getLogger(__name__)

TRANSLATABLE_INLINE_TAGS = _segment_output.TRANSLATABLE_INLINE_TAGS
TRANSLATABLE_INLINE_GLOBAL_ATTRIBUTES = (
    _segment_output.TRANSLATABLE_INLINE_GLOBAL_ATTRIBUTES
)
TRANSLATABLE_INLINE_TAG_ATTRIBUTES = _segment_output.TRANSLATABLE_INLINE_TAG_ATTRIBUTES

_PLACEHOLDER_SEQUENCE_RE = re.compile(
    r"(?:\{\{[0-9a-f-]{36}\}\})(?:\s*\{\{[0-9a-f-]{36}\}\})*"
)

try:
    import pysbd
except ImportError:  # pragma: no cover - optional local enhancement
    pysbd = None


class DomSegmenter:
    """Create a placeholder skeleton and a one-to-one ordered segment list."""

    # Deliberately conservative and multilingual. Unknown languages are not
    # forced through an English segmenter just to produce more chunks.
    _fallback_sentence_boundary = re.compile(
        r"(?<=[.!?\u061f\u3002\uff01\uff1f])(?:\s+|(?=\S))"
    )
    _inline_tags = TRANSLATABLE_INLINE_TAGS
    _excluded_tags = frozenset(
        {"script", "style", "noscript", "code", "pre", "svg", "math", "template"}
    )
    _semantic_tags = frozenset(
        {
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "p",
            "li",
            "td",
            "th",
            "figcaption",
            "blockquote",
            "dd",
            "dt",
            "caption",
        }
    )
    _container_tags = frozenset(
        {"div", "section", "article", "main", "aside", "header", "footer"}
    )
    _pysbd_languages = frozenset(
        {"en", "pl", "de", "fr", "es", "it", "pt", "nl", "ru", "uk", "tr"}
    )

    @staticmethod
    def apply_rules_and_segment(
        html_content: str, parsing_rules: dict, project_id: str
    ):
        """Compatibility path for custom rules; preserve source comments."""
        soup = BeautifulSoup(html_content or "", "html.parser")
        segments = []
        # ``Tag`` equality/hash follows mutable contents, so preserve role
        # identity separately while placeholders mutate selected children.
        element_to_role: dict[int, str] = {}
        for role, selector in (parsing_rules or {}).items():
            if not selector or str(selector).lower() == "none":
                continue
            try:
                for element in soup.select(selector):
                    if not DomSegmenter._has_excluded_ancestor(element):
                        element_to_role[id(element)] = role
            except Exception:  # noqa: BLE001 - third-party selector errors vary by backend.
                logger.warning(
                    "Invalid translator selector %r for role %r", selector, role
                )
        candidates = [
            element
            for element in soup.find_all(True)
            if id(element) in element_to_role
            # Prefer the deepest selected element: an outer container and its
            # selected paragraph must not flatten the paragraph or discard
            # direct prose around its preserved non-prose children.
            and not any(
                id(child) in element_to_role for child in element.find_all(True)
            )
        ]
        for block_index, element in enumerate(candidates, start=1):
            inner_html = element.decode_contents().strip()
            if not DomSegmenter._meaningful_html(inner_html):
                continue
            if element.find(DomSegmenter._excluded_tags) or element.find(
                string=lambda value: isinstance(value, Comment)
            ):
                DomSegmenter._segment_direct_prose(
                    element,
                    project_id,
                    block_index,
                    segments,
                    html_tag=element_to_role[id(element)],
                )
                continue
            segment = DomSegmenter._segment(
                project_id,
                block_index,
                element.name,
                element_to_role[id(element)],
                inner_html,
            )
            segments.append(segment)
            element.clear()
            element.append(f"{{{{{segment.id}}}}}")
        block_index = len(candidates)
        for element in soup.find_all(True):
            if id(
                element
            ) not in element_to_role or DomSegmenter._has_excluded_ancestor(element):
                continue
            if not DomSegmenter._has_direct_translatable_content(element):
                continue
            block_index += 1
            DomSegmenter._segment_direct_prose(
                element,
                project_id,
                block_index,
                segments,
                html_tag=element_to_role[id(element)],
            )
        DomSegmenter._sort_by_skeleton(soup, segments)
        DomSegmenter._set_order(segments)
        return str(soup), segments

    @staticmethod
    def _has_excluded_ancestor(element: Tag) -> bool:
        return any(
            getattr(parent, "name", None) in DomSegmenter._excluded_tags
            for parent in element.parents
        )

    @staticmethod
    def _meaningful_html(value: str) -> bool:
        return bool(
            value and BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
        )

    @staticmethod
    def _segment(
        project_id: str, block_index: int, outer_tag: str, html_tag: str, text: str
    ) -> TranslationSegment:
        return TranslationSegment(
            id=str(uuid.uuid4()),
            project_id=project_id,
            order_index=0,
            block_index=block_index,
            html_tag=html_tag,
            outer_tag=outer_tag,
            original_text=text,
            status="pending",
        )

    @staticmethod
    def _set_order(segments: list[TranslationSegment]) -> None:
        for index, segment in enumerate(segments):
            segment.order_index = index

    @staticmethod
    def _sort_by_skeleton(
        soup: BeautifulSoup, segments: list[TranslationSegment]
    ) -> None:
        skeleton = str(soup)
        segments.sort(key=lambda segment: skeleton.find(f"{{{{{segment.id}}}}}"))

    @staticmethod
    def _has_direct_translatable_content(element: Tag) -> bool:
        return any(
            (
                isinstance(child, NavigableString)
                and not isinstance(child, Comment)
                and child.strip()
                and not _PLACEHOLDER_SEQUENCE_RE.fullmatch(str(child).strip())
            )
            or (
                isinstance(child, Tag)
                and child.name in DomSegmenter._inline_tags
                and child.name not in DomSegmenter._excluded_tags
                and not child.find(DomSegmenter._excluded_tags)
                and not child.find(string=lambda value: isinstance(value, Comment))
                and DomSegmenter._meaningful_html(child.decode())
            )
            for child in element.contents
        )

    @staticmethod
    def _plain_text(html_str: str) -> str:
        return BeautifulSoup(html_str, "html.parser").get_text(" ", strip=True)

    @staticmethod
    def _fallback_split(plain_text: str) -> list[str]:
        values = [
            part.strip()
            for part in DomSegmenter._fallback_sentence_boundary.split(plain_text)
            if part.strip()
        ]
        return values or ([plain_text.strip()] if plain_text.strip() else [])

    @staticmethod
    def _split_into_sentences(
        html_str: str,
        *,
        sentence_segmenter=None,
        source_language: str | None = None,
    ) -> list[str]:
        """Split prose without ever splitting an inline HTML structure.

        Providers receive plain text. If inline markup is present, the full
        block remains a single reviewable segment; arbitrary provider offsets
        cannot safely be mapped back to a DOM without risking broken markup.
        """
        if not html_str or not html_str.strip():
            return []
        plain_text = DomSegmenter._plain_text(html_str)
        if not plain_text:
            return []
        if any(
            tag.name in DomSegmenter._inline_tags
            for tag in BeautifulSoup(html_str, "html.parser").find_all(True)
        ):
            return [html_str.strip()]

        raw_sentences = None
        if sentence_segmenter is not None:
            try:
                raw_sentences = sentence_segmenter.split_text(
                    plain_text, raise_on_failure=True
                )
                candidate_sentences = [
                    str(part).strip() for part in raw_sentences if str(part).strip()
                ]
                if not DomSegmenter._faithfully_covers_plain_text(
                    plain_text, candidate_sentences
                ):
                    logger.warning(
                        "Text segmentation provider omitted or reordered source text; "
                        "using local boundaries."
                    )
                    raw_sentences = DomSegmenter._fallback_split(plain_text)
            except Exception:
                logger.warning(
                    "Text segmentation provider failed; using local boundaries.",
                    exc_info=True,
                )
        language = (source_language or "").lower().split("-", 1)[0]
        if (
            raw_sentences is None
            and pysbd
            and language in DomSegmenter._pysbd_languages
        ):
            try:
                raw_sentences = pysbd.Segmenter(
                    language=language, clean=False, char_span=False
                ).segment(plain_text)
            except Exception:
                logger.warning(
                    "pysbd failed for %s; using fallback", language, exc_info=True
                )
        if raw_sentences is None:
            raw_sentences = DomSegmenter._fallback_split(plain_text)
        sentences = [str(part).strip() for part in raw_sentences if str(part).strip()]
        if len(sentences) <= 1:
            fallback = DomSegmenter._fallback_split(plain_text)
            if len(fallback) > 1:
                sentences = fallback
        return sentences or [html_str.strip()]

    @staticmethod
    def _faithfully_covers_plain_text(plain_text: str, sentences: list[str]) -> bool:
        """Accept provider boundaries only when they preserve source order/content."""

        def normalize(value: str) -> str:
            return re.sub(r"\s+", " ", value).strip()

        return bool(sentences) and normalize(" ".join(sentences)) == normalize(
            plain_text
        )

    @staticmethod
    def _placeholderize_sentences(
        source: str, sentences: list[str], placeholders: list[str]
    ) -> str | None:
        """Replace exact sentence spans while retaining every source separator.

        Sentence splitters return sentence bodies, not the whitespace (or lack
        of whitespace) between them.  Joining placeholders with a synthetic
        space subtly changed canonical documents such as ``Sentence.Next``.
        Exact sequential matching lets the immutable skeleton own those source
        separators, so an identity translation reconstructs the source bytes.
        If a provider rewrites a sentence instead of merely locating a
        boundary, fail closed and let the caller keep the block whole.
        """
        if not sentences or len(sentences) != len(placeholders):
            return None
        cursor = 0
        pieces: list[str] = []
        for sentence, placeholder in zip(sentences, placeholders, strict=True):
            start = source.find(sentence, cursor)
            if start < 0:
                return None
            pieces.extend((source[cursor:start], placeholder))
            cursor = start + len(sentence)
        pieces.append(source[cursor:])
        return "".join(pieces)

    @staticmethod
    def _candidate_elements(soup: BeautifulSoup) -> list[Tag]:
        candidates: list[Tag] = []
        for element in soup.find_all(True):
            if DomSegmenter._has_excluded_ancestor(element):
                continue
            if element.name in DomSegmenter._semantic_tags:
                candidates.append(element)
            elif element.name in DomSegmenter._container_tags:
                # Container fallback makes a bare <div> translatable, while a
                # nested paragraph keeps its natural granularity.
                has_content_child = (
                    element.find(
                        tuple(
                            DomSegmenter._semantic_tags | DomSegmenter._container_tags
                        )
                    )
                    is not None
                )
                if not has_content_child and DomSegmenter._meaningful_html(
                    element.decode_contents()
                ):
                    candidates.append(element)
        # Preserve nested semantic DOM (for example ``blockquote > p``) by
        # selecting the deepest candidate. Outer direct prose is handled in a
        # later pass without clearing its child structure.
        return [
            element
            for element in candidates
            if not any(child in candidates for child in element.find_all(True))
        ]

    @staticmethod
    def apply_semantic_segmentation(
        html_content: str,
        project_id: str,
        *,
        sentence_segmenter=None,
        source_language: str | None = None,
    ):
        soup = BeautifulSoup(html_content or "", "html.parser")
        segments: list[TranslationSegment] = []
        block_index = 0
        for element in DomSegmenter._candidate_elements(soup):
            block_index += 1
            # Never make executable/non-prose descendants or comments model
            # input. Segment the surrounding direct prose instead, retaining
            # those exact DOM nodes in the skeleton.
            if element.find(DomSegmenter._excluded_tags) or element.find(
                string=lambda value: isinstance(value, Comment)
            ):
                block_index = DomSegmenter._segment_direct_prose(
                    element,
                    project_id,
                    block_index,
                    segments,
                    sentence_segmenter=sentence_segmenter,
                    source_language=source_language,
                )
                continue
            inner_html = element.decode_contents().strip()
            if not DomSegmenter._meaningful_html(inner_html):
                continue
            sentences = (
                DomSegmenter._split_into_sentences(
                    inner_html,
                    sentence_segmenter=sentence_segmenter,
                    source_language=source_language,
                )
                if element.name
                in {"p", "blockquote", "div", "section", "article", "main"}
                else [inner_html]
            )
            source_for_replacement = (
                inner_html if element.find(True) else element.get_text()
            )
            placeholderized = DomSegmenter._placeholderize_sentences(
                source_for_replacement,
                sentences,
                [f"{{{{pending-{index}}}}}" for index in range(len(sentences))],
            )
            if placeholderized is None:
                # A sentence service may normalize or rewrite prose despite
                # claiming complete coverage.  Preserve the document and make
                # the whole inner fragment one reviewable segment instead.
                sentences = [inner_html]
                source_for_replacement = inner_html

            replacements: list[str] = []
            for sentence in sentences:
                if not DomSegmenter._meaningful_html(sentence):
                    continue
                segment = DomSegmenter._segment(
                    project_id, block_index, element.name, element.name, sentence
                )
                segments.append(segment)
                replacements.append(f"{{{{{segment.id}}}}}")
            if replacements:
                placeholderized = DomSegmenter._placeholderize_sentences(
                    source_for_replacement, sentences, replacements
                )
                if placeholderized is None:  # pragma: no cover - guarded above
                    raise RuntimeError("Unable to preserve sentence boundaries")
                element.clear()
                element.append(placeholderized)

        # An outer semantic/container node can have meaningful direct prose
        # around a deeper selected block. Preserve that shape and translate only
        # its direct text/allowed-inline children. ``body`` and document-level
        # prose are included so a valid snapshot is never silently ignored.
        parents = list(
            soup.find_all(
                tuple(
                    DomSegmenter._semantic_tags
                    | DomSegmenter._container_tags
                    | {"body"}
                )
            )
        )
        if any(
            isinstance(child, NavigableString)
            and not isinstance(child, Comment)
            and child.strip()
            for child in soup.contents
        ):
            parents.append(soup)
        for element in parents:
            if DomSegmenter._has_excluded_ancestor(element):
                continue
            if DomSegmenter._has_direct_translatable_content(element):
                block_index += 1
                block_index = DomSegmenter._segment_direct_prose(
                    element,
                    project_id,
                    block_index,
                    segments,
                    sentence_segmenter=sentence_segmenter,
                    source_language=source_language,
                    outer_tag="body" if element is soup else None,
                )
        DomSegmenter._sort_by_skeleton(soup, segments)
        DomSegmenter._set_order(segments)
        return str(soup), segments

    @staticmethod
    def _segment_direct_prose(
        element: Tag,
        project_id: str,
        block_index: int,
        segments: list[TranslationSegment],
        *,
        sentence_segmenter=None,
        source_language: str | None = None,
        html_tag: str | None = None,
        outer_tag: str | None = None,
    ) -> int:
        """Replace direct prose text only, leaving comments/excluded DOM intact."""
        for child in list(element.contents):
            if (
                isinstance(child, Tag)
                and child.name in DomSegmenter._inline_tags
                and child.name not in DomSegmenter._excluded_tags
                and not child.find(DomSegmenter._excluded_tags)
                and not child.find(string=lambda value: isinstance(value, Comment))
                and DomSegmenter._meaningful_html(child.decode())
            ):
                segment = DomSegmenter._segment(
                    project_id,
                    block_index,
                    outer_tag or element.name or "body",
                    html_tag or element.name,
                    child.decode(),
                )
                segments.append(segment)
                child.replace_with(
                    DomSegmenter._with_preserved_whitespace(
                        str(child), f"{{{{{segment.id}}}}}"
                    )
                )
                continue
            if not isinstance(child, NavigableString) or isinstance(child, Comment):
                continue
            text = str(child)
            if not text.strip() or _PLACEHOLDER_SEQUENCE_RE.fullmatch(text.strip()):
                continue
            sentences = DomSegmenter._split_into_sentences(
                text,
                sentence_segmenter=sentence_segmenter,
                source_language=source_language,
            )
            replacements = []
            for sentence in sentences:
                if not sentence.strip():
                    continue
                segment = DomSegmenter._segment(
                    project_id,
                    block_index,
                    outer_tag or element.name or "body",
                    html_tag or element.name,
                    sentence,
                )
                segments.append(segment)
                replacements.append(f"{{{{{segment.id}}}}}")
            if replacements:
                placeholderized = DomSegmenter._placeholderize_sentences(
                    text, sentences, replacements
                )
                if placeholderized is None:
                    # Treat a provider rewrite as one source-faithful unit.
                    for segment in segments[-len(replacements) :]:
                        segments.remove(segment)
                    segment = DomSegmenter._segment(
                        project_id,
                        block_index,
                        outer_tag or element.name or "body",
                        html_tag or element.name,
                        text.strip(),
                    )
                    segments.append(segment)
                    placeholderized = DomSegmenter._with_preserved_whitespace(
                        text, f"{{{{{segment.id}}}}}"
                    )
                child.replace_with(placeholderized)
        return block_index

    @staticmethod
    def _with_preserved_whitespace(source: str, replacement: str) -> str:
        """Keep DOM text-node boundaries when placeholders replace prose."""
        leading = re.match(r"^\s*", source).group(0)
        trailing = re.search(r"\s*$", source).group(0)
        return f"{leading}{replacement}{trailing}"

    @staticmethod
    def get_default_semantic_rules():
        return {
            "title": "h1",
            "heading": "h2, h3, h4, h5, h6",
            "paragraph": "p",
            "blockquote": "blockquote",
            "list_item": "li",
            "table_header": "th",
            "table_cell": "td",
            "image_caption": "figcaption",
            "footnote": "div.footnote, aside.footnote",
            "footnote_marker": "span.footnote_marker, a.noteref",
        }
