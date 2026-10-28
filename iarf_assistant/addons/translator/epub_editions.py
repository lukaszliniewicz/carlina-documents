"""Owner-scoped persistence for reusable Translator EPUB recipes."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import cast

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from carlina.content_codec import ContentPolicyError
from carlina.core.epub_export import (
    effective_epub_recipe,
    epub_value_sha256,
    normalize_epub_recipe,
)
from carlina.core.utc import utc_now_db
from iarf_assistant.addons.translator.block_workspace_api import (
    BlockWorkspaceIntegrityError,
    load_block_source_plan,
)
from iarf_assistant.addons.translator.models import (
    TranslationEpubEdition,
    TranslationProject,
    TranslationTargetBlockActive,
)
from iarf_assistant.extensions import db

_REVISION_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_VERSION = 2_147_483_646


class EpubEditionStoreError(ValueError):
    """A bounded, transport-neutral EPUB recipe storage failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def current_source_revision(parent: object) -> str:
    """Hash the verified active plan, target pointers and saved export context."""

    if not isinstance(parent, TranslationProject):
        raise EpubEditionStoreError("epub-edition-not-ready")
    try:
        workspace = load_block_source_plan(parent)
        record = workspace.record
        typed_target_rows = cast(
            list[TranslationTargetBlockActive],
            TranslationTargetBlockActive.query.filter_by(source_plan_id=record.id)
            .order_by(TranslationTargetBlockActive.block_id)
            .all(),
        )
        targets: list[dict[str, object]] = [
            {
                "block_id": target.block_id,
                "revision_id": target.revision_id,
                "version": target.version,
                "pair_receipt": target.pair_receipt,
            }
            for target in typed_target_rows
        ]
        from iarf_assistant.addons.translator.numbering_sync import numbering_revision

        settings = _string_keyed_mapping(parent.settings) or {}
        raw_export_metadata = settings.get("export_metadata")
        export_metadata = _metadata_mapping(raw_export_metadata)
        return epub_value_sha256(
            {
                "source_plan_id": record.id,
                "source_plan_hash": record.plan_hash,
                "active_plan_version": workspace.active_version,
                "targets": targets,
                "numbering_revision": numbering_revision(parent.id),
                "metadata": export_metadata or {},
                "target_lang": parent.target_lang,
            }
        )
    except (
        BlockWorkspaceIntegrityError,
        ContentPolicyError,
        UnicodeError,
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
        AttributeError,
    ):
        raise EpubEditionStoreError("epub-edition-not-ready") from None
    except SQLAlchemyError as exc:
        raise EpubEditionStoreError("epub-edition-not-ready") from exc


def _string_keyed_mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    entries = cast(Mapping[object, object], value)
    result: dict[str, object] = {}
    for key, item in entries.items():
        if not isinstance(key, str):
            return None
        result[key] = item
    return result


def _metadata_mapping(value: object) -> dict[str, object]:
    if not value:
        return {}
    metadata = _string_keyed_mapping(value)
    if metadata is None:
        raise EpubEditionStoreError("epub-edition-not-ready")
    return metadata


def _validate_scope(owner_id: int, project_id: str) -> None:
    if (
        type(owner_id) is not int
        or not 1 <= owner_id <= 2_147_483_647
        or type(project_id) is not str
        or not project_id
        or len(project_id) > 36
    ):
        raise EpubEditionStoreError("epub-edition-contract")


def _validate_version(version: int) -> None:
    if type(version) is not int or not 0 <= version <= _MAX_VERSION:
        raise EpubEditionStoreError("epub-edition-contract")


def _validate_revision(revision: str) -> None:
    if type(revision) is not str or not _REVISION_RE.fullmatch(revision):
        raise EpubEditionStoreError("epub-edition-contract")


def _owned_project(owner_id: int, project_id: str) -> TranslationProject:
    try:
        project = db.session.get(TranslationProject, project_id)
    except SQLAlchemyError:
        raise EpubEditionStoreError("epub-edition-not-ready") from None
    if project is None or project.user_id != owner_id:
        raise EpubEditionStoreError("epub-edition-not-found")
    return project


