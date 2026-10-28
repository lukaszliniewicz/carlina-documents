"""Typed host callbacks used by the Translator MCP registration boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol

from flask import Flask

if TYPE_CHECKING:
    from ...core.executor_profile import ExecutorProfile


class TranslatorExecutionStarter(Protocol):
    """Host callback for Translator-owned document-block admission."""

    def __call__(
        self,
        app: Flask,
        *,
        owner_user_id: int,
        project_id: str,
        mode: str,
        executor_profile: ExecutorProfile | None = None,
        research_dossier_revision_id: str | None = None,
        context_mode: Literal["self_contained", "retained"] | None = None,
    ) -> dict[str, Any]: ...


class SourcePageRenderer(Protocol):
    """Render source pages already resolved from Translator provenance."""

    def __call__(
        self,
        owner_user_id: int,
        document_id: str,
        page_numbers: list[int],
        *,
        max_edge: int,
    ) -> dict[str, Any]: ...
