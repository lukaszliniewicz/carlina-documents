from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import re
from typing import Any, Callable
import uuid

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified

from ..extensions import db
from ..models import RuntimeCollection, RuntimeCollectionItem, WorkflowRun, WorkflowStep
from .encryption import keyed_lookup_candidates, keyed_lookup_digest
from carlina.core.utc import utc_iso, utc_now_db


COLLECTION_STATUSES = {"active", "completed", "archived"}
ITEM_STATUSES = {"pending", "leased", "completed", "failed", "skipped"}
ITEM_STATUS_ALIASES = {
    "queued": "pending",
    "claimed": "leased",
    "processing": "leased",
    "running": "leased",
    "waiting_for_approval": "leased",
    "waiting_for_review": "leased",
    "paused": "leased",
    "cancelled": "skipped",
    "canceled": "skipped",
}
MAX_COLLECTION_ITEMS_PER_UPSERT = 2_000
MAX_COLLECTION_PARTITIONS = 100
MAX_ITEM_ANNOTATIONS = 100


def backfill_collection_lookup_hashes(
    *,
    limit: int = 500,
    commit: bool = True,
) -> dict[str, int]:
    """Incrementally index legacy encrypted source/external identifiers."""

    rows = (
        RuntimeCollectionItem.query.filter(
            or_(
                and_(
                    RuntimeCollectionItem.source_lookup_hash.is_(None),
                    RuntimeCollectionItem.source_id.is_not(None),
                ),
                and_(
                    RuntimeCollectionItem.external_lookup_hash.is_(None),
                    RuntimeCollectionItem.external_id.is_not(None),
                ),
            )
        )
        .order_by(RuntimeCollectionItem.created_at.asc(), RuntimeCollectionItem.id.asc())
        .limit(max(1, min(int(limit), 5_000)))
        .all()
    )
    source_count = 0
    external_count = 0
    for row in rows:
        if row.source_lookup_hash is None and row.source_id is not None:
            row.source_lookup_hash = keyed_lookup_digest(
                str(row.source_id),
                purpose="runtime-collection-source-id",
            )
            source_count += 1
        if row.external_lookup_hash is None and row.external_id is not None:
            row.external_lookup_hash = keyed_lookup_digest(
                str(row.external_id),
                purpose="runtime-collection-external-id",
            )
            external_count += 1
    if commit and (source_count or external_count):
        db.session.commit()
    return {
        "rows_scanned": len(rows),
        "source_hashes_added": source_count,
        "external_hashes_added": external_count,
    }


def run_scheduled_collection_lookup_backfill(app: Any) -> dict[str, int]:
    with app.app_context():
        result = backfill_collection_lookup_hashes()
        app.logger.info("Collection lookup hash backfill result=%s", result)
        return result


def _utcnow() -> datetime:
    return utc_now_db()


def _utc_iso(value: datetime | None = None) -> str:
    return utc_iso(value)


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    except (TypeError, ValueError):
        return None


def _bounded_text(value: Any, limit: int) -> str | None:
    text = str(value or "").strip()
    return text[:limit] if text else None


def _string_values(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple, set)) else [value]
    result = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def normalize_item_status(value: Any, *, default: str = "pending") -> str:
    status = str(value or default).strip().lower()
    status = ITEM_STATUS_ALIASES.get(status, status)
    return status if status in ITEM_STATUSES else default


def safe_collection_key(value: Any) -> str:
    text = str(value or f"collection-{uuid.uuid4()}").strip().lower()
    text = re.sub(r"[^a-z0-9_.-]+", "_", text).strip("_")
    return text[:96] or f"collection-{uuid.uuid4()}"


def _safe_item_key(value: Any, identity_hash: str) -> str:
    text = str(value or "").strip()
    if text:
        text = re.sub(r"[\x00-\x1f]+", "_", text)
        return text[:128]
    return f"item_{identity_hash[:16]}"


def _json_hash(value: Any, *, algorithm: str = "sha256") -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return getattr(hashlib, algorithm)(payload).hexdigest()


def collection_item_identity(raw_item: dict[str, Any]) -> str:
    """Return the stable identity using the Stage 6 precedence contract."""
    explicit = _bounded_text(raw_item.get("identity"), 2_000)
    if explicit:
        return f"identity:{explicit}"

    provider = str(raw_item.get("provider") or "").strip().lower()
    external_id = _bounded_text(raw_item.get("external_id"), 1_000)
    source_id = _bounded_text(raw_item.get("source_id"), 1_000)
    if provider and external_id:
        return f"provider:{provider}:{external_id}"
    if provider and source_id:
        return f"provider:{provider}:{source_id}"

    canonical_uri = _bounded_text(
        raw_item.get("canonical_uri")
        or raw_item.get("canonical_url")
        or raw_item.get("web_view_link"),
        4_000,
    )
    if canonical_uri:
        return f"canonical_uri:{canonical_uri}"

    path = _bounded_text(raw_item.get("path") or raw_item.get("file_path"), 4_000)
    if path:
        return f"path:{path.replace(chr(92), '/').lower()}"

    uri = _bounded_text(raw_item.get("uri") or raw_item.get("url"), 4_000)
    if uri:
        return f"uri:{uri}"

    content_hash = _bounded_text(raw_item.get("content_hash"), 256)
    if content_hash:
        return f"content_hash:{content_hash.lower()}"
    if source_id:
        return f"source_id:{source_id}"
    if external_id:
        return f"external_id:{external_id}"
    if raw_item.get("id") is not None:
        return f"id:{str(raw_item['id']).strip()}"
    return f"content:{_json_hash(raw_item)}"


