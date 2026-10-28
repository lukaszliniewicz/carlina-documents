from __future__ import annotations

from collections import Counter
from typing import Any

from flask import Blueprint, current_app, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy.orm import selectinload

from iarf_assistant.core.frontend_contracts import set_frontend_api_version_headers
from iarf_assistant.extensions import db
from iarf_assistant.knowledge import routes as legacy_knowledge
from iarf_assistant.models import (
    DataContext,
    DataContextChunk,
    DataContextCollection,
    KnowledgeCollectionMembership,
    KnowledgeSource,
    PluginCredential,
    data_context_collection_association,
)

frontend_knowledge_blueprint = Blueprint("frontend_knowledge_api", __name__)
MAX_ITEMS_PER_PAGE = 80
MAX_CHUNKS_PER_PAGE = 80


def _bounded_page_size(raw_value: Any, *, default: int, maximum: int) -> int:
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return default
    return min(max(value, 1), maximum)


def _loaded_addons() -> dict[str, Any]:
    manager = getattr(current_app, "addon_manager", None)
    loaded = getattr(manager, "loaded_addons", None)
    return loaded if isinstance(loaded, dict) else {}


def _knowledge_options() -> dict[str, Any]:
    loaded = _loaded_addons()
    engines: list[dict[str, Any]] = [
        {
            "id": "auto",
            "name": "Auto",
            "description": "Choose the best available extractor.",
        }
    ]
    doc_processor = loaded.get("doc_processor")
    if doc_processor is not None:
        try:
            client = doc_processor.initialize_client(
                {"current_user_id": current_user.id}
            )
            declared = client.get_engines()
            if isinstance(declared, list):
                seen = {"auto"}
                for engine in declared:
                    if not isinstance(engine, dict):
                        continue
                    engine_id = str(engine.get("id") or "").strip()
                    if not engine_id or engine_id in seen:
                        continue
                    seen.add(engine_id)
                    engines.append(
                        {
                            "id": engine_id,
                            "name": str(
                                engine.get("name")
                                or engine_id.replace("_", " ").title()
                            ),
                            "description": str(
                                engine.get("description") or ""
                            ),
                        }
                    )
        except Exception:
            current_app.logger.exception(
                "Could not load Knowledge document engines."
            )

    email_accounts: list[dict[str, Any]] = []
    if "email" in loaded:
        try:
            from iarf_assistant.addons.email.models import EmailAccount

            accounts = (
                EmailAccount.query.filter_by(user_id=current_user.id)
                .order_by(EmailAccount.account_email)
                .all()
            )
            email_accounts = [
                {
                    "id": account.id,
                    "label": account.account_email,
                    "provider": account.provider_id,
                    "active": account.is_active,
                }
                for account in accounts
            ]
        except Exception:
            current_app.logger.exception(
                "Could not load Knowledge email accounts."
            )

    drive_accounts: list[dict[str, Any]] = []
    if "googledrive" in loaded:
        try:
            credentials = (
                PluginCredential.query.filter_by(
                    user_id=current_user.id,
                    provider_name="google_auth",
                )
                .order_by(PluginCredential.credential_name)
                .all()
            )
            drive_accounts = [
                {
                    "id": credential.id,
                    "label": credential.credential_name,
                    "provider": "google_auth",
                    "active": credential.is_active,
                }
                for credential in credentials
            ]
        except Exception:
            current_app.logger.exception(
                "Could not load Knowledge Drive accounts."
            )

    return {
        "engines": engines,
        "email_accounts": email_accounts,
        "drive_accounts": drive_accounts,
        "providers": {
            "document_upload": True,
            "email": "email" in loaded,
            "googledrive": "googledrive" in loaded,
        },
    }


@frontend_knowledge_blueprint.after_request
def _frontend_knowledge_headers(response):
    set_frontend_api_version_headers(response)
    response.headers["Cache-Control"] = "private, no-cache"
    return response


@frontend_knowledge_blueprint.route(
    "/api/frontend/knowledge",
    methods=["GET"],
)
@login_required
def get_frontend_knowledge():
    """Return the small initial view model for the Svelte Knowledge desk."""
    collections = (
        DataContextCollection.query.filter_by(user_id=current_user.id)
        .options(selectinload(DataContextCollection.items))
        .order_by(DataContextCollection.name)
        .all()
    )
    payloads = [collection.to_dict() for collection in collections]
    source_counts = Counter(
        str(collection.source_type or "other") for collection in collections
    )
    return jsonify(
        {
            "collections": payloads,
            "summary": {
                "total": len(collections),
                "vector": sum(
                    collection.collection_mode == "vector"
                    for collection in collections
                ),
                "regular": sum(
                    collection.collection_mode == "regular"
                    for collection in collections
                ),
                "items": sum(payload["item_count"] for payload in payloads),
                "source_types": dict(sorted(source_counts.items())),
            },
            "options": _knowledge_options(),
        }
    )


