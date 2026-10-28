import logging
import os
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from carlina.runtime_environment import mark_worker_environment

mark_worker_environment(os.environ, addon_id="doc_processor")

from iarf_assistant.app import create_app
from iarf_assistant.core.document_knowledge import (
    document_source_modified_at,
    fail_document_source,
    finalize_document_source,
)
from iarf_assistant.core.task_queue import WorkerNode
from iarf_assistant.extensions import db
from iarf_assistant.knowledge.document_parser import extract_document_for_rag
from iarf_assistant.models import DataContext

logger = logging.getLogger("KnowledgeWorker")
logger.setLevel(logging.INFO)

app = create_app()

worker = WorkerNode(app.instance_path, "core_knowledge", idle_timeout=0)


def _cleanup_temp_file(file_path: str | None):
    if not file_path:
        return

    temp_path = Path(file_path)
    if temp_path.exists():
        try:
            temp_path.unlink()
        except Exception as e:
            logger.warning("Could not delete temporary file %s: %s", temp_path, e)


@worker.handler("process_knowledge_upload")
def process_knowledge_upload(payload: dict, task_queue, job_id: str):
    context_id = payload.get("context_id")
    source_id = payload.get("source_id")
    user_id = payload.get("user_id")
    file_path = payload.get("file_path")
    engine = payload.get("engine", "auto")
    kwargs = dict(payload.get("kwargs") or {})
    extractor_config = dict(kwargs)

    data_context = None
    if source_id:
        if not user_id:
            raise ValueError("Canonical knowledge upload requires user_id.")
    else:
        # Compatibility path for jobs queued before the canonical-source
        # migration. New uploads never create this duplicate text row.
        data_context = db.session.get(DataContext, context_id)
        if not data_context:
            raise ValueError(f"DataContext {context_id} not found.")
        user_id = data_context.user_id

    task_queue.set_status(
        job_id,
        {"status": "processing", "message": "Extracting document content...", "progress": 10},
    )

    def progress_callback(pct, msg):
        try:
            normalized_pct = max(0, min(100, int(pct or 0)))
        except Exception:
            normalized_pct = 0

        scaled_pct = 10 + int((normalized_pct / 100) * 75)
        task_queue.set_status(
            job_id,
            {"status": "processing", "message": msg or "Processing...", "progress": scaled_pct},
        )

    kwargs["progress_callback"] = progress_callback

    try:
        markdown_content = extract_document_for_rag(file_path, engine=engine, **kwargs)
        if not markdown_content or markdown_content.startswith("Error:"):
            raise RuntimeError(markdown_content or "Extraction returned no content.")

        task_queue.set_status(
            job_id,
            {
                "status": "processing",
                "message": "Writing immutable canonical source version...",
                "progress": 85,
            },
        )

        if source_id:
            source, version, created = finalize_document_source(
                user_id=int(user_id),
                source_id=source_id,
                content=markdown_content,
                extractor_name=engine,
                extractor_config=extractor_config,
                metadata={
                    "filename": payload.get("filename"),
                    "file_name": payload.get("filename"),
                    "file_path": payload.get("web_path"),
                    "collection_mode": payload.get("collection_mode"),
                },
                provenance={
                    "upload_collection_id": payload.get("collection_id"),
                    "worker_job_id": job_id,
                },
                source_modified_at=document_source_modified_at(file_path),
                commit=True,
            )
            result = {
                "message": "Successfully extracted canonical document source.",
                "source_id": source.id,
                "source_version_id": version.id,
                "version_created": created,
                "requires_index_refresh": (
                    payload.get("collection_mode") == "vector"
                ),
            }
        else:
            data_context.content = markdown_content
            metadata = dict(data_context.json_metadata or {})
            metadata.pop("status", None)
            metadata.pop("error_message", None)
            data_context.json_metadata = metadata
            db.session.commit()

            if hasattr(app, "rag_service"):
                app.rag_service.generate_embeddings_for_context(data_context.id)
            result = {
                "message": "Successfully extracted and embedded legacy document.",
                "legacy_context_id": data_context.id,
            }

        task_queue.set_status(
            job_id,
            {"status": "completed", "message": "Processing complete.", "progress": 100},
        )
        return result

    except Exception as e:
        db.session.rollback()
        logger.exception("Error processing knowledge upload")

        if source_id:
            try:
                fail_document_source(
                    user_id=int(user_id),
                    source_id=source_id,
                    error=e,
                    commit=True,
                )
            except Exception:
                db.session.rollback()
                logger.exception(
                    "Could not persist canonical source failure state"
                )
        else:
            failed_context = db.session.get(DataContext, context_id)
            if failed_context:
                metadata = dict(failed_context.json_metadata or {})
                metadata["status"] = "error"
                metadata["error_message"] = str(e)
                failed_context.json_metadata = metadata
                failed_context.content = f"Error processing document: {str(e)}"
                db.session.commit()

        raise
    finally:
        if not payload.get("preserve_file"):
            _cleanup_temp_file(file_path)


if __name__ == "__main__":
    logger.info("Starting Knowledge Background Worker...")
    with app.app_context():
        worker.start()