def _rendered_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("rendered", "text", "value", "name"):
            if isinstance(value.get(key), str):
                return value[key]
    return None


def _adapt_generic(raw_item: dict[str, Any]) -> dict[str, Any]:
    return dict(raw_item)


def _adapt_wordpress_post(raw_item: dict[str, Any]) -> dict[str, Any]:
    external_id = raw_item.get("external_id") or raw_item.get("post_id") or raw_item.get("id")
    uri = (
        raw_item.get("canonical_uri")
        or raw_item.get("link")
        or raw_item.get("url")
        or (f"wordpress://posts/{external_id}" if external_id is not None else None)
    )
    title = _rendered_text(raw_item.get("title")) or raw_item.get("name")
    content = (
        _rendered_text(raw_item.get("content"))
        or _rendered_text(raw_item.get("excerpt"))
        or raw_item.get("text")
    )
    metadata = dict(raw_item.get("metadata")) if isinstance(raw_item.get("metadata"), dict) else {}
    for key in ("status", "type", "slug", "date", "modified"):
        if raw_item.get(key) is not None:
            metadata[key] = raw_item[key]
    adapted = {
        **raw_item,
        "provider": "wordpress",
        "source_type": "wordpress_post",
        "external_id": external_id,
        "source_id": raw_item.get("source_id") or external_id,
        "canonical_uri": uri,
        "uri": uri,
        "title": title,
        "metadata": metadata,
        "source_ref": {
            "provider": "wordpress",
            "source_type": "wordpress_post",
            "external_id": str(external_id) if external_id is not None else None,
            "canonical_uri": uri,
        },
    }
    if content:
        adapted["content_ref"] = {
            "text": str(content),
            "inline_field": "content",
        }
    return adapted


def _adapt_drive_file(raw_item: dict[str, Any]) -> dict[str, Any]:
    external_id = raw_item.get("external_id") or raw_item.get("file_id") or raw_item.get("id")
    uri = (
        raw_item.get("canonical_uri")
        or raw_item.get("webViewLink")
        or raw_item.get("web_view_link")
        or raw_item.get("url")
        or (f"gdrive://files/{external_id}" if external_id is not None else None)
    )
    title = raw_item.get("name") or _rendered_text(raw_item.get("title"))
    content = (
        raw_item.get("text")
        or raw_item.get("content")
        or raw_item.get("description")
    )
    metadata = dict(raw_item.get("metadata")) if isinstance(raw_item.get("metadata"), dict) else {}
    field_map = {
        "mimeType": "mime_type",
        "mime_type": "mime_type",
        "modifiedTime": "modified_time",
        "modified_time": "modified_time",
        "size": "size",
        "md5Checksum": "md5_checksum",
        "md5_checksum": "md5_checksum",
    }
    for source_key, target_key in field_map.items():
        if raw_item.get(source_key) is not None:
            metadata[target_key] = raw_item[source_key]
    adapted = {
        **raw_item,
        "provider": "googledrive",
        "source_type": "drive_file",
        "external_id": external_id,
        "source_id": raw_item.get("source_id") or external_id,
        "canonical_uri": uri,
        "uri": uri,
        "title": title,
        "metadata": metadata,
        "source_ref": {
            "provider": "googledrive",
            "source_type": "drive_file",
            "external_id": str(external_id) if external_id is not None else None,
            "canonical_uri": uri,
        },
    }
    if content:
        adapted["content_ref"] = {
            "text": str(content),
            "inline_field": "content",
        }
    return adapted


COLLECTION_ADAPTERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "generic": _adapt_generic,
    "wordpress.posts": _adapt_wordpress_post,
    "gdrive.files": _adapt_drive_file,
}
COLLECTION_ADAPTER_ALIASES = {
    "wordpress": "wordpress.posts",
    "wordpress_posts": "wordpress.posts",
    "wp_posts": "wordpress.posts",
    "drive": "gdrive.files",
    "googledrive": "gdrive.files",
    "googledrive_files": "gdrive.files",
    "drive_files": "gdrive.files",
}


def normalize_adapter_id(value: Any) -> str:
    adapter_id = str(value or "generic").strip().lower()
    adapter_id = COLLECTION_ADAPTER_ALIASES.get(adapter_id, adapter_id)
    if adapter_id not in COLLECTION_ADAPTERS:
        raise ValueError(
            f"Unsupported collection adapter '{value}'. "
            f"Available adapters: {', '.join(sorted(COLLECTION_ADAPTERS))}."
        )
    return adapter_id


