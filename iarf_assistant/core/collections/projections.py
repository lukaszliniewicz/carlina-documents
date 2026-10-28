"""Pure runtime-collection serializers with explicit bulk projections."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def _partitions(collection: Any) -> list[dict[str, Any]]:
    value = collection.partitions_json
    return value if isinstance(value, list) else []


def serialize_runtime_collection_summary(
    collection: Any,
    *,
    status_counts: Mapping[str, int] | None = None,
    annotation_count: int = 0,
) -> dict[str, Any]:
    """Serialize one collection without issuing database queries."""

    normalized_counts = {
        str(status or "pending").lower(): int(count or 0)
        for status, count in (status_counts or {}).items()
    }
    partitions = _partitions(collection)
    return {
        "id": collection.collection_key,
        "record_id": collection.id,
        "title": collection.title,
        "objective": collection.objective,
        "kind": collection.kind,
        "adapter_id": collection.adapter_id,
        "status": collection.status,
        "item_count": sum(normalized_counts.values()),
        "item_status_counts": normalized_counts,
        "annotation_count": int(annotation_count or 0),
        "partition_count": len(partitions),
        "latest_partition_id": (
            partitions[-1].get("id")
            if partitions and isinstance(partitions[-1], dict)
            else None
        ),
        "updated_at": (
            collection.updated_at.isoformat() if collection.updated_at else None
        ),
        "storage": "rows_v1",
    }


def serialize_runtime_collection(
    collection: Any,
    *,
    summary: Mapping[str, Any],
    items: Sequence[Any] | None = None,
    total_item_count: int | None = None,
) -> dict[str, Any]:
    """Serialize a collection from already-loaded rows and aggregates."""

    partitions = _partitions(collection)
    payload = {
        "id": collection.collection_key,
        "record_id": collection.id,
        "workflow_run_id": collection.workflow_run_id,
        "owner_user_id": collection.owner_user_id,
        "title": collection.title,
        "objective": collection.objective,
        "description": collection.objective,
        "kind": collection.kind,
        "adapter_id": collection.adapter_id,
        "status": collection.status,
        "identity_keys": collection.identity_keys or [],
        "schema": collection.schema_json or {},
        "partitions": partitions,
        "metadata": collection.metadata_json or {},
        "storage_version": collection.storage_version,
        "storage": "rows_v1",
        "created_at": (
            collection.created_at.isoformat() if collection.created_at else None
        ),
        "updated_at": (
            collection.updated_at.isoformat() if collection.updated_at else None
        ),
        "completed_at": (
            collection.completed_at.isoformat() if collection.completed_at else None
        ),
        "summary": dict(summary),
    }
    if items is not None:
        serialized_items = [item.to_dict() for item in items]
        total = (
            int(total_item_count)
            if total_item_count is not None
            else len(serialized_items)
        )
        payload["items"] = serialized_items
        payload["returned_item_count"] = len(serialized_items)
        payload["omitted_item_count"] = max(0, total - len(serialized_items))
    return payload


def load_collection_summaries(
    collections: Iterable[Any],
) -> dict[str, dict[str, Any]]:
    """Load aggregates for any number of collections in one SQL statement."""

    rows = list(collections)
    if not rows:
        return {}

    from sqlalchemy import func

    from iarf_assistant.extensions import db
    from iarf_assistant.models import RuntimeCollectionItem

    collection_ids = [str(collection.id) for collection in rows]
    aggregates = (
        db.session.query(
            RuntimeCollectionItem.collection_id,
            RuntimeCollectionItem.status,
            func.count(RuntimeCollectionItem.id),
            func.coalesce(func.sum(RuntimeCollectionItem.annotation_count), 0),
        )
        .filter(RuntimeCollectionItem.collection_id.in_(collection_ids))
        .group_by(
            RuntimeCollectionItem.collection_id,
            RuntimeCollectionItem.status,
        )
        .all()
    )
    counts_by_collection: dict[str, dict[str, int]] = {
        collection_id: {} for collection_id in collection_ids
    }
    annotations_by_collection = dict.fromkeys(collection_ids, 0)
    for collection_id, status, item_count, annotation_count in aggregates:
        key = str(collection_id)
        counts_by_collection[key][str(status or "pending").lower()] = int(
            item_count or 0
        )
        annotations_by_collection[key] += int(annotation_count or 0)
    return {
        str(collection.id): serialize_runtime_collection_summary(
            collection,
            status_counts=counts_by_collection[str(collection.id)],
            annotation_count=annotations_by_collection[str(collection.id)],
        )
        for collection in rows
    }


def build_run_collection_summary(
    summaries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the run-level collection rollup from pure summary payloads."""

    status_counts: dict[str, int] = {}
    for summary in summaries:
        for status, count in dict(summary.get("item_status_counts") or {}).items():
            status_counts[str(status)] = status_counts.get(str(status), 0) + int(
                count or 0
            )
    return {
        "runtime_collections": [dict(item) for item in summaries[-50:]],
        "runtime_collection_count": len(summaries),
        "runtime_work_item_count": sum(
            int(summary.get("item_count") or 0) for summary in summaries
        ),
        "runtime_work_item_status_counts": status_counts,
    }
