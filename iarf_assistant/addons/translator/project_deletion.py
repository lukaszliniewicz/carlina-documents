"""Shared Translator project aggregate deletion and physical cleanup."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from sqlalchemy import delete, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from ...extensions import db

from .embedding_reclaimer import cleanup_embedding_vector_identities
from .models import TranslationEmbeddingGeneration, TranslationProject
from .translation_memory import translation_memory_store

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProjectDeletionArtifacts:
    project_id: str
    user_id: int
    source_lang: str
    target_lang: str
    embedding_identities: tuple[tuple[str, int], ...]
    vector_ids: tuple[int, ...]


def collect_project_deletion_artifacts(
    project: TranslationProject,
) -> ProjectDeletionArtifacts:
    """Capture cleanup identity before the authoritative database delete."""

    project_id = str(project.id)
    user_id = int(project.user_id)
    source_lang = str(project.source_lang)
    target_lang = str(project.target_lang)
    embedding_identities: list[tuple[str, int]] = []
    try:
        # Add-on migrations are normally applied before Translator is exposed,
        # but older databases retain the legacy delete path if the additive
        # embedding-generation table is not present yet.
        if inspect(db.engine).has_table(TranslationEmbeddingGeneration.__tablename__):
            embedding_identities = [
                (generation.namespace, generation.dimensions)
                for generation in TranslationEmbeddingGeneration.query.filter_by(
                    project_id=project_id
                ).all()
            ]
    except SQLAlchemyError:
        logger.exception(
            "Unable to inspect Translator embedding generations for project %s",
            project_id,
        )
    vector_ids = tuple(
        segment.vec_id for segment in project.segments if segment.vec_id is not None
    )
    return ProjectDeletionArtifacts(
        project_id=project_id,
        user_id=user_id,
        source_lang=source_lang,
        target_lang=target_lang,
        embedding_identities=tuple(embedding_identities),
        vector_ids=vector_ids,
    )


def delete_project_aggregate(project: TranslationProject) -> None:
    """Stage one complete project delete without a partial ORM cascade flush."""

    if db.session.get_bind().dialect.name != "sqlite":
        db.session.delete(project)
        return
    # Immutable lineage includes within-project RESTRICT edges. One aggregate-
    # root statement lets SQLite apply every project-owned CASCADE atomically;
    # ORM row-by-row deletion can flush a source plan before its operations.
    db.session.execute(text("PRAGMA defer_foreign_keys = ON"))
    db.session.execute(
        delete(TranslationProject).where(TranslationProject.id == project.id),
        execution_options={"synchronize_session": False},
    )


def cleanup_deleted_project_vectors(
    app: Any,
    artifacts: ProjectDeletionArtifacts,
) -> None:
    """Best-effort physical cleanup after the database deletion has committed."""

    try:
        cleanup_embedding_vector_identities(app, artifacts.embedding_identities)
    except Exception:  # noqa: BLE001 - DB deletion is already authoritative.
        logger.exception(
            "Failed to delete Translator embedding vector tables for project %s",
            artifacts.project_id,
        )
    if not artifacts.vector_ids:
        return
    project_identity = SimpleNamespace(
        id=artifacts.project_id,
        user_id=artifacts.user_id,
        source_lang=artifacts.source_lang,
        target_lang=artifacts.target_lang,
    )
    try:
        store = translation_memory_store(app, project_identity)
        if store is not None:
            store.delete(list(artifacts.vector_ids))
    except Exception:  # noqa: BLE001 - DB deletion is already authoritative.
        logger.exception(
            "Failed to delete stale translation vectors for project %s",
            artifacts.project_id,
        )


__all__ = [
    "ProjectDeletionArtifacts",
    "cleanup_deleted_project_vectors",
    "collect_project_deletion_artifacts",
    "delete_project_aggregate",
]
