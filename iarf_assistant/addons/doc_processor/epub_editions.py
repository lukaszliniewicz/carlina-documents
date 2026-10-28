"""Owner-scoped persistence for reusable Document Processor EPUB recipes."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from importlib import import_module
from typing import cast

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from carlina.content_codec import ContentPolicyError
from carlina.core.epub_export import (
    effective_epub_recipe,
    epub_value_sha256,
    normalize_epub_recipe,
)
from carlina.core.utc import utc_now_db
from iarf_assistant.addons.doc_processor.models import (
    DocumentEpubEdition,
    ProcessedDocument,
)
from iarf_assistant.extensions import db

_REVISION_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_VERSION = 2_147_483_646
get_canonical_document_metadata: Callable[[ProcessedDocument], object] = cast(
    Callable[[ProcessedDocument], object],
    import_module(
        "iarf_assistant.addons.doc_processor.metadata_service"
    ).get_canonical_document_metadata,
)
_canonical_metadata = get_canonical_document_metadata


class EpubEditionStoreError(ValueError):
    """A bounded, transport-neutral EPUB recipe storage failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def current_source_revision(parent: object) -> str:
    """Hash only the completed HTML and its canonical publication metadata."""

    if (
        not isinstance(parent, ProcessedDocument)
        or parent.status != "completed"
        or type(parent.output_html) is not str
        or not parent.output_html.strip()
    ):
        raise EpubEditionStoreError("epub-edition-not-ready")
    try:
        html_sha256 = hashlib.sha256(parent.output_html.encode("utf-8")).hexdigest()
        raw_metadata = _canonical_metadata(parent)
        metadata = _string_keyed_mapping(raw_metadata)
        if metadata is None:
            raise EpubEditionStoreError("epub-edition-not-ready")
        return epub_value_sha256({"html_sha256": html_sha256, "metadata": metadata})
    except EpubEditionStoreError:
        raise
    except SQLAlchemyError as exc:
        raise EpubEditionStoreError("epub-edition-not-ready") from exc
    except (
        ContentPolicyError,
        UnicodeError,
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
    ):
        raise EpubEditionStoreError("epub-edition-not-ready") from None


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


def _validate_scope(owner_id: int, document_id: str) -> None:
    if (
        type(owner_id) is not int
        or not 1 <= owner_id <= 2_147_483_647
        or type(document_id) is not str
        or not document_id
        or len(document_id) > 36
    ):
        raise EpubEditionStoreError("epub-edition-contract")


def _validate_version(version: int) -> None:
    if type(version) is not int or not 0 <= version <= _MAX_VERSION:
        raise EpubEditionStoreError("epub-edition-contract")


def _validate_revision(revision: str) -> None:
    if type(revision) is not str or not _REVISION_RE.fullmatch(revision):
        raise EpubEditionStoreError("epub-edition-contract")


def _owned_document(owner_id: int, document_id: str) -> ProcessedDocument:
    try:
        document = db.session.get(ProcessedDocument, document_id)
    except SQLAlchemyError:
        raise EpubEditionStoreError("epub-edition-not-ready") from None
    if document is None or document.user_id != owner_id:
        raise EpubEditionStoreError("epub-edition-not-found")
    return document


def _canonical_recipe(value: object) -> dict[str, object]:
    try:
        return normalize_epub_recipe(value)
    except TypeError, ValueError:
        raise EpubEditionStoreError("epub-edition-contract") from None