@frontend_knowledge_blueprint.route(
    "/api/frontend/knowledge/collections",
    methods=["POST"],
)
@login_required
def create_frontend_collection():
    return legacy_knowledge.create_collection()


@frontend_knowledge_blueprint.route(
    "/api/frontend/knowledge/collections/<int:collection_id>",
    methods=["PUT", "DELETE"],
)
@login_required
def mutate_frontend_collection(collection_id: int):
    if request.method == "DELETE":
        return legacy_knowledge.delete_collection(collection_id)
    return legacy_knowledge.update_collection(collection_id)


@frontend_knowledge_blueprint.route(
    "/api/frontend/knowledge/collections/<int:collection_id>/items",
    methods=["GET"],
)
@login_required
def get_frontend_collection_items(collection_id: int):
    collection = db.session.get(DataContextCollection, collection_id)
    if not collection or collection.user_id != current_user.id:
        return jsonify({"error": "Collection not found"}), 404

    page = max(request.args.get("page", 1, type=int), 1)
    per_page = _bounded_page_size(
        request.args.get("per_page"),
        default=30,
        maximum=MAX_ITEMS_PER_PAGE,
    )
    canonical_query = (
        KnowledgeCollectionMembership.query
        .join(KnowledgeSource)
        .filter(
            KnowledgeCollectionMembership.collection_id == collection_id,
            KnowledgeSource.user_id == current_user.id,
        )
    )
    if canonical_query.first() is not None:
        from iarf_assistant.core.document_knowledge import canonical_source_item

        pagination = (
            canonical_query
            .order_by(
                KnowledgeCollectionMembership.position.asc(),
                KnowledgeCollectionMembership.id.asc(),
            )
            .paginate(page=page, per_page=per_page, error_out=False)
        )
        items = []
        for membership in pagination.items:
            item = canonical_source_item(membership.source)
            item["membership_id"] = membership.id
            item["membership_metadata"] = membership.metadata_json or {}
            items.append(item)
        return jsonify(
            {
                "items": items,
                "total": pagination.total,
                "pages": pagination.pages,
                "current_page": pagination.page,
                "has_next": pagination.has_next,
                "has_prev": pagination.has_prev,
            }
        )

    pagination = (
        DataContext.query.join(
            data_context_collection_association,
            DataContext.id
            == data_context_collection_association.c.context_id,
        )
        .filter(
            data_context_collection_association.c.collection_id
            == collection_id,
            DataContext.user_id == current_user.id,
        )
        .order_by(DataContext.created_at.desc(), DataContext.id.desc())
        .paginate(page=page, per_page=per_page, error_out=False)
    )
    return jsonify(
        {
            "items": [item.to_dict() for item in pagination.items],
            "total": pagination.total,
            "pages": pagination.pages,
            "current_page": pagination.page,
            "has_next": pagination.has_next,
            "has_prev": pagination.has_prev,
        }
    )


@frontend_knowledge_blueprint.route(
    "/api/frontend/knowledge/contexts/<int:context_id>/xray",
    methods=["GET"],
)
@login_required
def get_frontend_context_xray(context_id: int):
    context = db.session.get(DataContext, context_id)
    if not context or context.user_id != current_user.id:
        return jsonify({"error": "Data Context not found"}), 404

    page = max(request.args.get("page", 1, type=int), 1)
    per_page = _bounded_page_size(
        request.args.get("per_page"),
        default=30,
        maximum=MAX_CHUNKS_PER_PAGE,
    )
    pagination = (
        DataContextChunk.query.filter_by(data_context_id=context.id)
        .order_by(DataContextChunk.id)
        .paginate(page=page, per_page=per_page, error_out=False)
    )
    return jsonify(
        {
            "id": context.id,
            "source_type": context.source_type,
            "source_id": context.source_id,
            "metadata": context.json_metadata,
            "raw_content": context.content,
            "chunks": [
                {
                    "id": chunk.id,
                    "text": chunk.chunk_text,
                    "meta": chunk.json_metadata,
                }
                for chunk in pagination.items
            ],
            "chunk_total": pagination.total,
            "chunk_pages": pagination.pages,
            "chunk_page": pagination.page,
            "has_next": pagination.has_next,
            "has_prev": pagination.has_prev,
        }
    )


