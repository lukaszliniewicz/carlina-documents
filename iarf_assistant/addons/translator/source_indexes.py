"""Invocation-scoped, source-only indexes for immutable translation plans.

These are disposable assembly accelerators, never durable evidence. No target
pointer, glossary, dossier or owner authorization result belongs in this cache.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import logging
from time import perf_counter
from typing import cast

from .block_contract import TranslationSourcePlan


@dataclass(slots=True)
class _SourceIndexScope:
    plan: TranslationSourcePlan
    values: dict[str, object] = field(default_factory=dict)
    hits: int = 0


_scope: ContextVar[_SourceIndexScope | None] = ContextVar(
    "translator_source_index_scope",
    default=None,
)


def reuse_source_indexes[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """Share pure indexes through one keyword-plan assembly call.

    Only the same frozen DTO object can share an index. Nested calls using
    another plan get a separate scope, restored even after an exception. No
    plaintext or ORM object is retained beyond the outer assembly invocation.
    """

    @wraps(function)
    def scoped(*args: P.args, **kwargs: P.kwargs) -> R:
        plan = kwargs.get("plan")
        existing = _scope.get()
        if not isinstance(plan, TranslationSourcePlan) or (
            existing is not None and existing.plan is plan
        ):
            return function(*args, **kwargs)
        scope = _SourceIndexScope(plan)
        token = _scope.set(scope)
        started = perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            _scope.reset(token)
            logging.getLogger(__name__).debug(
                "Translator assembly timing function=%s blocks=%d elapsed_ms=%.3f indexes=%d hits=%d",
                function.__name__,
                len(plan.blocks),
                (perf_counter() - started) * 1000,
                len(scope.values),
                scope.hits,
            )

    return scoped


def source_index[T](
    plan: TranslationSourcePlan,
    name: str,
    build: Callable[[], T],
) -> T:
    """Reuse a source-only value; unscoped callers simply compute it afresh."""
    scope = _scope.get()
    if scope is None or scope.plan is not plan:
        return build()
    if name not in scope.values:
        scope.values[name] = build()
    else:
        scope.hits += 1
    return cast(T, scope.values[name])