def _canonical_recipe(value: object) -> dict[str, object]:
    try:
        return normalize_epub_recipe(value)
    except TypeError, ValueError:
        raise EpubEditionStoreError("epub-edition-contract") from None


def _response(
    row: TranslationEpubEdition | None, current_revision: str | None
) -> dict[str, object]:
    stored_recipe = None
    if row is not None and row.recipe is not None:
        stored_recipe = _canonical_recipe(row.recipe)
    stale = bool(
        stored_recipe is not None
        and row is not None
        and row.source_revision != current_revision
    )
    effective = (
        effective_epub_recipe(stored_recipe, stale=stale)
        if stored_recipe is not None
        else None
    )
    return {
        "version": row.version if row is not None else 0,
        "recipe": stored_recipe,
        "effective_recipe": effective,
        "source_revision": row.source_revision if row is not None else None,
        "current_source_revision": current_revision,
        "stale": stale,
    }


def _current_revision_or_none(parent: TranslationProject) -> str | None:
    try:
        return current_source_revision(parent)
    except EpubEditionStoreError as exc:
        if exc.code == "epub-edition-not-ready":
            return None
        raise


def _verify_save_source(
    *, owner_id: int, project_id: str, expected_source_revision: str
) -> None:
    """Re-read the bound source after this transaction has acquired its write lock."""

    db.session.flush()
    db.session.expire_all()
    project = _owned_project(owner_id, project_id)
    try:
        current_revision = current_source_revision(project)
    except EpubEditionStoreError as exc:
        if exc.code == "epub-edition-not-ready" and not isinstance(
            exc.__cause__, SQLAlchemyError
        ):
            raise EpubEditionStoreError("epub-edition-source-stale") from None
        raise
    if current_revision != expected_source_revision:
        raise EpubEditionStoreError("epub-edition-source-stale")


def get_epub_edition(*, owner_id: int, project_id: str) -> dict[str, object]:
    _validate_scope(owner_id, project_id)
    project = _owned_project(owner_id, project_id)
    current_revision = _current_revision_or_none(project)
    try:
        row = db.session.get(TranslationEpubEdition, project_id)
    except SQLAlchemyError:
        raise EpubEditionStoreError("epub-edition-not-ready") from None
    if row is not None and row.owner_user_id != owner_id:
        raise EpubEditionStoreError("epub-edition-not-found")
    try:
        return _response(row, current_revision)
    except EpubEditionStoreError:
        raise
    except TypeError, ValueError:
        raise EpubEditionStoreError("epub-edition-not-ready") from None