def _response(
    row: DocumentEpubEdition | None, current_revision: str | None
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


def _current_revision_or_none(parent: ProcessedDocument) -> str | None:
    try:
        return current_source_revision(parent)
    except EpubEditionStoreError as exc:
        if exc.code == "epub-edition-not-ready":
            return None
        raise


def _verify_save_source(
    *, owner_id: int, document_id: str, expected_source_revision: str
) -> None:
    """Re-read the bound source after this transaction has acquired its write lock."""

    db.session.flush()
    db.session.expire_all()
    document = _owned_document(owner_id, document_id)
    try:
        current_revision = current_source_revision(document)
    except EpubEditionStoreError as exc:
        if exc.code == "epub-edition-not-ready" and not isinstance(
            exc.__cause__, SQLAlchemyError
        ):
            raise EpubEditionStoreError("epub-edition-source-stale") from None
        raise
    if current_revision != expected_source_revision:
        raise EpubEditionStoreError("epub-edition-source-stale")


def get_epub_edition(*, owner_id: int, document_id: str) -> dict[str, object]:
    _validate_scope(owner_id, document_id)
    document = _owned_document(owner_id, document_id)
    current_revision = _current_revision_or_none(document)
    try:
        row = db.session.get(DocumentEpubEdition, document_id)
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
    document_id: str,
    recipe: object,
    expected_version: int,
    expected_source_revision: str,
) -> dict[str, object]:
    _validate_scope(owner_id, document_id)
    _validate_version(expected_version)
    _validate_revision(expected_source_revision)
    document = _owned_document(owner_id, document_id)
    current_revision = current_source_revision(document)
    if expected_source_revision != current_revision:
        raise EpubEditionStoreError("epub-edition-source-stale")
    canonical_recipe = _canonical_recipe(recipe)
    try:
        row = db.session.get(DocumentEpubEdition, document_id)
    except SQLAlchemyError:
        raise EpubEditionStoreError("epub-edition-not-ready") from None

    if expected_version == 0:
        if row is not None:
            raise EpubEditionStoreError("epub-edition-conflict")
        row = DocumentEpubEdition()
        row.document_id = document_id
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
                document_id=document_id,
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
                    db.session.query(DocumentEpubEdition)
                    .filter_by(
                        document_id=document_id,
                        owner_user_id=owner_id,
                        version=expected_version,
                    )
                    .update(
                        {DocumentEpubEdition.version: expected_version},
                        synchronize_session=False,
                    )
                )
                if unchanged != 1:
                    raise EpubEditionStoreError("epub-edition-conflict")
                _verify_save_source(
                    owner_id=owner_id,
                    document_id=document_id,
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
            return get_epub_edition(owner_id=owner_id, document_id=document_id)
        try:
            changed = (
                db.session.query(DocumentEpubEdition)
                .filter_by(
                    document_id=document_id,
                    owner_user_id=owner_id,
                    version=expected_version,
                )
                .update(
                    {
                        DocumentEpubEdition.version: expected_version + 1,
                        DocumentEpubEdition.source_revision: current_revision,
                        DocumentEpubEdition.recipe: canonical_recipe,
                        DocumentEpubEdition.updated_at: utc_now_db(),
                    },
                    synchronize_session=False,
                )
            )
            if changed != 1:
                raise EpubEditionStoreError("epub-edition-conflict")
            _verify_save_source(
                owner_id=owner_id,
                document_id=document_id,
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
    return get_epub_edition(owner_id=owner_id, document_id=document_id)


def clear_epub_edition(
    *, owner_id: int, document_id: str, expected_version: int
) -> dict[str, object]:
    _validate_scope(owner_id, document_id)
    _validate_version(expected_version)
    document = _owned_document(owner_id, document_id)
    current_revision = _current_revision_or_none(document)
    try:
        row = db.session.get(DocumentEpubEdition, document_id)
    except SQLAlchemyError:
        raise EpubEditionStoreError("epub-edition-not-ready") from None
    if expected_version == 0 and row is None:
        return _response(None, current_revision)
    if row is None or row.owner_user_id != owner_id or row.version != expected_version:
        raise EpubEditionStoreError("epub-edition-conflict")
    try:
        changed = (
            db.session.query(DocumentEpubEdition)
            .filter_by(
                document_id=document_id,
                owner_user_id=owner_id,
                version=expected_version,
            )
            .update(
                {
                    DocumentEpubEdition.version: expected_version + 1,
                    DocumentEpubEdition.source_revision: None,
                    DocumentEpubEdition.recipe: None,
                    DocumentEpubEdition.updated_at: utc_now_db(),
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
    return get_epub_edition(owner_id=owner_id, document_id=document_id)
