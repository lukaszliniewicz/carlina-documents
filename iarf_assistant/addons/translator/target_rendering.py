"""Deterministic target reconstruction and fail-closed semantic validation."""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from carlina.core.semantic_diagrams import diagnose_mermaid_diagrams
from carlina.core.semantic_html import (
    MAX_INLINE_IMAGE_CHARS,
    SAFE_DATA_IMAGE_RE,
    SAFE_LANGUAGE_RE,
    SAFE_LINK_SCHEMES,
)
from carlina.core.semantic_images import diagnose_image_descriptions

from .block_contract import (
    BlockContractError,
    ProtectedInlineToken,
    TranslationBlock,
    TranslationSourcePlan,
)
from .block_notes import (
    ANNOTATION_PROFILE_STUDY,
    BlockNoteCandidate,
    ReaderNoteCandidate,
    note_anchor_span,
    render_endnotes,
    stable_note_ids,
    stable_reader_note_ids,
    validate_note_candidates,
)
from .block_planner import _LosslessParser, _Node, _source_offsets, _Text
from .target_contract import (
    TargetBlockCandidate,
    lexical_slots,
    literal_reference_slots,
    token_map,
)
from .target_validation import TargetValidationFinding, require_valid_target_candidate

TARGET_SEMANTIC_VALIDATOR_VERSION = "target-semantic-document-v1"
_OPEN_TAG_RE = re.compile(r"<\s*([A-Za-z][A-Za-z0-9:_-]*)\b[^>]*>")
_LANG_ATTR_RE = re.compile(
    r"\s+(?:xml:lang|lang)\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE
)
# HTML document whitespace is ordinary data, not a control-injection vector.
# Keep the same boundary as the provider candidate DTO: reject C0/C1 controls
# except tab, CR, and LF.
_UNSAFE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_INLINE_CLOSE_TAG_RE = re.compile(
    r"</\s*(?:a|abbr|b|cite|code|del|em|i|ins|kbd|mark|q|s|samp|small|span|"
    r"strong|sub|sup|time|u|var)\s*>",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class TargetSemanticValidationResult:
    findings: tuple[TargetValidationFinding, ...]
    validator_version: str = TARGET_SEMANTIC_VALIDATOR_VERSION

    @property
    def valid(self) -> bool:
        return not any(finding.severity == "fatal" for finding in self.findings)

    def evidence(self) -> dict[str, object]:
        return {
            "validator_version": self.validator_version,
            "valid": self.valid,
            "findings": [finding.to_dict() for finding in self.findings],
        }


def _source_fragment(block: TranslationBlock, plan: TranslationSourcePlan) -> str:
    source = plan.snapshot.output_html
    if not (0 <= block.source_start < block.source_end <= len(source)):
        raise BlockContractError("target-render-block-span")
    return source[block.source_start : block.source_end]


def _token_contains_only_literal(block: TranslationBlock, token_id: str) -> bool:
    """Whether a token's element scope is intentionally foreign/literal.

    The source planner gives open/close tokens one stable element ID.  A
    translated scope receives the target language; a scope whose owned text is
    entirely literal remains source-authored.  This decision is based only on
    the immutable source plan, never on provider markup.
    """

    token = token_map(block)[token_id]
    if token.kind == "void":
        return True
    peers = [
        item
        for item in token_map(block).values()
        if item.element_id == token.element_id
    ]
    start = min(item.raw_start for item in peers)
    end = max(item.raw_end for item in peers)
    owned = [
        slot
        for slot in block.slots
        if slot.kind != "attribute" and start <= slot.raw_start and slot.raw_end <= end
    ]
    return bool(owned) and all(
        slot.disposition == "literal-preserved" for slot in owned
    )


def _rewrite_token_attributes(
    token_value: str,
    token_start: int,
    token_end: int,
    lexical_replacements: Mapping[tuple[int, int], str],
) -> str:
    replacements = [
        (start - token_start, end - token_start, html.escape(value, quote=True))
        for (start, end), value in lexical_replacements.items()
        if token_start <= start < end <= token_end
    ]
    return (
        _apply_replacements(token_value, replacements) if replacements else token_value
    )


def _render_token(
    block: TranslationBlock,
    token_id: str,
    target_language: str,
    lexical_replacements: Mapping[tuple[int, int], str],
) -> str:
    token = token_map(block)[token_id]
    value = _rewrite_token_attributes(
        token.source_value,
        token.raw_start,
        token.raw_end,
        lexical_replacements,
    )
    if token.kind != "open" or _token_contains_only_literal(block, token_id):
        return value
    return _derive_outer_language(value, target_language)


def _target_core(
    block: TranslationBlock,
    candidate: TargetBlockCandidate,
    target_language: str,
    lexical_replacements: Mapping[tuple[int, int], str],
    *,
    ignored_token_ids: frozenset[str] = frozenset(),
) -> str:
    tokens = token_map(block)
    literals = {slot.id: slot for slot in literal_reference_slots(block)}
    pieces: list[str] = []
    for part in candidate.parts:
        if part.kind == "text":
            pieces.append(html.escape(part.text or "", quote=False))
        elif part.kind == "token":
            if (part.token_id or "") in ignored_token_ids:
                continue
            token = tokens.get(part.token_id or "")
            if token is None:
                raise BlockContractError("target-render-token")
            pieces.append(
                _render_token(block, token.id, target_language, lexical_replacements)
            )
        elif part.kind == "format":
            tag = part.format_tag or ""
            pieces.append(f"<{tag}>" if part.format_boundary == "open" else f"</{tag}>")
        else:
            literal = literals.get(part.literal_slot_id or "")
            if literal is None:
                raise BlockContractError("target-render-literal")
            # The source span is an immutable exact witness.  It is not a
            # provider string and must not be escaped or normalized again.
            pieces.append(literal.source_value)
    return "".join(pieces)


def _source_only_wrapper_tokens(
    block: TranslationBlock,
) -> tuple[tuple[ProtectedInlineToken, ...], tuple[ProtectedInlineToken, ...]]:
    """Return the one deterministic target wrapper around a corrected merge.

    All original source wrapper tokens remain in the plan for byte identity.
    Only the first opening chain and final closing chain wrap the target; the
    intervening close/open boundaries are intentionally absent from target
    markup and from provider/editor candidates.
    """

    tokens = {token.id: token for token in block.protected_inline}
    if not any(token.movement_policy == "source-only" for token in tokens.values()):
        return (), ()
    openings: list[ProtectedInlineToken] = []
    reversed_closings: list[ProtectedInlineToken] = []
    for part in block.wire:
        token = tokens.get(part.ref_id) if part.kind == "token" else None
        if (
            token is None
            or token.kind != "open"
            or token.movement_policy != "source-only"
        ):
            break
        openings.append(token)
    for part in reversed(block.wire):
        token = tokens.get(part.ref_id) if part.kind == "token" else None
        if (
            token is None
            or token.kind != "close"
            or token.movement_policy != "source-only"
        ):
            break
        reversed_closings.append(token)
    if not openings or len(openings) != len(reversed_closings):
        raise BlockContractError("target-render-merge-wrapper")
    if any(
        opening.tag != closing.tag
        for opening, closing in zip(openings, reversed_closings, strict=True)
    ):
        raise BlockContractError("target-render-merge-wrapper")
    return tuple(openings), tuple(reversed(reversed_closings))


def _render_source_only_wrapper(
    token: ProtectedInlineToken,
    *,
    target_language: str,
    lexical_replacements: Mapping[tuple[int, int], str],
) -> str:
    value = _rewrite_token_attributes(
        token.source_value,
        token.raw_start,
        token.raw_end,
        lexical_replacements,
    )
    return (
        _derive_outer_language(value, target_language)
        if token.kind == "open"
        else value
    )


def _apply_replacements(value: str, replacements: list[tuple[int, int, str]]) -> str:
    result = value
    for start, end, replacement in sorted(replacements, reverse=True):
        if start < 0 or end <= start or end > len(result):
            raise BlockContractError("target-render-replacement")
        result = result[:start] + replacement + result[end:]
    return result


def _derive_outer_language(fragment: str, target_language: str) -> str:
    match = _OPEN_TAG_RE.search(fragment)
    if match is None:
        return fragment
    original = match.group(0)
    stripped = _LANG_ATTR_RE.sub("", original[:-1])
    escaped = html.escape(target_language, quote=True)
    # Preserve a valid void/self-closing opening tag.  The parser accepts both
    # ``/>`` and `` />``; normalize either to one unambiguous space before the
    # closing slash so language attributes remain part of the element.
    if stripped.rstrip().endswith("/"):
        stripped = stripped.rstrip()[:-1].rstrip()
        closing = " />"
    else:
        closing = ">"
    replacement = f'{stripped} lang="{escaped}" xml:lang="{escaped}"{closing}'
    return fragment[: match.start()] + replacement + fragment[match.end() :]


def render_target_block_fragment(
    plan: TranslationSourcePlan,
    plan_id: str,
    block: TranslationBlock,
    candidate: TargetBlockCandidate,
    *,
    target_language: str,
    nested_lexical_replacements: Mapping[tuple[int, int], str] | None = None,
    fixed_token_ids: frozenset[str] = frozenset(),
    legacy_direct_prose: bool = False,
) -> str:
    """Render one validated target block while retaining source wrappers exactly."""

    if not isinstance(target_language, str) or not SAFE_LANGUAGE_RE.fullmatch(
        target_language
    ):
        raise BlockContractError("target-render-language")
    require_valid_target_candidate(plan, plan_id, block, candidate)
    render_start, render_end = _target_replacement_span(
        block,
        fixed_token_ids,
        legacy_direct_prose=legacy_direct_prose,
    )
    fragment = plan.snapshot.output_html[render_start:render_end]
    nested_lexical_replacements = nested_lexical_replacements or {}
    lexical = {item.slot_id: item.value for item in candidate.lexical_attributes}
    attr_slots = lexical_slots(block)
    if not block.slots or (
        not any(slot.kind != "attribute" for slot in block.slots) and attr_slots
    ):
        # Accessibility-only blocks are a source attribute value span rather
        # than an element fragment.  Its one lexical slot is its whole output.
        if len(attr_slots) != 1:
            raise BlockContractError("target-render-accessibility")
        return html.escape(lexical[attr_slots[0].id], quote=True)

    token_spans = [
        (token.raw_start, token.raw_end)
        for token in block.protected_inline
        if token.id not in fixed_token_ids
    ]
    replacements: list[tuple[int, int, str]] = []
    for slot in attr_slots:
        if not (render_start <= slot.raw_start < slot.raw_end <= render_end):
            continue
        replacements.append(
            (
                slot.raw_start - render_start,
                slot.raw_end - render_start,
                html.escape(lexical[slot.id], quote=True),
            )
        )
    for (start, end), value in nested_lexical_replacements.items():
        if not (render_start <= start < end <= render_end):
            continue
        if any(
            token_start <= start < end <= token_end
            for token_start, token_end in token_spans
        ):
            # The core replacement renders this opening token from its source
            # witness, so inject the nested lexical value there instead of
            # creating an overlapping fragment replacement.
            continue
        replacements.append(
            (
                start - render_start,
                end - render_start,
                html.escape(value, quote=True),
            )
        )
    non_attribute = [
        (item.raw_start, item.raw_end)
        for item in [*block.slots, *block.protected_inline]
        if getattr(item, "kind", None) != "attribute"
        and not (isinstance(item, ProtectedInlineToken) and item.id in fixed_token_ids)
        and not (
            legacy_direct_prose
            and getattr(item, "disposition", None) == "structural-only"
        )
    ]
    if non_attribute:
        start = min(item[0] for item in non_attribute)
        end = max(item[1] for item in non_attribute)
        if not (render_start <= start < end <= render_end):
            raise BlockContractError("target-render-core-span")
        core = _target_core(
            block,
            candidate,
            target_language,
            nested_lexical_replacements,
            ignored_token_ids=fixed_token_ids,
        )
        wrapper_openings, wrapper_closings = _source_only_wrapper_tokens(block)
        if wrapper_openings:
            core = (
                "".join(
                    _render_source_only_wrapper(
                        token,
                        target_language=target_language,
                        lexical_replacements=nested_lexical_replacements,
                    )
                    for token in wrapper_openings
                )
                + core
                + "".join(
                    _render_source_only_wrapper(
                        token,
                        target_language=target_language,
                        lexical_replacements=nested_lexical_replacements,
                    )
                    for token in wrapper_closings
                )
            )
        replacements.append(
            (
                start - render_start,
                end - render_start,
                core,
            )
        )
    rendered = _apply_replacements(fragment, replacements)
    return _derive_outer_language(rendered, target_language)


def _nodes(roots: Sequence[_Node | object]) -> list[_Node]:
    result: list[_Node] = []
    for item in roots:
        if isinstance(item, _Node):
            result.append(item)
            result.extend(_nodes(item.children))
    return result


def _complete_document_root(roots: Sequence[_Node | _Text]) -> _Node | None:
    """Return the one structurally valid document root, if present."""

    if len(roots) != 1 or not isinstance(roots[0], _Node) or roots[0].tag != "html":
        return None
    root = roots[0]
    root_children = [child for child in root.children if isinstance(child, _Node)]
    heads = [child for child in root_children if child.tag == "head"]
    bodies = [child for child in root_children if child.tag == "body"]
    if len(heads) > 1 or len(bodies) != 1:
        return None
    if heads and heads[0].start > bodies[0].start:
        return None
    for node in _nodes(roots):
        if node.tag == "html" and node is not root:
            return None
        if node.tag in {"head", "body"} and node.parent is not root:
            return None
    return root


def _safe_url(value: str, *, image: bool = False) -> bool:
    if _UNSAFE_CONTROL_RE.search(value) or not value or value.strip() != value:
        return False
    if image and value.lower().startswith("data:"):
        return len(value) <= MAX_INLINE_IMAGE_CHARS and bool(
            SAFE_DATA_IMAGE_RE.fullmatch(value)
        )
    parsed = urlparse(value)
    if value.startswith("#"):
        return len(value) > 1 and " " not in value
    if parsed.scheme:
        return parsed.scheme.lower() in SAFE_LINK_SCHEMES and bool(
            parsed.netloc or parsed.path
        )
    # The semantic representation permits local document/media references.
    # Never turn a scheme-relative URL into an implicit network URL.
    return not value.startswith("//") and " " not in value and not value.startswith(":")


def validate_target_semantic_document(document: str) -> TargetSemanticValidationResult:
    """Validate an assembled target document without source access or repair."""

    codes: set[str] = set()
    if (
        not isinstance(document, str)
        or not document
        or len(document.encode("utf-8")) > 20 * 1024 * 1024
    ):
        return TargetSemanticValidationResult(
            (TargetValidationFinding("target-document-size"),)
        )
    if _UNSAFE_CONTROL_RE.search(document):
        return TargetSemanticValidationResult(
            (TargetValidationFinding("target-document-control"),)
        )
    try:
        parser = _LosslessParser(document)
        roots, _texts = parser.parse()
        root = _complete_document_root(roots)
        if root is None:
            return TargetSemanticValidationResult(
                (TargetValidationFinding("target-document-root"),)
            )
        if any(start > root.start for start, _end in parser.doctype_spans):
            return TargetSemanticValidationResult(
                (TargetValidationFinding("target-document-root"),)
            )
        for root in roots:
            if isinstance(root, _Node):
                _source_offsets(root, document)
    except BlockContractError:
        return TargetSemanticValidationResult(
            (TargetValidationFinding("target-document-markup"),)
        )

    nodes = _nodes(roots)
    if diagnose_image_descriptions(BeautifulSoup(document, "html.parser")):
        codes.add("target-document-image-description")
    if diagnose_mermaid_diagrams(BeautifulSoup(document, "html.parser")):
        codes.add("target-document-diagram")
    ids: set[str] = set()
    references: list[tuple[str, str]] = []
    note_ids: set[str] = set()
    headings: list[int] = []
    for node in nodes:
        attrs = {attr.name.lower(): attr.value for attr in node.attrs}
        identifier = attrs.get("id")
        if identifier:
            if identifier in ids:
                codes.add("target-document-id-duplicate")
            ids.add(identifier)
        if node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            headings.append(int(node.tag[1]))
        role = (attrs.get("role") or "").lower()
        epub = (attrs.get("epub:type") or "").lower()
        if identifier and (
            role in {"doc-footnote", "doc-endnote"}
            or "footnote" in epub
            or "endnote" in epub
        ):
            note_ids.add(identifier)
        for name, value in attrs.items():
            if value is None:
                continue
            if name in {"lang", "xml:lang"} and not SAFE_LANGUAGE_RE.fullmatch(value):
                codes.add("target-document-language")
            if name in {"href", "src", "cite"} and not _safe_url(
                value, image=node.tag == "img" and name == "src"
            ):
                codes.add("target-document-url")
            if name in {"aria-describedby", "aria-labelledby", "headers"}:
                references.extend((name, item) for item in value.split() if item)
            if name == "href" and value.startswith("#"):
                references.append(("href", value[1:]))
        if role == "doc-noteref" or "noteref" in epub:
            href = attrs.get("href")
            if not href or not href.startswith("#"):
                codes.add("target-document-noteref")
            else:
                references.append(("noteref", href[1:]))
        if node.tag in {"ul", "ol"} and any(
            child.tag not in {"li"}
            for child in node.children
            if isinstance(child, _Node)
        ):
            codes.add("target-document-list")
        if node.tag == "table" and not any(
            child.tag in {"tbody", "thead", "tfoot", "tr"}
            for child in node.children
            if isinstance(child, _Node)
        ):
            codes.add("target-document-table")
        if node.tag == "figure" and "mermaid-diagram-v1" not in (
            attrs.get("class") or ""
        ).split() and not any(
            child.tag in {"img", "figcaption"}
            for child in node.children
            if isinstance(child, _Node)
        ):
            codes.add("target-document-figure")
    for kind, target in references:
        if target not in ids:
            codes.add("target-document-reference")
        if kind == "noteref" and target not in note_ids:
            codes.add("target-document-noteref")
    findings = [TargetValidationFinding(code) for code in sorted(codes)]
    if headings and any(right > left + 1 for left, right in pairwise(headings)):
        # A heading jump can be an intentional editorial choice.  Preserve it
        # as evidence rather than changing or rejecting valid target bytes.
        findings.append(
            TargetValidationFinding("target-document-heading-order", "report")
        )
    return TargetSemanticValidationResult(tuple(findings))


def _derive_document_language(document: str, target_language: str) -> str:
    match = re.search(r"<\s*html\b[^>]*>", document, re.IGNORECASE)
    if match is None:
        raise BlockContractError("target-document-root")
    return (
        _derive_outer_language(document[: match.end()], target_language)
        + document[match.end() :]
    )


def _complete_target_document(document: str, target_language: str) -> str:
    """Return one complete target document without changing source evidence.

    Document-processing providers intentionally attest either a complete HTML
    document or a semantic fragment such as ``<main>...</main>``.  Translation
    offsets and hashes remain bound to those exact source bytes.  The preview
    and export boundary, however, requires one complete HTML document, so add a
    target-only envelope after reconstruction when the attested shape is a
    fragment.
    """

    try:
        parser = _LosslessParser(document)
        roots, _texts = parser.parse()
    except BlockContractError as exc:
        raise BlockContractError("target-document-markup") from exc

    root_nodes = [root for root in roots if isinstance(root, _Node)]
    document_root = _complete_document_root(roots)
    if document_root is not None:
        if any(start > document_root.start for start, _end in parser.doctype_spans):
            raise BlockContractError("target-document-root")
        return _derive_document_language(document, target_language)
    shell_nodes = [
        node for node in _nodes(roots) if node.tag in {"html", "head", "body"}
    ]
    if parser.doctype_spans:
        raise BlockContractError("target-document-root")
    if any(node.tag == "html" or node.parent is not None for node in shell_nodes):
        raise BlockContractError("target-document-root")

    escaped = html.escape(target_language, quote=True)
    envelope = f'<html lang="{escaped}" xml:lang="{escaped}">'
    if shell_nodes:
        only_shell = len(shell_nodes) == len(root_nodes) and all(
            not isinstance(root, _Text) or not root.raw.strip() for root in roots
        )
        body_count = sum(node.tag == "body" for node in shell_nodes)
        head_count = sum(node.tag == "head" for node in shell_nodes)
        ordered_shell = sorted(shell_nodes, key=lambda node: node.start)
        if (
            not only_shell
            or body_count > 1
            or head_count > 1
            or (head_count and body_count and ordered_shell[0].tag != "head")
        ):
            raise BlockContractError("target-document-root")
        empty_body = "" if body_count else "<body></body>"
        return f"{envelope}{document}{empty_body}</html>"
    return f"{envelope}<body>{document}</body></html>"


def _coerce_reader_note(
    value: ReaderNoteCandidate | Mapping[str, object], block_id: str
) -> ReaderNoteCandidate:
    """Normalize the internal reader-note projection for semantic rendering."""

    if isinstance(value, ReaderNoteCandidate):
        note = value
    elif isinstance(value, Mapping):
        note_id = value.get("id", value.get("note_id"))
        anchor = value.get("anchor")
        if anchor is not None:
            if not isinstance(anchor, Mapping):
                raise BlockContractError("target-document-note-anchor")
            if set(anchor) - {
                "kind",
                "anchor_kind",
                "text",
                "anchor_text",
                "occurrence_index",
            }:
                raise BlockContractError("target-document-note-anchor")
            kind = anchor.get("kind", anchor.get("anchor_kind"))
            if (
                "kind" in anchor
                and "anchor_kind" in anchor
                and anchor["kind"] != anchor["anchor_kind"]
            ):
                raise BlockContractError("target-document-note-anchor")
            if kind == "whole_block":
                kind = "block"
            anchor_text = anchor.get("text", anchor.get("anchor_text"))
            occurrence = anchor.get("occurrence_index")
        else:
            kind = value.get("anchor_kind", value.get("kind", "block"))
            if (
                "kind" in value
                and "anchor_kind" in value
                and value["kind"] != value["anchor_kind"]
            ):
                raise BlockContractError("target-document-note-anchor")
            anchor_text = value.get("anchor_text")
            occurrence = value.get("occurrence_index")
        citations = value.get("citations", value.get("citation_ids", ()))
        if citations is None:
            citations = ()
        if isinstance(citations, list):
            citations = tuple(citations)
        target_revision_id = value.get("target_revision_id")
        note = ReaderNoteCandidate(
            id=note_id,
            block_id=value.get("block_id", block_id),
            anchor_kind=kind,
            anchor_text=anchor_text,
            occurrence_index=occurrence,
            body=value.get("body"),
            citations=citations,
            target_revision_id=target_revision_id,
        )
    else:
        raise BlockContractError("target-document-note-shape")
    if note.block_id != block_id:
        raise BlockContractError("target-document-note-owner")
    return note


def _text_occurrences(value: str, needle: str) -> list[tuple[int, int]]:
    matches: list[tuple[int, int]] = []
    start = 0
    while True:
        found = value.find(needle, start)
        if found < 0:
            return matches
        matches.append((found, found + len(needle)))
        start = found + 1


def _visible_text_offsets(fragment: str) -> tuple[str, tuple[int, ...]]:
    """Map DOM-visible characters to raw HTML end offsets.

    The lossless parser splits entity references into text nodes, so scanning
    those nodes retains exact raw offsets while decoding entities.  Inline
    element boundaries disappear from visible text, allowing a reader anchor
    to span protected markup without reparsing or serializing the fragment.
    """

    try:
        _roots, text_nodes = _LosslessParser(fragment).parse()
    except BlockContractError as exc:
        raise BlockContractError("target-document-note-anchor") from exc
    visible: list[str] = []
    ends: list[int] = []
    for text_node in text_nodes:
        raw = fragment[text_node.start : text_node.end]
        cursor = 0
        while cursor < len(raw):
            if raw[cursor] == "&":
                semicolon = raw.find(";", cursor + 1)
                if semicolon != -1:
                    token = raw[cursor : semicolon + 1]
                    expanded = html.unescape(token)
                    if expanded != token:
                        visible.extend(expanded)
                        ends.extend([text_node.start + semicolon + 1] * len(expanded))
                        cursor = semicolon + 1
                        continue
            visible.append(raw[cursor])
            ends.append(text_node.start + cursor + 1)
            cursor += 1
    return "".join(visible), tuple(ends)


def _reader_target_anchor_end(fragment: str, note: ReaderNoteCandidate) -> int:
    visible, ends = _visible_text_offsets(fragment)
    occurrences = _text_occurrences(visible, note.anchor_text or "")
    occurrence = note.occurrence_index
    if occurrence is None or occurrence >= len(occurrences):
        raise BlockContractError("target-document-note-occurrence")
    _start, visible_end = occurrences[occurrence]
    if visible_end <= 0 or visible_end > len(ends):
        raise BlockContractError("target-document-note-occurrence")
    raw_end = ends[visible_end - 1]
    # A visible selection that ends inside nested inline elements belongs after
    # those closing elements.  Never cross the containing block close, which
    # keeps the noteref inside the rendered block.
    while raw_end < len(fragment):
        match = _INLINE_CLOSE_TAG_RE.match(fragment, raw_end)
        if match is None:
            break
        raw_end = match.end()
    return raw_end


def _reader_source_anchor_is_valid(
    block: TranslationBlock, note: ReaderNoteCandidate
) -> None:
    occurrences = _text_occurrences(block.projection, note.anchor_text or "")
    if note.occurrence_index is None or note.occurrence_index >= len(occurrences):
        raise BlockContractError("target-document-note-occurrence")


def _block_end_insertion_point(fragment: str) -> int:
    """Choose a deterministic end-of-block insertion point."""

    closing_tags = list(re.finditer(r"</\s*[A-Za-z][A-Za-z0-9:_-]*\s*>", fragment))
    return closing_tags[-1].start() if closing_tags else len(fragment)


def _existing_html_ids(document: str) -> set[str]:
    try:
        _roots, _text_nodes = _LosslessParser(document).parse()
    except BlockContractError:
        return set()
    result: set[str] = set()
    for node in _nodes(_roots):
        for attr in node.attrs:
            if attr.name.lower() == "id" and attr.value:
                result.add(attr.value)
    return result


def _apply_note_insertions(
    fragment: str, insertions: Sequence[tuple[int, int, str]]
) -> str:
    """Apply raw offsets from right to left, preserving service order."""

    result = fragment
    grouped: dict[int, list[tuple[int, str]]] = {}
    for offset, order, reference in insertions:
        grouped.setdefault(offset, []).append((order, reference))
    for offset in sorted(grouped, reverse=True):
        if not 0 <= offset <= len(result):
            raise BlockContractError("target-document-note-anchor")
        references = "".join(reference for _order, reference in sorted(grouped[offset]))
        result = result[:offset] + references + result[offset:]
    return result



def _legacy_direct_prose_nested_blocks(
    block: TranslationBlock,
    content_blocks: Sequence[TranslationBlock],
) -> tuple[TranslationBlock, ...]:
    """Return independently translated blocks enclosed by a legacy prose envelope.

    A small number of persisted plans can give a ``direct_prose()`` block a
    coarse source envelope that also covers nested structural content.  The
    block's lexical slots remain disjoint from the nested blocks, so export can
    safely reconstruct it from its actual lexical coverage instead of replacing
    the whole coarse envelope.
    """

    if "/direct_prose()[" not in block.locator:
        return ()
    return tuple(
        other
        for other in content_blocks
        if other.id != block.id
        and block.source_start <= other.source_start
        and other.source_end <= block.source_end
    )


def _legacy_direct_prose_fixed_token_ids(
    block: TranslationBlock,
    nested_blocks: Sequence[TranslationBlock],
) -> frozenset[str]:
    """Keep wrappers spanning nested translated blocks in the source skeleton."""

    if not nested_blocks or not block.protected_inline:
        return frozenset()
    element_spans: dict[str, tuple[int, int]] = {}
    for token in block.protected_inline:
        start, end = element_spans.get(
            token.element_id, (token.raw_start, token.raw_end)
        )
        element_spans[token.element_id] = (
            min(start, token.raw_start),
            max(end, token.raw_end),
        )
    fixed_elements = {
        element_id
        for element_id, (start, end) in element_spans.items()
        if any(
            start <= child.source_start and child.source_end <= end
            for child in nested_blocks
        )
    }
    return frozenset(
        token.id for token in block.protected_inline if token.element_id in fixed_elements
    )


def _target_replacement_span(
    block: TranslationBlock,
    fixed_token_ids: frozenset[str] = frozenset(),
    *,
    legacy_direct_prose: bool = False,
) -> tuple[int, int]:
    """Return the raw source envelope actually owned by one target replacement."""

    if not legacy_direct_prose:
        return block.source_start, block.source_end
    spans = [
        (slot.raw_start, slot.raw_end)
        for slot in block.slots
        if slot.kind != "attribute" and slot.disposition != "structural-only"
    ] + [
        (token.raw_start, token.raw_end)
        for token in block.protected_inline
        if token.id not in fixed_token_ids
    ]
    if not spans:
        raise BlockContractError("target-render-legacy-direct-prose-span")
    start = min(item[0] for item in spans)
    end = max(item[1] for item in spans)
    if not (block.source_start <= start < end <= block.source_end):
        raise BlockContractError("target-render-core-span")
    return start, end


def _require_target_replacement_spans_non_overlapping(
    blocks: Sequence[TranslationBlock],
    fixed_tokens: Mapping[str, frozenset[str]],
    legacy_direct_prose: Mapping[str, bool],
) -> None:
    spans = sorted(
        _target_replacement_span(
            block,
            fixed_tokens.get(block.id, frozenset()),
            legacy_direct_prose=legacy_direct_prose.get(block.id, False),
        )
        for block in blocks
    )
    if any(left[1] > right[0] for left, right in pairwise(spans)):
        raise BlockContractError("target-document-block-overlap")


def render_target_document(
    plan: TranslationSourcePlan,
    plan_id: str,
    candidates: Mapping[str, TargetBlockCandidate],
    *,
    target_language: str,
    notes: Mapping[
        str,
        Sequence[BlockNoteCandidate | ReaderNoteCandidate | Mapping[str, object]],
    ]
    | None = None,
) -> str:
    """Reconstruct one complete target document from exactly one result per block."""

    if not isinstance(target_language, str) or not SAFE_LANGUAGE_RE.fullmatch(
        target_language
    ):
        raise BlockContractError("target-render-language")
    if set(candidates) != {block.id for block in plan.blocks}:
        raise BlockContractError("target-document-block-cardinality")
    candidates_by_block = dict(candidates)
    attribute_blocks = [
        block for block in plan.blocks if _is_accessibility_attribute_block(block)
    ]
    content_blocks = [block for block in plan.blocks if block not in attribute_blocks]
    legacy_nested_blocks = {
        block.id: _legacy_direct_prose_nested_blocks(block, content_blocks)
        for block in content_blocks
    }
    legacy_direct_prose = {
        block_id: bool(nested) for block_id, nested in legacy_nested_blocks.items()
    }
    fixed_tokens = {
        block.id: _legacy_direct_prose_fixed_token_ids(
            block, legacy_nested_blocks.get(block.id, ())
        )
        for block in content_blocks
    }
    _require_target_replacement_spans_non_overlapping(
        content_blocks, fixed_tokens, legacy_direct_prose
    )
    consumed_attribute_blocks: set[str] = set()
    replacements: list[tuple[int, int, str]] = []
    note_endnotes: list[
        tuple[BlockNoteCandidate | ReaderNoteCandidate, str, str, int]
    ] = []
    if notes is None:
        notes = {}
    elif not isinstance(notes, Mapping):
        raise BlockContractError("target-document-note-shape")
    plan_block_ids = {block.id for block in plan.blocks}
    if any(block_id not in plan_block_ids for block_id in notes):
        raise BlockContractError("target-document-note-owner")
    if any(notes.get(block.id) for block in attribute_blocks if block.id in notes):
        raise BlockContractError("target-document-note-attribute")
    note_number = 0
    for block in content_blocks:
        candidate = candidates.get(block.id)
        if candidate is None:
            raise BlockContractError("target-document-block-missing")
        nested = _contained_attribute_values(
            plan,
            plan_id,
            block,
            attribute_blocks,
            candidates_by_block,
        )
        consumed_attribute_blocks.update(nested[1])
        fragment = render_target_block_fragment(
            plan,
            plan_id,
            block,
            candidate,
            target_language=target_language,
            nested_lexical_replacements=nested[0],
            fixed_token_ids=fixed_tokens.get(block.id, frozenset()),
            legacy_direct_prose=legacy_direct_prose.get(block.id, False),
        )
        block_notes: list[BlockNoteCandidate] = []
        reader_notes: list[ReaderNoteCandidate] = []
        raw_block_notes = notes.get(block.id, ())
        if raw_block_notes is None or isinstance(raw_block_notes, (str, bytes)):
            raise BlockContractError("target-document-note-shape")
        if not isinstance(raw_block_notes, Sequence):
            raise BlockContractError("target-document-note-shape")
        for index, item in enumerate(raw_block_notes):
            if isinstance(item, BlockNoteCandidate):
                note = item
                if note.block_id != block.id:
                    raise BlockContractError("target-document-note-owner")
            elif isinstance(item, ReaderNoteCandidate) or (
                isinstance(item, Mapping)
                and (
                    "anchor_kind" in item
                    or "kind" in item
                    or isinstance(item.get("anchor"), Mapping)
                )
            ):
                reader_notes.append(_coerce_reader_note(item, block.id))
                continue
            elif isinstance(item, Mapping) and set(item) >= {"anchor_text", "body"}:
                legacy_index = len(block_notes)
                note = BlockNoteCandidate(
                    block_id=block.id,
                    anchor_text=item["anchor_text"],
                    body=item["body"],
                    order_index=legacy_index,
                )
            elif isinstance(item, Mapping) and set(item) == {"anchor", "body"}:
                legacy_index = len(block_notes)
                note = BlockNoteCandidate(
                    block_id=block.id,
                    anchor_text=item["anchor"],
                    body=item["body"],
                    order_index=legacy_index,
                )
            else:
                raise BlockContractError("target-document-note-shape")
            if note.order_index != len(block_notes):
                raise BlockContractError("target-document-note-order")
            block_notes.append(note)
        if block_notes:
            # Rendering verifies already accepted notes independently of the
            # generation profile, which was enforced against the frozen packet.
            validate_note_candidates(
                block_notes,
                {block.id: fragment},
                annotation_profile=ANNOTATION_PROFILE_STUDY,
            )
        note_specs: list[
            tuple[BlockNoteCandidate | ReaderNoteCandidate, str, str, int]
        ] = []
        for note in [*block_notes, *reader_notes]:
            note_number += 1
            if isinstance(note, BlockNoteCandidate):
                note_id, reference_id = stable_note_ids(block.id, note.order_index)
            else:
                note_id, reference_id = stable_reader_note_ids(note.id)
                if note.anchor_kind == "target":
                    _reader_target_anchor_end(fragment, note)
                elif note.anchor_kind == "source":
                    _reader_source_anchor_is_valid(block, note)
            note_specs.append((note, note_id, reference_id, note_number))
            note_endnotes.append((note, note_id, reference_id, note_number))

        insertions: list[tuple[int, int, str]] = []
        for service_order, (note, note_id, reference_id, number) in enumerate(
            note_specs
        ):
            if isinstance(note, BlockNoteCandidate):
                _start, end = note_anchor_span(fragment, note.anchor_text)
            elif note.anchor_kind == "target":
                end = _reader_target_anchor_end(fragment, note)
            else:
                end = _block_end_insertion_point(fragment)
            reference = (
                f'<a id="{html.escape(reference_id, quote=True)}" '
                f'href="#{html.escape(note_id, quote=True)}" role="doc-noteref" '
                f'epub:type="noteref" aria-label="Translator note T{number}">[T{number}]</a>'
            )
            insertions.append((end, service_order, reference))
        if insertions:
            fragment = _apply_note_insertions(fragment, insertions)
        replacement_start, replacement_end = _target_replacement_span(
            block,
            fixed_tokens.get(block.id, frozenset()),
            legacy_direct_prose=legacy_direct_prose.get(block.id, False),
        )
        replacements.append((replacement_start, replacement_end, fragment))
    replacements.extend(
        _render_standalone_attribute_opening_tags(
            plan,
            plan_id,
            [
                block
                for block in attribute_blocks
                if block.id not in consumed_attribute_blocks
            ],
            candidates_by_block,
            target_language,
        )
    )
    from .source_plan_corrections import corrected_exclusion_spans

    replacements.extend(
        (start, end, "") for start, end in corrected_exclusion_spans(plan)
    )
    # Source plans prove non-overlapping canonical blocks.  Keep an explicit
    # check here because target reconstruction is a separate trust boundary.
    ordered = sorted(replacements)
    if any(left[1] > right[0] for left, right in pairwise(ordered)):
        raise BlockContractError("target-document-block-overlap")
    document = _apply_replacements(plan.snapshot.output_html, replacements)
    document = _complete_target_document(document, target_language)
    generated_ids = [item[1] for item in note_endnotes] + [
        item[2] for item in note_endnotes
    ]
    if len(set(generated_ids)) != len(generated_ids) or set(
        generated_ids
    ) & _existing_html_ids(plan.snapshot.output_html):
        raise BlockContractError("target-document-note-id")
    if note_endnotes:
        endnotes = render_endnotes(note_endnotes)
        body_close = re.search(r"</body\s*>", document, re.IGNORECASE)
        if body_close is None:
            raise BlockContractError("target-document-note-root")
        document = (
            document[: body_close.start()] + endnotes + document[body_close.start() :]
        )
    result = validate_target_semantic_document(document)
    if not result.valid:
        raise BlockContractError("target-document-validation")
    return document


def _is_accessibility_attribute_block(block: TranslationBlock) -> bool:
    return bool(lexical_slots(block)) and not any(
        slot.kind != "attribute" for slot in block.slots
    )


def _require_non_attribute_spans_non_overlapping(
    blocks: Sequence[TranslationBlock],
) -> None:
    spans = sorted((block.source_start, block.source_end) for block in blocks)
    if any(left[1] > right[0] for left, right in pairwise(spans)):
        raise BlockContractError("target-document-block-overlap")


def _contained_attribute_values(
    plan: TranslationSourcePlan,
    plan_id: str,
    container: TranslationBlock,
    attribute_blocks: Sequence[TranslationBlock],
    candidates: Mapping[str, TargetBlockCandidate],
) -> tuple[dict[tuple[int, int], str], set[str]]:
    """Map declared accessibility blocks into one containing source block."""

    values: dict[tuple[int, int], str] = {}
    consumed: set[str] = set()
    for attribute_block in attribute_blocks:
        if not (
            container.source_start
            <= attribute_block.source_start
            < attribute_block.source_end
            <= container.source_end
        ):
            continue
        slots = lexical_slots(attribute_block)
        if len(slots) != 1:
            raise BlockContractError("target-render-accessibility")
        candidate = candidates.get(attribute_block.id)
        if candidate is None:
            raise BlockContractError("target-document-block-missing")
        require_valid_target_candidate(
            plan,
            plan_id,
            attribute_block,
            candidate,
        )
        value = candidate.lexical_attributes[0].value
        key = (attribute_block.source_start, attribute_block.source_end)
        if key in values:
            raise BlockContractError("target-render-nested-lexical-duplicate")
        values[key] = value
        consumed.add(attribute_block.id)
    return values, consumed


def _attribute_owner_index(plan: TranslationSourcePlan) -> dict[tuple[int, int], _Node]:
    """Index canonical attribute-value spans by their verified opening tag.

    Accessibility blocks only retain the value span, so target reconstruction
    reparses the immutable source once to recover their owning element.  This
    keeps language derivation element-scoped without guessing from source text.
    """

    source = plan.snapshot.output_html
    try:
        parser = _LosslessParser(source)
        roots, _texts = parser.parse()
        nodes = _nodes(roots)
        for root in roots:
            if isinstance(root, _Node):
                _source_offsets(root, source)
    except BlockContractError as exc:
        raise BlockContractError("target-render-attribute-owner") from exc
    index: dict[tuple[int, int], _Node] = {}
    for node in nodes:
        for attr in node.attrs:
            if attr.value_start is None or attr.value_end is None:
                continue
            span = (attr.value_start, attr.value_end)
            if (
                span in index
                or not (node.start < span[0] < span[1] < node.start_tag_end)
                or source[span[0] : span[1]] != (attr.value or "")
            ):
                raise BlockContractError("target-render-attribute-owner")
            index[span] = node
    return index


def _render_standalone_attribute_opening_tags(
    plan: TranslationSourcePlan,
    plan_id: str,
    blocks: Sequence[TranslationBlock],
    candidates: Mapping[str, TargetBlockCandidate],
    target_language: str,
) -> list[tuple[int, int, str]]:
    """Replace all translated standalone attributes of each owner atomically."""

    owners = _attribute_owner_index(plan)
    grouped: dict[tuple[int, int], tuple[_Node, dict[tuple[int, int], str]]] = {}
    for block in blocks:
        slots = lexical_slots(block)
        if len(slots) != 1:
            raise BlockContractError("target-render-accessibility")
        candidate = candidates.get(block.id)
        if candidate is None:
            raise BlockContractError("target-document-block-missing")
        require_valid_target_candidate(plan, plan_id, block, candidate)
        slot = slots[0]
        span = (slot.raw_start, slot.raw_end)
        owner = owners.get(span)
        if (
            owner is None
            or slot.source_value != plan.snapshot.output_html[span[0] : span[1]]
        ):
            raise BlockContractError("target-render-attribute-owner")
        owner_span = (owner.start, owner.start_tag_end)
        owner_values = grouped.setdefault(owner_span, (owner, {}))[1]
        if span in owner_values:
            raise BlockContractError("target-render-attribute-duplicate")
        owner_values[span] = candidate.lexical_attributes[0].value

    source = plan.snapshot.output_html
    rendered: list[tuple[int, int, str]] = []
    for (start, end), (_owner, values) in grouped.items():
        replacements = [
            (value_start - start, value_end - start, html.escape(value, quote=True))
            for (value_start, value_end), value in values.items()
        ]
        opening_tag = _apply_replacements(source[start:end], replacements)
        rendered.append(
            (start, end, _derive_outer_language(opening_tag, target_language))
        )
    return rendered


__all__ = [
    "TARGET_SEMANTIC_VALIDATOR_VERSION",
    "TargetSemanticValidationResult",
    "render_target_block_fragment",
    "render_target_document",
    "validate_target_semantic_document",
]
