"""Shared Translation Memory profile routing.

TM vectors are only comparable within an owner-scoped source/target language
pair and embedding profile.  Keep namespace construction here so writers,
queries, and route-level cleanup never drift into different vector tables.
"""

from __future__ import annotations

import hashlib
from typing import Any


def translation_memory_namespace(
    project: Any, model: str, dimensions: int, task_type: str
) -> str:
    """Return the stable namespace for one language pair + embedding profile."""

    signature = hashlib.sha256(
        (
            f"{str(project.source_lang).lower()}|{str(project.target_lang).lower()}|"
            f"{model}|{int(dimensions)}|{task_type}"
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"translator_tm_{signature}"


def translation_memory_store(app: Any, project: Any):
    """Resolve the isolated TM store for the project's active embedding profile."""

    rag_service = getattr(app, "rag_service", None)
    if rag_service is None or not rag_service.is_available:
        return None
    model, dimensions, _, _, task_type = rag_service._get_app_config()
    return rag_service.create_isolated_store(
        translation_memory_namespace(project, model, dimensions, task_type),
        dimensions,
        model,
        task_type,
        user_id=project.user_id,
    )


__all__ = ["translation_memory_namespace", "translation_memory_store"]