def save_epub_edition(
    *,
    owner_id: int,
    project_id: str,
    recipe: object,
    expected_version: int,
    expected_source_revision: str,
) -> dict[str, object]:
    _validate_scope(owner_id, project_id)
    _validate_version(expected_version)
    _validate_revision(expected_source_revision)
    project = _owned_project(owner_id, project_id)
    current_revision = current_source_revision(project)
    if expected_source_revision != current_revision:
        raise EpubEditionStoreError("epub-edition-source-stale")
    canonical_recipe = _canonical_recipe(recipe)
    try:
        row = db.session.get(TranslationEpubEdition, project_id)
    except SQLAlchemyError:
        raise EpubEditionStoreError("epub-edition-not-ready") from None

    if expected_version == 0:
        if row is not None:
            raise EpubEditionStoreError("epub-edition-conflict")
        row = TranslationEpubEdition()
        row.project_id = project_id
        row.owner_user_id = owner_id
        row.version = 1
        row.source_revision = current_revision
        row.recipe = canonical_recipe
        row.updated_at = utc_now_db()
        db.session.add(row)
        try:
            db.session.flush()
            _verify_save_source(
                owner_id=owner_id,
                project_id=project_id,
                expected_source_revision=expected_source_revision,
            )
            db.session.commit()
        except EpubEditionStoreError:
            db.session.rollback()
            raise
        except IntegrityError:
            db.session.rollback()
            raise EpubEditionStoreError("epub-edition-conflict") from None
        except SQLAlchemyError:
            db.session.rollback()
            raise EpubEditionStoreError("epub-edition-not-ready") from None
        except Exception:
            db.session.rollback()
            raise
    else:
        if (
            row is None
            or row.owner_user_id != owner_id
            or row.version != expected_version
        ):
            raise EpubEditionStoreError("epub-edition-conflict")
        if (
            row.recipe is not None
            and _canonical_recipe(row.recipe) == canonical_recipe
            and row.source_revision == current_revision
        ):
            try:
                unchanged = (
                    db.session.query(TranslationEpubEdition)
                    .filter_by(
                        project_id=project_id,
                        owner_user_id=owner_id,
                        version=expected_version,
                    )
                    .update(
                        {TranslationEpubEdition.version: expected_version},
                        synchronize_session=False,
                    )
                )
                if unchanged != 1:
                    raise EpubEditionStoreError("epub-edition-conflict")
                _verify_save_source(
                    owner_id=owner_id,
                    project_id=project_id,
                    expected_source_revision=expected_source_revision,
                )
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                raise EpubEditionStoreError("epub-edition-conflict") from None
            except EpubEditionStoreError:
                db.session.rollback()
                raise
            except SQLAlchemyError:
                db.session.rollback()
                raise EpubEditionStoreError("epub-edition-not-ready") from None
            except Exception:
                db.session.rollback()
                raise
            return get_epub_edition(owner_id=owner_id, project_id=project_id)
        try:
            changed = (
                db.session.query(TranslationEpubEdition)
                .filter_by(
                    project_id=project_id,
                    owner_user_id=owner_id,
                    version=expected_version,
                )
                .update(
                    {
                        TranslationEpubEdition.version: expected_version + 1,
                        TranslationEpubEdition.source_revision: current_revision,
                        TranslationEpubEdition.recipe: canonical_recipe,
                        TranslationEpubEdition.updated_at: utc_now_db(),
                    },
                    synchronize_session=False,
                )
            )
            if changed != 1:
                raise EpubEditionStoreError("epub-edition-conflict")
            _verify_save_source(
                owner_id=owner_id,
                project_id=project_id,
                expected_source_revision=expected_source_revision,
            )
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            raise EpubEditionStoreError("epub-edition-conflict") from None
        except EpubEditionStoreError:
            db.session.rollback()
            raise
        except SQLAlchemyError:
            db.session.rollback()
            raise EpubEditionStoreError("epub-edition-not-ready") from None
        except Exception:
            db.session.rollback()
            raise
    return get_epub_edition(owner_id=owner_id, project_id=project_id)


def clear_epub_edition(
    *, owner_id: int, project_id: str, expected_version: int
) -> dict[str, object]:
    _validate_scope(owner_id, project_id)
    _validate_version(expected_version)
    project = _owned_project(owner_id, project_id)
    current_revision = _current_revision_or_none(project)
    try:
        row = db.session.get(TranslationEpubEdition, project_id)
    except SQLAlchemyError:
        raise EpubEditionStoreError("epub-edition-not-ready") from None
    if expected_version == 0 and row is None:
        return _response(None, current_revision)
    if row is None or row.owner_user_id != owner_id or row.version != expected_version:
        raise EpubEditionStoreError("epub-edition-conflict")
    try:
        changed = (
            db.session.query(TranslationEpubEdition)
            .filter_by(
                project_id=project_id,
                owner_user_id=owner_id,
                version=expected_version,
            )
            .update(
                {
                    TranslationEpubEdition.version: expected_version + 1,
                    TranslationEpubEdition.source_revision: None,
                    TranslationEpubEdition.recipe: None,
                    TranslationEpubEdition.updated_at: utc_now_db(),
                },
                synchronize_session=False,
            )
        )
        if changed != 1:
            db.session.rollback()
            raise EpubEditionStoreError("epub-edition-conflict")
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise EpubEditionStoreError("epub-edition-conflict") from None
    except EpubEditionStoreError:
        raise
    except SQLAlchemyError:
        db.session.rollback()
        raise EpubEditionStoreError("epub-edition-not-ready") from None
    except Exception:
        db.session.rollback()
        raise
    return get_epub_edition(owner_id=owner_id, project_id=project_id)