@frontend_knowledge_blueprint.route(
    "/api/frontend/knowledge/sources/<string:source_id>/xray",
    methods=["GET"],
)
@login_required
def get_frontend_source_xray(source_id: str):
    from iarf_assistant.core.document_knowledge import canonical_source_xray
    from iarf_assistant.core.knowledge_fabric import KnowledgeOwnershipError

    page = max(request.args.get("page", 1, type=int), 1)
    per_page = _bounded_page_size(
        request.args.get("per_page"),
        default=30,
        maximum=MAX_CHUNKS_PER_PAGE,
    )
    try:
        return jsonify(canonical_source_xray(
            user_id=current_user.id,
            source_id=source_id,
            page=page,
            per_page=per_page,
        ))
    except KnowledgeOwnershipError:
        return jsonify({"error": "Knowledge source not found"}), 404


@frontend_knowledge_blueprint.route(
    "/api/frontend/knowledge/collections/<int:collection_id>/upload",
    methods=["POST"],
)
@login_required
def upload_frontend_collection_item(collection_id: int):
    return legacy_knowledge.upload_to_collection(collection_id)


@frontend_knowledge_blueprint.route(
    "/api/frontend/knowledge/jobs/<string:job_id>",
    methods=["GET"],
)
@login_required
def get_frontend_knowledge_job(job_id: str):
    return legacy_knowledge.get_job_status(job_id)


@frontend_knowledge_blueprint.route(
    "/api/frontend/knowledge/collections/<int:collection_id>/sync",
    methods=["GET"],
)
@login_required
def sync_frontend_collection(collection_id: int):
    return legacy_knowledge.build_collection_from_db(collection_id)


@frontend_knowledge_blueprint.route(
    "/api/frontend/knowledge/collections/<int:collection_id>/enable-rag",
    methods=["POST"],
)
@login_required
def enable_frontend_collection_rag(collection_id: int):
    return legacy_knowledge.enable_rag_for_collection(collection_id)


@frontend_knowledge_blueprint.route(
    "/api/frontend/knowledge/collections/<int:collection_id>/export",
    methods=["GET"],
)
@login_required
def export_frontend_collection(collection_id: int):
    return legacy_knowledge.export_collection(collection_id)


@frontend_knowledge_blueprint.route(
    "/api/frontend/knowledge/items/<int:item_id>",
    methods=["DELETE"],
)
@login_required
def delete_frontend_knowledge_item(item_id: int):
    return legacy_knowledge.delete_item(item_id)


@frontend_knowledge_blueprint.route(
    "/api/frontend/knowledge/sources/<string:source_id>",
    methods=["DELETE"],
)
@login_required
def remove_frontend_knowledge_source(source_id: str):
    source = db.session.get(KnowledgeSource, source_id)
    if source is None or source.user_id != current_user.id:
        return jsonify({"error": "Knowledge source not found"}), 404
    collection_id = request.args.get("collection_id", type=int)
    if collection_id is None:
        return jsonify({"error": "collection_id is required"}), 400
    collection = db.session.get(DataContextCollection, collection_id)
    if collection is None or collection.user_id != current_user.id:
        return jsonify({"error": "Collection not found"}), 404
    membership = KnowledgeCollectionMembership.query.filter_by(
        collection_id=collection.id,
        source_id=source.id,
    ).one_or_none()
    if membership is None:
        return jsonify({"error": "Source is not in this collection"}), 404
    db.session.delete(membership)
    db.session.commit()
    return jsonify({
        "success": True,
        "message": (
            "Source removed from this collection. Its canonical versions "
            "remain available to other views."
        ),
        "source_id": source.id,
    })


@frontend_knowledge_blueprint.route(
    "/api/frontend/knowledge/search",
    methods=["POST"],
)
@login_required
def search_frontend_knowledge():
    return legacy_knowledge.rag_search()


def register_frontend_knowledge_blueprint(app) -> None:
    app.register_blueprint(frontend_knowledge_blueprint)


__all__ = [
    "frontend_knowledge_blueprint",
    "register_frontend_knowledge_blueprint",
]
