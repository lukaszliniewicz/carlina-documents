"""Pure source-note relationship indexing for document-block plans.

The relationship index is deliberately derived from the attested source HTML
and the immutable block spans.  It does not consult persistence, resolve EPUB
members, or rewrite/replan source markup.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from html import unescape
from types import MappingProxyType
from urllib.parse import unquote, urlsplit

from .block_contract import TranslationBlock, TranslationSourcePlan
from .block_planner import (
    _iter_nodes,
    _LosslessParser,
    _Node,
    _source_offsets,
    _Text,
)

NOTE_RELATION_REASONS = frozenset(
    {
        "missing_target",
        "ambiguous_target",
        "unscoped_link",
        "unmapped_note",
    }
)
_MAX_DIAGNOSTIC_LABEL = 32


@dataclass(frozen=True, slots=True)
class NoteRelationDiagnostic:
    """A bounded diagnostic for one source note reference."""

    label: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or len(self.label) > _MAX_DIAGNOSTIC_LABEL:
            raise ValueError("note-diagnostic-label")
        if self.reason not in NOTE_RELATION_REASONS:
            raise ValueError("note-diagnostic-reason")


# The shorter name is convenient for callers and preserves the two-field
# diagnostic shape in the public contract.
NoteDiagnostic = NoteRelationDiagnostic


@dataclass(frozen=True, slots=True)
class NoteRelations:
    """Resolved note bodies, direct references, and bounded failures."""

    notes: Mapping[str, tuple[TranslationBlock, ...]]
    references: Mapping[str, tuple[str, ...]]
    unresolved: Mapping[str, tuple[NoteRelationDiagnostic, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "notes",
            MappingProxyType(
                {target_id: tuple(blocks) for target_id, blocks in self.notes.items()}
            ),
        )
        object.__setattr__(
            self,
            "references",
            MappingProxyType(
                {
                    block_id: tuple(target_ids)
                    for block_id, target_ids in self.references.items()
                }
            ),
        )
        object.__setattr__(
            self,
            "unresolved",
            MappingProxyType(
                {
                    block_id: tuple(diagnostics)
                    for block_id, diagnostics in self.unresolved.items()
                }
            ),
        )


@dataclass(slots=True)
class _RelationScope:
    plan: TranslationSourcePlan
    relations: NoteRelations | None = None


_relation_scope: ContextVar[_RelationScope | None] = ContextVar(
    "translator_note_relation_scope", default=None
)


def reuse_note_relations[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """Reuse pure source parsing inside one keyword-plan assembly call.

    Nested packet fits share only the exact immutable plan object. No index
    survives the outer call, and mutable target state is never cached here.
    """

    @wraps(function)
    def scoped(*args: P.args, **kwargs: P.kwargs) -> R:
        plan = kwargs.get("plan")
        existing = _relation_scope.get()
        if not isinstance(plan, TranslationSourcePlan) or (
            existing is not None and existing.plan is plan
        ):
            return function(*args, **kwargs)
        token = _relation_scope.set(_RelationScope(plan))
        try:
            return function(*args, **kwargs)
        finally:
            _relation_scope.reset(token)

    return scoped


def _attribute(node: _Node, name: str) -> str | None:
    value = node.attr(name)
    return unescape(value) if value is not None else None


def _tokens(value: str | None) -> set[str]:
    return set((value or "").lower().split())


def _is_noteref(node: _Node) -> bool:
    if node.tag != "a":
        return False
    role = _tokens(_attribute(node, "role"))
    epub_type = _tokens(_attribute(node, "epub:type"))
    return (
        "doc-noteref" in role
        or "noteref" in epub_type
        or "footnote-ref" in _tokens(_attribute(node, "class"))
    )


def _is_note_target(node: _Node) -> bool:
    role = _tokens(_attribute(node, "role"))
    epub_type = _tokens(_attribute(node, "epub:type"))
    return bool(role & {"doc-footnote", "doc-endnote"}) or bool(
        epub_type & {"footnote", "endnote"}
    )


def _text_content(node: _Node) -> str:
    pieces: list[str] = []

    def visit(children: Iterable[_Node | _Text]) -> None:
        for child in children:
            if isinstance(child, _Text):
                pieces.append(unescape(child.raw))
            else:
                visit(child.children)

    visit(node.children)
    return " ".join("".join(pieces).split())


def _diagnostic_label(node: _Node, fragment: str | None = None) -> str:
    label = _text_content(node)
    if not label and fragment:
        label = fragment
    return label[:_MAX_DIAGNOSTIC_LABEL]


def _source_nodes(plan: TranslationSourcePlan) -> list[_Node]:
    parser = _LosslessParser(plan.snapshot.output_html)
    roots, _texts = parser.parse()
    for root in roots:
        if isinstance(root, _Node):
            _source_offsets(root, plan.snapshot.output_html)
    return list(_iter_nodes(roots))


def _target_id(node: _Node) -> str | None:
    value = _attribute(node, "id")
    return value if value else None


def _source_order(block: TranslationBlock) -> tuple[int, int, str, str]:
    return (block.source_start, block.source_end, block.locator, block.id)


def _containing_block(
    node: _Node, blocks: Sequence[TranslationBlock]
) -> TranslationBlock | None:
    end = node.end
    if end is None:
        return None
    containing = [
        block
        for block in blocks
        if block.source_start <= node.start and end <= block.source_end
    ]
    if not containing:
        return None
    return min(
        containing,
        key=lambda block: (
            block.source_end - block.source_start,
            block.source_start,
            block.source_end,
            block.id,
        ),
    )


def _note_blocks(
    target: _Node, blocks: Sequence[TranslationBlock]
) -> tuple[TranslationBlock, ...]:
    end = target.end
    if end is None:
        return ()
    contained = [
        block
        for block in blocks
        if block.role == "note_body"
        and target.start <= block.source_start
        and block.source_end <= end
    ]
    if contained:
        return tuple(sorted(contained, key=_source_order))

    # A semantic note target can be an inline element nested in one planner
    # block.  Include that one block only when the planner already classified
    # it as note_body; an ordinary containing paragraph is unrelated prose.
    enclosing = [
        block
        for block in blocks
        if block.source_start <= target.start and end <= block.source_end
    ]
    if not enclosing:
        return ()
    nearest = min(
        enclosing,
        key=lambda block: (
            block.source_end - block.source_start,
            block.source_start,
            block.source_end,
            block.id,
        ),
    )
    return (nearest,) if nearest.role == "note_body" else ()


def _resolve_fragment(href: str | None) -> tuple[str | None, str | None]:
    """Return ``(fragment, failure_reason)`` for one source href."""

    if href is None:
        return None, "missing_target"
    decoded_href = unescape(href)
    try:
        parsed = urlsplit(decoded_href)
    except ValueError:
        return None, "missing_target"
    if not decoded_href:
        return None, "missing_target"
    if parsed.path or not decoded_href.startswith("#"):
        return None, "unscoped_link"
    if not parsed.fragment:
        return None, "missing_target"
    fragment = unquote(parsed.fragment)
    return (fragment, None) if fragment else (None, "missing_target")


def build_note_relations(plan: TranslationSourcePlan) -> NoteRelations:
    """Build direct source note relationships from one immutable source plan."""

    scope = _relation_scope.get()
    if scope is not None and scope.plan is plan:
        if scope.relations is None:
            scope.relations = _build_note_relations(plan)
        return scope.relations
    return _build_note_relations(plan)


def _build_note_relations(plan: TranslationSourcePlan) -> NoteRelations:
    nodes = _source_nodes(plan)
    blocks = tuple(plan.blocks)
    nodes_by_id: dict[str, list[_Node]] = {}
    for node in nodes:
        target_id = _target_id(node)
        if target_id is not None:
            nodes_by_id.setdefault(target_id, []).append(node)

    references: dict[str, list[str]] = {}
    unresolved: dict[str, list[NoteRelationDiagnostic]] = {}
    notes: dict[str, tuple[TranslationBlock, ...]] = {}
    seen_by_block: dict[str, set[str]] = {}

    anchors = [
        node
        for node in nodes
        if _is_noteref(node)
    ]
    anchors.sort(key=lambda node: (node.start, node.end or node.start))
    for anchor in anchors:
        owner = _containing_block(anchor, blocks)
        if owner is None:
            continue
        fragment, failure = _resolve_fragment(anchor.attr("href"))
        label = _diagnostic_label(anchor, fragment)
        if failure is not None:
            unresolved.setdefault(owner.id, []).append(
                NoteRelationDiagnostic(label=label, reason=failure)
            )
            continue
        assert fragment is not None
        candidates = nodes_by_id.get(fragment, [])
        if not candidates:
            unresolved.setdefault(owner.id, []).append(
                NoteRelationDiagnostic(label=label, reason="missing_target")
            )
            continue
        if len(candidates) != 1:
            unresolved.setdefault(owner.id, []).append(
                NoteRelationDiagnostic(label=label, reason="ambiguous_target")
            )
            continue
        target = candidates[0]
        if not _is_note_target(target):
            unresolved.setdefault(owner.id, []).append(
                NoteRelationDiagnostic(label=label, reason="unmapped_note")
            )
            continue
        note_blocks = _note_blocks(target, blocks)
        if not note_blocks:
            unresolved.setdefault(owner.id, []).append(
                NoteRelationDiagnostic(label=label, reason="unmapped_note")
            )
            continue
        seen = seen_by_block.setdefault(owner.id, set())
        if fragment not in seen:
            seen.add(fragment)
            references.setdefault(owner.id, []).append(fragment)
        notes.setdefault(fragment, note_blocks)

    return NoteRelations(
        notes={key: value for key, value in notes.items()},
        references={key: tuple(value) for key, value in references.items()},
        unresolved={key: tuple(value) for key, value in unresolved.items()},
    )


def window_blocks_are_linked(
    plan: TranslationSourcePlan, block_ids: Sequence[str]
) -> bool:
    """Validate a contiguous base plus an explicitly linked note suffix."""

    if not isinstance(plan, TranslationSourcePlan):
        return False
    if isinstance(block_ids, (str, bytes)) or not isinstance(block_ids, Sequence):
        return False
    if not block_ids:
        return False
    blocks_by_id = {block.id: block for block in plan.blocks}
    if any(not isinstance(block_id, str) for block_id in block_ids):
        return False
    if len(block_ids) != len(set(block_ids)):
        return False
    if any(block_id not in blocks_by_id for block_id in block_ids):
        return False

    ordered = sorted(plan.blocks, key=_source_order)
    source_position = {block.id: index for index, block in enumerate(ordered)}
    positions = [source_position[block_id] for block_id in block_ids]
    prefix_length = 1
    while (
        prefix_length < len(positions)
        and positions[prefix_length] == positions[prefix_length - 1] + 1
    ):
        prefix_length += 1
    if prefix_length == len(positions):
        return True

    base_ids = tuple(block_ids[:prefix_length])
    suffix_ids = tuple(block_ids[prefix_length:])
    if any(
        positions[index] >= positions[index + 1]
        for index in range(prefix_length, len(positions) - 1)
    ):
        return False

    relations = build_note_relations(plan)
    target_blocks: set[str] = set()
    for base_id in base_ids:
        for target_id in relations.references.get(base_id, ()):
            target_blocks.update(block.id for block in relations.notes.get(target_id, ()))
    return all(block_id in target_blocks for block_id in suffix_ids)


__all__ = [
    "NOTE_RELATION_REASONS",
    "NoteDiagnostic",
    "NoteRelationDiagnostic",
    "NoteRelations",
    "build_note_relations",
    "reuse_note_relations",
    "window_blocks_are_linked",
]