def adapt_collection_items(
    adapter_id: Any,
    raw_items: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    normalized_adapter_id = normalize_adapter_id(adapter_id)
    adapter = COLLECTION_ADAPTERS[normalized_adapter_id]
    return normalized_adapter_id, [adapter(item) for item in raw_items]


def normalize_collection_item(
    raw_item: dict[str, Any],
    *,
    now: str | None = None,
) -> dict[str, Any]:
    identity = collection_item_identity(raw_item)
    identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    item_key = _safe_item_key(
        raw_item.get("item_key") or raw_item.get("id"),
        identity_hash,
    )
    metadata = dict(raw_item.get("metadata")) if isinstance(raw_item.get("metadata"), dict) else {}
    content_ref = (
        dict(raw_item.get("content_ref"))
        if isinstance(raw_item.get("content_ref"), dict)
        else {}
    )
    if not content_ref:
        for key in ("text", "content", "body", "excerpt", "summary"):
            value = raw_item.get(key)
            rendered = _rendered_text(value)
            if rendered:
                content_ref = {"text": rendered, "inline_field": key}
                break
    resource_reference_ids = _string_values(
        raw_item.get("resource_reference_ids")
        or raw_item.get("resource_reference_id")
        or metadata.get("resource_reference_ids")
    )
    if resource_reference_ids:
        metadata["resource_reference_ids"] = resource_reference_ids
    fingerprint = _bounded_text(raw_item.get("fingerprint"), 64)
    if not fingerprint:
        fingerprint = _json_hash({
            "identity": identity,
            "content_hash": raw_item.get("content_hash"),
            "text": content_ref.get("text"),
            "uri": raw_item.get("canonical_uri") or raw_item.get("uri"),
        })
    source_ref = (
        dict(raw_item.get("source_ref"))
        if isinstance(raw_item.get("source_ref"), dict)
        else {
            "provider": raw_item.get("provider"),
            "source_type": raw_item.get("source_type"),
            "external_id": raw_item.get("external_id"),
            "source_id": raw_item.get("source_id"),
            "canonical_uri": raw_item.get("canonical_uri"),
        }
    )
    timestamp = now or _utc_iso()
    return {
        "item_key": item_key,
        "identity": identity,
        "identity_hash": identity_hash,
        "fingerprint": fingerprint,
        "parent_item_key": _bounded_text(
            raw_item.get("parent_item_id") or item_key,
            128,
        ),
        "chunk_index": _positive_int(raw_item.get("chunk_index")),
        "chunk_count": _positive_int(raw_item.get("chunk_count")),
        "source_type": _bounded_text(raw_item.get("source_type"), 100),
        "source_id": _bounded_text(raw_item.get("source_id"), 1_000),
        "provider": _bounded_text(raw_item.get("provider"), 100),
        "external_id": _bounded_text(raw_item.get("external_id"), 1_000),
        "uri": _bounded_text(raw_item.get("uri"), 8_000),
        "canonical_uri": _bounded_text(
            raw_item.get("canonical_uri") or raw_item.get("canonical_url"),
            8_000,
        ),
        "title": _bounded_text(raw_item.get("title"), 512),
        "content_hash": _bounded_text(raw_item.get("content_hash"), 128),
        "content_ref": content_ref,
        "source_ref": source_ref,
        "resource_reference_ids": resource_reference_ids,
        "estimated_tokens": _positive_int(
            raw_item.get("estimated_tokens")
            or metadata.get("estimated_tokens")
        ),
        "status": normalize_item_status(raw_item.get("status")),
        "status_explicit": raw_item.get("status") is not None,
        "attempt_count": int(raw_item.get("attempt_count") or raw_item.get("claim_count") or 0),
        "failure_count": int(raw_item.get("failure_count") or raw_item.get("retry_count") or 0),
        "lease_token": _bounded_text(
            raw_item.get("lease_token") or raw_item.get("claim_id"),
            36,
        ),
        "lease_owner": _bounded_text(raw_item.get("lease_owner"), 255),
        "lease_expires_at": _parse_iso(raw_item.get("lease_expires_at")),
        "workflow_step_id": _bounded_text(raw_item.get("workflow_step_id"), 36),
        "annotations": (
            list(raw_item.get("annotations"))
            if isinstance(raw_item.get("annotations"), list)
            else []
        ),
        "result_ref": (
            dict(raw_item.get("result_ref"))
            if isinstance(raw_item.get("result_ref"), dict)
            else (
                {"inline": raw_item.get("result")}
                if raw_item.get("result") is not None
                else {}
            )
        ),
        "error": _bounded_text(raw_item.get("error"), 8_000),
        "metadata": metadata,
        "created_at": _parse_iso(raw_item.get("created_at")),
        "updated_at": _parse_iso(raw_item.get("updated_at")) or _parse_iso(timestamp),
    }


def estimate_collection_item_tokens(
    item: dict[str, Any],
    *,
    chars_per_token: float,
    include_annotations: bool,
) -> int:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    content_ref = item.get("content_ref") if isinstance(item.get("content_ref"), dict) else {}
    for value in (
        item.get("estimated_tokens"),
        metadata.get("estimated_tokens"),
        metadata.get("token_count"),
        content_ref.get("estimated_tokens"),
        content_ref.get("token_count"),
    ):
        explicit = _positive_int(value)
        if explicit:
            return explicit
    text_parts = []
    for key in (
        "title",
        "uri",
        "canonical_uri",
        "source_id",
        "provider",
        "external_id",
        "content_hash",
    ):
        if item.get(key):
            text_parts.append(str(item[key]))
    for key in ("text", "preview", "path", "artifact_id", "locator", "uri"):
        if isinstance(content_ref.get(key), str):
            text_parts.append(content_ref[key])
    if include_annotations and item.get("annotations"):
        text_parts.append(json.dumps(item["annotations"], default=str))
    char_count = sum(len(part) for part in text_parts)
    return max(1, int((char_count + chars_per_token - 1) / chars_per_token))


class RuntimeCollectionService:
    """Canonical row-backed collection ledger with atomic lease fencing."""

    def __init__(
        self,
        *,
        run: WorkflowRun,
        owner_user_id: int | None,
        workflow_step_id: str | None = None,
    ):
        self.run = run
        self.run_id = str(run.id)
        self.owner_user_id = (
            int(owner_user_id)
            if owner_user_id is not None
            else run.created_by_user_id
        )
        self.workflow_step_id = str(workflow_step_id) if workflow_step_id else None
        self._migrate_legacy_if_needed()

    @staticmethod
    def adapter_cards() -> list[dict[str, Any]]:
        return [
            {
                "id": "generic",
                "source_type": "generic",
                "identity_order": [
                    "explicit identity",
                    "provider/external id",
                    "canonical URI or path",
                    "content hash",
                ],
            },
            {
                "id": "wordpress.posts",
                "source_type": "wordpress_post",
                "provider": "wordpress",
            },
            {
                "id": "gdrive.files",
                "source_type": "drive_file",
                "provider": "googledrive",
            },
        ]

    def _migrate_legacy_if_needed(self) -> None:
        existing = RuntimeCollection.query.filter_by(
            workflow_run_id=self.run_id
        ).first()
        if existing:
            return
        state = self.run.state_json if isinstance(self.run.state_json, dict) else {}
        legacy = (
            state.get("runtime_collections")
            if isinstance(state.get("runtime_collections"), list)
            else []
        )
        if not legacy:
            return
        for raw_collection in legacy:
            if not isinstance(raw_collection, dict):
                continue
            adapter_id = "generic"
            try:
                adapter_id = normalize_adapter_id(
                    raw_collection.get("adapter_id") or raw_collection.get("kind")
                )
            except ValueError:
                adapter_id = "generic"
            collection = RuntimeCollection(
                workflow_run_id=self.run_id,
                owner_user_id=self.owner_user_id,
                collection_key=safe_collection_key(raw_collection.get("id")),
                title=str(
                    raw_collection.get("title")
                    or raw_collection.get("id")
                    or "Migrated collection"
                )[:255],
                objective=str(
                    raw_collection.get("objective")
                    or raw_collection.get("description")
                    or ""
                ),
                kind=str(raw_collection.get("kind") or "items")[:100],
                adapter_id=adapter_id,
                status=str(raw_collection.get("status") or "active")[:32],
                identity_keys=_string_values(raw_collection.get("identity_keys")),
                schema_json=(
                    raw_collection.get("schema")
                    if isinstance(raw_collection.get("schema"), dict)
                    else {}
                ),
                partitions_json=(
                    raw_collection.get("partitions")
                    if isinstance(raw_collection.get("partitions"), list)
                    else []
                ),
                metadata_json=(
                    raw_collection.get("metadata")
                    if isinstance(raw_collection.get("metadata"), dict)
                    else {}
                ),
                storage_version=1,
                created_at=_parse_iso(raw_collection.get("created_at")) or _utcnow(),
                updated_at=_parse_iso(raw_collection.get("updated_at")) or _utcnow(),
            )
            db.session.add(collection)
            db.session.flush()
            self._upsert_rows(
                collection,
                [
                    item
                    for item in raw_collection.get("items") or []
                    if isinstance(item, dict)
                ],
                adapter_id="generic",
                preserve_runtime=True,
            )
        compact_state = dict(state)
        compact_state.pop("runtime_collections", None)
        compact_state["runtime_collection_storage"] = "rows_v1"
        compact_state["runtime_collection_migrated_at"] = _utc_iso()
        self.run.state_json = compact_state
        flag_modified(self.run, "state_json")
        db.session.commit()

    def _query_collection(self, collection_id: Any) -> RuntimeCollection:
        target = str(collection_id or "").strip()
        if not target:
            raise ValueError("collection_id is required.")
        query = RuntimeCollection.query.filter_by(workflow_run_id=self.run_id)
        record = query.filter(
            or_(
                RuntimeCollection.id == target,
                RuntimeCollection.collection_key == target,
            )
        ).first()
        if not record:
            raise KeyError(f"Runtime collection '{collection_id}' was not found.")
        if (
            self.owner_user_id is not None
            and record.owner_user_id is not None
            and int(record.owner_user_id) != int(self.owner_user_id)
        ):
            raise PermissionError("Collection access denied.")
        return record

    def create(self, payload: dict[str, Any]) -> RuntimeCollection:
        key = safe_collection_key(
            payload.get("collection_id")
            or payload.get("id")
            or payload.get("title")
        )
        record = RuntimeCollection.query.filter_by(
            workflow_run_id=self.run_id,
            collection_key=key,
        ).first()
        adapter_id = normalize_adapter_id(
            payload.get("adapter_id")
            or payload.get("adapter")
            or (record.adapter_id if record else None)
            or "generic"
        )
        now = _utcnow()
        if record is None:
            record = RuntimeCollection(
                workflow_run_id=self.run_id,
                owner_user_id=self.owner_user_id,
                collection_key=key,
                title=str(payload.get("title") or key)[:255],
                objective=str(
                    payload.get("objective")
                    or payload.get("description")
                    or ""
                ),
                kind=str(payload.get("kind") or "items")[:100],
                adapter_id=adapter_id,
                status="active",
                identity_keys=_string_values(payload.get("identity_keys")),
                schema_json=(
                    payload.get("schema")
                    if isinstance(payload.get("schema"), dict)
                    else {}
                ),
                partitions_json=[],
                metadata_json=(
                    payload.get("metadata")
                    if isinstance(payload.get("metadata"), dict)
                    else {}
                ),
                storage_version=1,
                created_at=now,
                updated_at=now,
            )
            db.session.add(record)
        else:
            record.title = str(payload.get("title") or record.title or key)[:255]
            record.objective = str(
                payload.get("objective")
                or payload.get("description")
                or record.objective
                or ""
            )
            record.kind = str(payload.get("kind") or record.kind or "items")[:100]
            record.adapter_id = adapter_id
            if payload.get("identity_keys") is not None:
                record.identity_keys = _string_values(payload.get("identity_keys"))
            if isinstance(payload.get("schema"), dict):
                record.schema_json = payload["schema"]
            if isinstance(payload.get("metadata"), dict):
                record.metadata_json = {
                    **(
                        record.metadata_json
                        if isinstance(record.metadata_json, dict)
                        else {}
                    ),
                    **payload["metadata"],
                }
            record.updated_at = now
        state = dict(self.run.state_json) if isinstance(self.run.state_json, dict) else {}
        state["runtime_collection_storage"] = "rows_v1"
        self.run.state_json = state
        flag_modified(self.run, "state_json")
        db.session.commit()
        return record

    def get(
        self,
        collection_id: Any,
        *,
        include_items: bool = True,
        status: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        record = self._query_collection(collection_id)
        normalized_status = normalize_item_status(status) if status else None
        return record.to_dict(
            include_items=include_items,
            status=normalized_status,
            limit=limit,
        )

    def upsert(
        self,
        collection_id: Any,
        raw_items: Any,
        *,
        adapter_id: Any = None,
    ) -> dict[str, Any]:
        if not isinstance(raw_items, list):
            raise ValueError("items must be an array.")
        if len(raw_items) > MAX_COLLECTION_ITEMS_PER_UPSERT:
            raise ValueError(
                f"items contains {len(raw_items)} entries; "
                f"limit is {MAX_COLLECTION_ITEMS_PER_UPSERT}."
            )
        typed_items = [item for item in raw_items if isinstance(item, dict)]
        collection = self._query_collection(collection_id)
        selected_adapter, adapted = adapt_collection_items(
            adapter_id or collection.adapter_id,
            typed_items,
        )
        collection.adapter_id = selected_adapter
        result = self._upsert_rows(
            collection,
            adapted,
            adapter_id=selected_adapter,
            preserve_runtime=False,
        )
        collection.updated_at = _utcnow()
        db.session.commit()
        row_ids = result.pop("row_ids")
        item_rows = RuntimeCollectionItem.query.filter(
            RuntimeCollectionItem.id.in_(row_ids)
        ).all()
        items_by_id = {row.id: row for row in item_rows}
        result["items"] = [
            items_by_id[item_id].to_dict()
            for item_id in row_ids
            if item_id in items_by_id
        ]
        result["collection"] = collection.to_dict(include_items=False)
        result["adapter_id"] = selected_adapter
        return result

    def _upsert_rows(
        self,
        collection: RuntimeCollection,
        raw_items: list[dict[str, Any]],
        *,
        adapter_id: str,
        preserve_runtime: bool,
    ) -> dict[str, Any]:
        del adapter_id
        created_count = 0
        updated_count = 0
        row_ids = []
        for raw_item in raw_items:
            normalized = normalize_collection_item(raw_item)
            row = RuntimeCollectionItem.query.filter_by(
                collection_id=collection.id,
                identity_hash=normalized["identity_hash"],
            ).first()
            if row is None:
                item_key = normalized["item_key"]
                conflicting_key = RuntimeCollectionItem.query.filter_by(
                    collection_id=collection.id,
                    item_key=item_key,
                ).first()
                if conflicting_key is not None:
                    item_key = _safe_item_key(
                        f"{item_key[:108]}_{normalized['identity_hash'][:12]}",
                        normalized["identity_hash"],
                    )
                row = RuntimeCollectionItem(
                    collection_id=collection.id,
                    item_key=item_key,
                    identity_hash=normalized["identity_hash"],
                    identity_key=normalized["identity"],
                    fingerprint=normalized["fingerprint"],
                    created_at=normalized["created_at"] or _utcnow(),
                    updated_at=normalized["updated_at"] or _utcnow(),
                )
                self._merge_row(
                    row,
                    normalized,
                    preserve_runtime=preserve_runtime,
                    new=True,
                )
                try:
                    with db.session.begin_nested():
                        db.session.add(row)
                        db.session.flush()
                except IntegrityError:
                    row = RuntimeCollectionItem.query.filter_by(
                        collection_id=collection.id,
                        identity_hash=normalized["identity_hash"],
                    ).one()
                    self._merge_row(
                        row,
                        normalized,
                        preserve_runtime=preserve_runtime,
                        new=False,
                    )
                    updated_count += 1
                else:
                    created_count += 1
            else:
                self._merge_row(
                    row,
                    normalized,
                    preserve_runtime=preserve_runtime,
                    new=False,
                )
                updated_count += 1
            db.session.flush()
            row_ids.append(row.id)
        return {
            "created_count": created_count,
            "updated_count": updated_count,
            "row_ids": row_ids,
        }

    @staticmethod
    def _merge_row(
        row: RuntimeCollectionItem,
        normalized: dict[str, Any],
        *,
        preserve_runtime: bool,
        new: bool,
    ) -> None:
        for attribute, key in (
            ("fingerprint", "fingerprint"),
            ("parent_item_key", "parent_item_key"),
            ("chunk_index", "chunk_index"),
            ("chunk_count", "chunk_count"),
            ("source_type", "source_type"),
            ("provider", "provider"),
            ("uri", "uri"),
            ("canonical_uri", "canonical_uri"),
            ("title", "title"),
            ("content_hash", "content_hash"),
            ("estimated_tokens", "estimated_tokens"),
            ("workflow_step_id", "workflow_step_id"),
        ):
            value = normalized.get(key)
            if value is not None:
                setattr(row, attribute, value)
        for attribute, hash_attribute, purpose in (
            ("source_id", "source_lookup_hash", "runtime-collection-source-id"),
            ("external_id", "external_lookup_hash", "runtime-collection-external-id"),
        ):
            value = normalized.get(attribute)
            if value is not None:
                setattr(row, attribute, value)
                setattr(row, hash_attribute, keyed_lookup_digest(str(value), purpose=purpose))
        if normalized.get("content_ref"):
            row.content_ref_json = normalized["content_ref"]
        if normalized.get("source_ref"):
            row.source_ref_json = normalized["source_ref"]
        if normalized.get("resource_reference_ids"):
            row.resource_reference_ids = _string_values([
                *(row.resource_reference_ids or []),
                *normalized["resource_reference_ids"],
            ])
        row.metadata_json = {
            **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
            **normalized["metadata"],
        }
        if new or normalized.get("status_explicit"):
            row.status = normalized["status"]
        if preserve_runtime:
            row.attempt_count = normalized["attempt_count"]
            row.failure_count = normalized["failure_count"]
            row.lease_token = normalized["lease_token"]
            row.lease_owner = normalized["lease_owner"]
            row.lease_expires_at = normalized["lease_expires_at"]
            row.annotations_json = normalized["annotations"][-MAX_ITEM_ANNOTATIONS:]
            row.annotation_count = len(row.annotations_json)
            row.result_ref_json = normalized["result_ref"]
            row.latest_error = normalized["error"]
        row.updated_at = _utcnow()

    def partition(
        self,
        collection_id: Any,
        *,
        statuses: Any = None,
        partition_id: Any = None,
        max_items_per_partition: int = 10,
        max_input_tokens: int = 30_000,
        reserved_output_tokens: int = 4_000,
        chars_per_token: float = 4.0,
        include_annotations: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        collection = self._query_collection(collection_id)
        raw_statuses = _string_values(statuses) or ["pending"]
        eligible_statuses = {normalize_item_status(value) for value in raw_statuses}
        max_items = max(1, min(int(max_items_per_partition or 10), 500))
        max_tokens = max(1_000, min(int(max_input_tokens or 30_000), 200_000))
        reserved = max(0, min(int(reserved_output_tokens or 0), 100_000))
        usable = max(1, max_tokens - reserved)
        chars_per_token = max(1.0, min(float(chars_per_token or 4.0), 12.0))
        partition_key = safe_collection_key(
            partition_id or f"partitions-{uuid.uuid4()}"
        )
        rows = RuntimeCollectionItem.query.filter(
            RuntimeCollectionItem.collection_id == collection.id,
            RuntimeCollectionItem.status.in_(sorted(eligible_statuses)),
        ).order_by(
            RuntimeCollectionItem.created_at.asc(),
            RuntimeCollectionItem.item_key.asc(),
        ).yield_per(250)
        groups = []
        current_refs = []
        current_tokens = 0
        item_count = 0

        def flush() -> None:
            nonlocal current_refs, current_tokens
            if not current_refs:
                return
            index = len(groups) + 1
            groups.append({
                "id": f"{partition_key}-{index:04d}",
                "status": "ready",
                "item_ids": [
                    item["item_id"]
                    for item in current_refs
                    if item.get("item_id")
                ],
                "item_count": len(current_refs),
                "estimated_input_tokens": current_tokens,
                "oversized": any(item["oversized"] for item in current_refs),
                "items": current_refs,
            })
            current_refs = []
            current_tokens = 0

        for row in rows:
            item_count += 1
            item = row.to_dict()
            estimate = estimate_collection_item_tokens(
                item,
                chars_per_token=chars_per_token,
                include_annotations=include_annotations,
            )
            ref = {
                "item_id": row.item_key,
                "identity_hash": row.identity_hash,
                "source_ref": item.get("source_ref") or {},
                "title": row.title,
                "status": row.status,
                "estimated_input_tokens": estimate,
                "oversized": estimate > usable,
            }
            if estimate > usable:
                flush()
                current_refs = [ref]
                current_tokens = estimate
                flush()
                continue
            if (
                len(current_refs) >= max_items
                or (current_refs and current_tokens + estimate > usable)
            ):
                flush()
            current_refs.append(ref)
            current_tokens += estimate
        flush()
        record = {
            "id": partition_key,
            "collection_id": collection.collection_key,
            "status": "ready",
            "created_at": _utc_iso(),
            "status_filter": sorted(eligible_statuses),
            "item_count": item_count,
            "partition_count": len(groups),
            "estimated_input_tokens": sum(
                int(group["estimated_input_tokens"]) for group in groups
            ),
            "max_items_per_partition": max_items,
            "max_input_tokens": max_tokens,
            "reserved_output_tokens": reserved,
            "usable_input_tokens": usable,
            "chars_per_token": chars_per_token,
            "include_annotations": bool(include_annotations),
            "metadata": metadata or {},
            "partitions": groups,
        }
        existing = (
            collection.partitions_json
            if isinstance(collection.partitions_json, list)
            else []
        )
        collection.partitions_json = [
            item
            for item in existing
            if isinstance(item, dict) and item.get("id") != partition_key
        ][-(MAX_COLLECTION_PARTITIONS - 1):] + [record]
        collection.updated_at = _utcnow()
        db.session.commit()
        return {
            "collection": collection.to_dict(include_items=False),
            "partition": record,
        }

    def claim(
        self,
        collection_id: Any,
        *,
        limit: int = 10,
        claimant: Any = None,
        statuses: Any = None,
        lease_seconds: int = 900,
    ) -> dict[str, Any]:
        collection = self._query_collection(collection_id)
        limit = max(1, min(int(limit or 10), 200))
        claimant_text = str(
            claimant or self.workflow_step_id or self.owner_user_id or "worker"
        ).strip()[:255]
        eligible_statuses = {
            normalize_item_status(value)
            for value in (_string_values(statuses) or ["pending"])
        }
        eligible_statuses.discard("leased")
        lease_seconds = max(30, min(int(lease_seconds or 900), 86_400))
        now = _utcnow()
        expires_at = now + timedelta(seconds=lease_seconds)
        token = str(uuid.uuid4())
        eligibility = or_(
            RuntimeCollectionItem.status.in_(sorted(eligible_statuses)),
            and_(
                RuntimeCollectionItem.status == "leased",
                or_(
                    RuntimeCollectionItem.lease_expires_at.is_(None),
                    RuntimeCollectionItem.lease_expires_at <= now,
                ),
            ),
        )
        candidate_ids = (
            select(RuntimeCollectionItem.id)
            .where(
                RuntimeCollectionItem.collection_id == collection.id,
                eligibility,
            )
            .order_by(
                RuntimeCollectionItem.created_at.asc(),
                RuntimeCollectionItem.item_key.asc(),
            )
            .limit(limit)
        )
        statement = (
            update(RuntimeCollectionItem)
            .where(
                RuntimeCollectionItem.id.in_(candidate_ids),
                RuntimeCollectionItem.collection_id == collection.id,
                eligibility,
            )
            .values(
                status="leased",
                lease_token=token,
                lease_owner=claimant_text,
                lease_expires_at=expires_at,
                leased_at=now,
                updated_at=now,
                attempt_count=RuntimeCollectionItem.attempt_count + 1,
            )
            .returning(RuntimeCollectionItem.id)
        )
        claimed_ids = list(db.session.execute(statement).scalars())
        collection.updated_at = now
        rows = (
            RuntimeCollectionItem.query.filter(
                RuntimeCollectionItem.id.in_(claimed_ids)
            )
            .order_by(
                RuntimeCollectionItem.created_at.asc(),
                RuntimeCollectionItem.item_key.asc(),
            )
            .all()
            if claimed_ids
            else []
        )
        result = {
            "collection": collection.to_dict(include_items=False),
            "claim_id": token,
            "lease_token": token,
            "claimant": claimant_text,
            "lease_expires_at": _utc_iso(expires_at),
            "claimed_count": len(rows),
            "items": [row.to_dict() for row in rows],
        }
        db.session.commit()
        return result

    def annotate(
        self,
        collection_id: Any,
        raw_annotations: Any,
        *,
        lease_token: Any = None,
        require_lease: bool = True,
    ) -> dict[str, Any]:
        if not isinstance(raw_annotations, list):
            raise ValueError("annotations must be an array.")
        collection = self._query_collection(collection_id)
        now = _utcnow()
        updated_ids = []
        missing = []
        rejected = []
        default_token = str(lease_token or "").strip()
        for raw in raw_annotations[:1_000]:
            if not isinstance(raw, dict):
                continue
            row = self._resolve_item(collection, raw)
            reference = (
                raw.get("item_id")
                or raw.get("id")
                or raw.get("identity")
                or raw.get("source_id")
            )
            if row is None:
                if reference:
                    missing.append(str(reference))
                continue
            supplied_token = str(
                raw.get("lease_token")
                or raw.get("claim_id")
                or default_token
                or ""
            ).strip()
            rejection = self._lease_rejection(
                row,
                supplied_token,
                now=now,
                require_lease=require_lease,
            )
            if rejection:
                rejected.append({
                    "item_id": row.item_key,
                    "reason": rejection,
                    "current_status": row.status,
                    "current_lease_owner": row.lease_owner,
                })
                continue
            annotation_payload = (
                raw.get("annotation")
                if isinstance(raw.get("annotation"), dict)
                else {}
            )
            result_ref = (
                raw.get("result_ref")
                if isinstance(raw.get("result_ref"), dict)
                else (
                    raw.get("data_ref")
                    if isinstance(raw.get("data_ref"), dict)
                    else {}
                )
            )
            annotation = {
                "id": str(raw.get("annotation_id") or uuid.uuid4()),
                "kind": str(raw.get("kind") or "finding")[:100],
                "annotation": annotation_payload,
                "result_ref": result_ref,
                "evidence_ids": _string_values(raw.get("evidence_ids")),
                "source_item_id": row.item_key,
                "source_ref": row.source_ref_json or {},
                "metadata": (
                    raw.get("metadata")
                    if isinstance(raw.get("metadata"), dict)
                    else {}
                ),
                "created_at": _utc_iso(now),
                "created_by_user_id": self.owner_user_id,
                "lease_token_hash": (
                    hashlib.sha256(supplied_token.encode("utf-8")).hexdigest()[:16]
                    if supplied_token
                    else None
                ),
            }
            error = _bounded_text(raw.get("error"), 8_000)
            if error:
                annotation["error"] = error
            existing_annotations = (
                row.annotations_json
                if isinstance(row.annotations_json, list)
                else []
            )
            row.annotations_json = [
                *existing_annotations,
                annotation,
            ][-MAX_ITEM_ANNOTATIONS:]
            row.annotation_count = len(row.annotations_json)
            target_status = normalize_item_status(
                raw.get("status"),
                default="failed" if error else "completed",
            )
            row.status = target_status
            row.result_ref_json = result_ref or row.result_ref_json or {}
            row.latest_error = error
            if error:
                row.failure_count = int(row.failure_count or 0) + 1
            if raw.get("workflow_step_id") or self.workflow_step_id:
                row.workflow_step_id = str(
                    raw.get("workflow_step_id") or self.workflow_step_id
                )[:36]
            row.updated_at = now
            row.completed_at = (
                now if target_status in {"completed", "skipped"} else None
            )
            if target_status != "leased":
                row.lease_token = None
                row.lease_owner = None
                row.lease_expires_at = None
            updated_ids.append(row.id)
        collection.updated_at = now
        db.session.commit()
        updated = (
            RuntimeCollectionItem.query.filter(
                RuntimeCollectionItem.id.in_(updated_ids)
            ).all()
            if updated_ids
            else []
        )
        return {
            "collection": collection.to_dict(include_items=False),
            "updated_count": len(updated),
            "missing": missing,
            "rejected_count": len(rejected),
            "rejected": rejected,
            "items": [row.to_dict() for row in updated],
        }

    @staticmethod
    def _lease_rejection(
        row: RuntimeCollectionItem,
        supplied_token: str,
        *,
        now: datetime,
        require_lease: bool,
    ) -> str | None:
        if row.status == "leased":
            if row.lease_expires_at is None or row.lease_expires_at <= now:
                return "lease_expired"
            if not supplied_token:
                return "lease_token_required"
            if supplied_token != str(row.lease_token or ""):
                return "stale_lease_token"
            return None
        if require_lease:
            return "item_not_leased"
        return None

    @staticmethod
    def _resolve_item(
        collection: RuntimeCollection,
        payload: dict[str, Any],
    ) -> RuntimeCollectionItem | None:
        for key in ("item_id", "id"):
            if payload.get(key) is not None:
                row = RuntimeCollectionItem.query.filter_by(
                    collection_id=collection.id,
                    item_key=str(payload[key]),
                ).first()
                if row:
                    return row
        if payload.get("identity"):
            identity = str(payload["identity"])
            identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            row = RuntimeCollectionItem.query.filter_by(
                collection_id=collection.id,
                identity_hash=identity_hash,
            ).first()
            if row:
                return row
        if payload.get("source_id") is not None:
            source_id = str(payload["source_id"])
            candidates = keyed_lookup_candidates(
                source_id,
                purpose="runtime-collection-source-id",
            )
            row = RuntimeCollectionItem.query.filter(
                RuntimeCollectionItem.collection_id == collection.id,
                RuntimeCollectionItem.source_lookup_hash.in_(candidates),
            ).first()
            if row:
                return row
            for row in RuntimeCollectionItem.query.filter_by(
                collection_id=collection.id,
                source_lookup_hash=None,
            ).all():
                if str(row.source_id or "") == source_id:
                    row.source_lookup_hash = keyed_lookup_digest(
                        source_id,
                        purpose="runtime-collection-source-id",
                    )
                    return row
        if payload.get("external_id") is not None:
            external_id = str(payload["external_id"])
            candidates = keyed_lookup_candidates(
                external_id,
                purpose="runtime-collection-external-id",
            )
            row = RuntimeCollectionItem.query.filter(
                RuntimeCollectionItem.collection_id == collection.id,
                RuntimeCollectionItem.external_lookup_hash.in_(candidates),
            ).first()
            if row:
                return row
            for row in RuntimeCollectionItem.query.filter_by(
                collection_id=collection.id,
                external_lookup_hash=None,
            ).all():
                if str(row.external_id or "") == external_id:
                    row.external_lookup_hash = keyed_lookup_digest(
                        external_id,
                        purpose="runtime-collection-external-id",
                    )
                    return row
        return None

    def sync_delegation_steps(self, steps: list[WorkflowStep]) -> None:
        rows_by_key: dict[tuple[str, str], RuntimeCollectionItem] = {}
        collections = {
            row.collection_key: row
            for row in RuntimeCollection.query.filter_by(
                workflow_run_id=self.run_id
            ).all()
        }
        if not collections:
            return
        now = _utcnow()
        changed = False
        for step in steps:
            metadata = (
                step.metadata_json
                if isinstance(step.metadata_json, dict)
                else {}
            )
            delegation = (
                metadata.get("delegation_item")
                if isinstance(metadata.get("delegation_item"), dict)
                else {}
            )
            collection = collections.get(str(delegation.get("collection_id") or ""))
            item_key = str(delegation.get("item_id") or "")
            if not collection or not item_key:
                continue
            cache_key = (collection.id, item_key)
            row = rows_by_key.get(cache_key)
            if row is None:
                row = RuntimeCollectionItem.query.filter_by(
                    collection_id=collection.id,
                    item_key=item_key,
                ).first()
                if row:
                    rows_by_key[cache_key] = row
            if row is None:
                continue
            step_status = str(step.status or "planned").lower()
            if step_status in {"planned", "ready", "queued"}:
                if row.status not in {"pending", "leased"}:
                    continue
                target_status = row.status
            elif step_status in {
                "running",
                "waiting_for_approval",
                "waiting_for_review",
                "paused",
            }:
                target_status = "leased"
                token = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"iarf:workflow-step:{collection.id}:{step.id}",
                    )
                )
                if row.lease_token != token:
                    row.attempt_count = int(row.attempt_count or 0) + 1
                row.lease_token = token
                row.lease_owner = step.id
                row.leased_at = row.leased_at or now
                row.lease_expires_at = now + timedelta(hours=1)
            elif step_status == "completed":
                target_status = "completed"
                row.result_ref_json = {
                    "kind": "workflow_step_output",
                    "workflow_run_id": self.run_id,
                    "workflow_step_id": step.id,
                    "inline": step.output_payload or {},
                }
                row.latest_error = None
                row.completed_at = now
            elif step_status == "failed":
                target_status = "failed"
                row.latest_error = _bounded_text(step.error, 8_000)
                if row.status != "failed":
                    row.failure_count = int(row.failure_count or 0) + 1
            elif step_status in {"cancelled", "skipped"}:
                target_status = "skipped"
                row.completed_at = now
            else:
                continue
            row.status = target_status
            row.workflow_step_id = step.id
            row.updated_at = now
            if target_status != "leased":
                row.lease_token = None
                row.lease_owner = None
                row.lease_expires_at = None
            collection.updated_at = now
            changed = True
        if changed:
            db.session.commit()
