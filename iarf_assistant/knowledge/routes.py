import logging
from datetime import datetime
import json
import time
import uuid
from email.utils import parsedate_to_datetime
from werkzeug.utils import secure_filename
from flask import Blueprint, request, jsonify, Response, stream_with_context, current_app
from pathlib import Path
import shutil
from flask_login import login_required, current_user
from ..extensions import db
from ..models import DataContext, DataContextCollection, Setting, DataContextChunk
from ..pages import render_frontend_shell, render_frontend_unavailable
from ..core.upload_paths import uploads_root
from .document_parser import extract_document_for_rag

try:
    from markdownify import markdownify as md
except ImportError:
    md = None
    logging.warning("markdownify is not installed. HTML to MD conversion for emails will not be available in knowledge collection building.")

try:
    import pypandoc
except ImportError:
    pypandoc = None
    logging.warning("pypandoc is not installed. Document conversion will not be available.")
try:
    from docx2pdf import convert
except ImportError:
    convert = None
    logging.warning("docx2pdf is not installed. DOCX to PDF conversion will not be available.")


def _get_collection_dir(collection):
    """Gets the storage directory for a collection, creating it if it doesn't exist."""
    if not collection.storage_path:
        return None, "Collection has no storage path configured."
    
    uploads_dir = uploads_root()
    collection_dir = uploads_dir / collection.storage_path
    try:
        collection_dir.mkdir(parents=True, exist_ok=True)
        return collection_dir, None
    except Exception as e:
        logging.error(f"Could not create collection directory {collection_dir}: {e}")
        return None, str(e)


def _admin_job_log_links():
    if not getattr(current_user, 'is_admin', False):
        return []

    registry = getattr(current_app, 'log_registry', None)
    if not registry:
        return []

    links = []
    for source_id in ("jobs.runtime", "job.core_knowledge", "worker.core_knowledge"):
        try:
            links.append(registry.get_link_for_source(source_id))
        except KeyError:
            continue
    return links

def _convert_to_pdf(source_path, output_dir):
    """Converts a document to PDF. Returns path to PDF or error."""
    source_path = Path(source_path)
    output_path = output_dir / f"{source_path.stem}.pdf"

    if source_path.suffix.lower() == '.docx':
        if not convert:
            return None, "docx2pdf library is not installed."
        try:
            convert(str(source_path), str(output_path))
            return output_path, None
        except Exception as e:
            logging.error(f"Error converting DOCX to PDF: {e}")
            return None, str(e)
    
    if source_path.suffix.lower() in ['.doc', '.rtf', '.odt']:
        if not pypandoc:
            return None, "pypandoc library is not installed."
        try:
            pypandoc.convert_file(str(source_path), 'pdf', outputfile=str(output_path))
            return output_path, None
        except Exception as e:
            logging.error(f"Error converting {source_path.suffix} to PDF with pandoc: {e}")
            return None, str(e)

    return None, f"Unsupported file type for PDF conversion: {source_path.suffix}"

def safe_unlink(path: Path, retries: int = 5, delay: float = 0.5):
    """Attempts to safely delete a file, with retries for Windows/OneDrive file locks."""
    if not path.exists():
        return
    for i in range(retries):
        try:
            path.unlink()
            return
        except OSError as e:
            if i == retries - 1:
                logging.warning(f"Could not delete {path} after {retries} attempts: {e}")
            else:
                time.sleep(delay)

def _convert_to_markdown(source_path, force_ocr=False, no_ocr=False, ocr_lang="eng", paginate_output=False):
    """Converts a document to Markdown text. Returns text or error."""
    source_path = Path(source_path)
    
    # Route PDFs to the advanced hybrid extractor
    if source_path.suffix.lower() == '.pdf':
        try:
            md_content = extract_document_for_rag(
                str(source_path),
                force_ocr=force_ocr,
                no_ocr=no_ocr,
                ocr_lang=ocr_lang,
                paginate_output=paginate_output,
            )
            if md_content:
                return md_content, None
        except Exception as e:
            logging.error(f"Error in hybrid PDF extraction: {e}")
            return None, str(e)

    # Fallback to Pandoc for DOCX, EPUB, TXT, RTF
    if not pypandoc:
        return None, "pypandoc library is not installed."
    try:
        # Extra args can help with PDF conversion quality
        extra_args = ['--extract-media=.', '--wrap=none']
        md_content = pypandoc.convert_file(str(source_path), 'markdown', extra_args=extra_args)
        return md_content, None
    except Exception as e:
        logging.error(f"Error converting to Markdown with pandoc: {e}")
        return None, str(e)

def _format_date_for_export(iso_date_str):
    if not iso_date_str:
        return 'No Date'
    try:
        # Handles ISO format with or without timezone
        dt_object = datetime.fromisoformat(iso_date_str.replace('Z', '+00:00'))
        return dt_object.strftime('%d/%m/%y, %H:%M')
    except (ValueError, TypeError):
        # Fallback for non-iso strings (e.g. raw email headers)
        try:
            dt_object = parsedate_to_datetime(iso_date_str)
            return dt_object.strftime('%d/%m/%y, %H:%M')
        except Exception:
            return iso_date_str # Return original if all parsing fails

blueprint = Blueprint(
    'knowledge',
    __name__,
    url_prefix='/knowledge',
    template_folder='templates'
)

@blueprint.route('/')
@login_required
def index():
    """Renders the main Knowledge Manager page."""
    shell = render_frontend_shell(route_id="knowledge")
    if shell is not None:
        return shell
    return render_frontend_unavailable(route_id="knowledge")

# --- API Endpoints for Knowledge Manager ---

@blueprint.route('/api/contexts/<int:context_id>/xray', methods=['GET'])
@login_required
def get_context_xray(context_id):
    """Returns the raw extracted text and the resulting chunks for debugging/transparency."""
    context = db.session.get(DataContext, context_id)
    if not context or context.user_id != current_user.id:
        return jsonify({"error": "Data Context not found"}), 404
        
    chunks = []
    for chunk in context.chunks:
        chunks.append({
            "id": chunk.id,
            "text": chunk.chunk_text,
            "meta": chunk.json_metadata
        })
        
    return jsonify({
        "id": context.id,
        "source_type": context.source_type,
        "source_id": context.source_id,
        "metadata": context.json_metadata,
        "raw_content": context.content,
        "chunks": chunks
    })

@blueprint.route('/api/contexts', methods=['GET'])
@login_required
def get_contexts():
    """Returns a paginated and filtered list of DataContext items for the current user."""
    try:
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 20, type=int)
        source_type = request.args.get('source_type', None, type=str)
        
        query = DataContext.query.filter_by(user_id=current_user.id)
        if source_type and source_type != 'all':
            query = query.filter_by(source_type=source_type)
        
        query = query.order_by(DataContext.created_at.desc())
        
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)
        items = [item.to_dict() for item in pagination.items]
        
        return jsonify({
            'items': items,
            'total': pagination.total,
            'pages': pagination.pages,
            'current_page': pagination.page,
            'has_next': pagination.has_next,
            'has_prev': pagination.has_prev
        })
    except Exception as e:
        logging.exception("Error fetching data contexts.")
        return jsonify({"error": str(e)}), 500

@blueprint.route('/api/collections', methods=['GET'])
@login_required
def get_collections():
    """Returns a list of all DataContextCollection items for the current user."""
    try:
        collections = DataContextCollection.query.filter_by(user_id=current_user.id).order_by(DataContextCollection.name).all()
        return jsonify([c.to_dict() for c in collections])
    except Exception as e:
        logging.exception("Error fetching collections.")
        return jsonify({"error": str(e)}), 500

@blueprint.route('/api/collections', methods=['POST'])
@login_required
def create_collection():
    """Creates a new DataContextCollection."""
    data = request.json
    name = data.get('name')
    source_type = data.get('source_type')
    collection_mode = data.get('collection_mode')
    source_metadata = data.get('source_metadata')

    if not name or not source_type or not source_metadata or not collection_mode:
        return jsonify({"error": "Missing required fields: name, source_type, collection_mode, source_metadata"}), 400
    
    try:
        new_collection = DataContextCollection(
            user_id=current_user.id,
            name=name,
            source_type=source_type,
            collection_mode=collection_mode,
            source_metadata=source_metadata
        )
        db.session.add(new_collection)
        db.session.flush() # Flush to get the ID for the path
        
        # storage_path is relative to the main /uploads folder
        new_collection.storage_path = f"{current_user.id}/collections/{new_collection.id}"
        
        db.session.commit()
        return jsonify(new_collection.to_dict()), 201
    except Exception as e:
        db.session.rollback()
        logging.exception("Error creating collection.")
        return jsonify({"error": str(e)}), 500

@blueprint.route('/api/collections/<int:collection_id>/upload', methods=['POST'])
@login_required
def upload_to_collection(collection_id):
    """Handles file uploads for a 'document_upload' collection."""
    collection = db.session.get(DataContextCollection, collection_id)
    if not collection or collection.user_id != current_user.id:
        return jsonify({"error": "Collection not found"}), 404
    
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
    
    # Try getting engine from request form, fallback to collection's default metadata
    form_engine = request.form.get('engine')
    default_engine = (collection.source_metadata or {}).get('engine', 'auto')
    engine = form_engine if form_engine else default_engine

    kwargs = {}
    boolean_keys = [
        'evaluate',
        'removehf',
        'ignore_footnotes',
        'llm_boundary_merge',
        'extract_images',
        'metadata_prepass',
        'metadata_delete_flagged_pages',
        'metadata_exclude_flagged_pages',
        'prompt_cache',
        'use_doc_unwarping',
        'use_doc_orientation_classify',
        'paddle_remove_hf',
        'merge_tables',
        'relevel_titles',
        'paddle_preserve_footnotes',
        'paddle_chart_rec',
        'paddle_seal_rec',
        'concatenate_pages',
        'paginate_output',
    ]
    for key in boolean_keys:
        val = request.form.get(key)
        if val is not None:
            kwargs[key] = val.lower() == 'true'

    batch_size = request.form.get('batch_size') or request.form.get('paddle_batch_size')
    if batch_size:
        kwargs['batch_size'] = int(batch_size)

    if request.form.get('layout_shape_mode'):
        kwargs['layout_shape_mode'] = request.form.get('layout_shape_mode')
    if request.form.get('parsing_model'):
        kwargs['parsing_model'] = request.form.get('parsing_model')
    if request.form.get('parallel_pages'):
        kwargs['pdf_options'] = kwargs.get('pdf_options', {})
        kwargs['pdf_options']['parallel_pages'] = int(request.form.get('parallel_pages'))
    if request.form.get('metadata_pages'):
        kwargs['pdf_options'] = kwargs.get('pdf_options', {})
        kwargs['pdf_options']['metadata_pages'] = int(request.form.get('metadata_pages'))
    if request.form.get('metadata_end_pages'):
        kwargs['pdf_options'] = kwargs.get('pdf_options', {})
        kwargs['pdf_options']['metadata_end_pages'] = int(request.form.get('metadata_end_pages'))
    if request.form.get('ocr_language'):
        kwargs['langs'] = request.form.get('ocr_language')

    ocr_strategy = request.form.get('ocr_strategy')
    if engine == 'auto' and ocr_strategy:
        if ocr_strategy == 'force_ocr':
            kwargs['force_ocr'] = True
        elif ocr_strategy == 'no_ocr':
            kwargs['no_ocr'] = True
    
    collection_dir, error = _get_collection_dir(collection)
    if error:
        return jsonify({"error": f"Could not access collection storage: {error}"}), 500

    try:
        filename = secure_filename(file.filename)
        temp_save_path = collection_dir / filename
        file.save(temp_save_path)

        final_path = temp_save_path
        if collection.collection_mode == 'regular':
            if temp_save_path.suffix.lower() in ['.doc', '.docx', '.rtf', '.odt']:
                pdf_path, error = _convert_to_pdf(temp_save_path, collection_dir)
                if error:
                    safe_unlink(temp_save_path) # Clean up temp file safely
                    return jsonify({"error": f"PDF conversion failed: {error}"}), 500
                final_path = pdf_path
                safe_unlink(temp_save_path) # Clean up original file safely
        elif collection.collection_mode != 'vector':
            return jsonify({"error": f"Unsupported collection mode: {collection.collection_mode}"}), 400

        uploads_base_dir = uploads_root()
        web_path = '/uploads/' + final_path.relative_to(uploads_base_dir).as_posix()
        from iarf_assistant.core.document_knowledge import (
            canonical_source_item,
            create_pending_document_source,
        )
        source = create_pending_document_source(
            user_id=current_user.id,
            external_id=f"upload:{uuid.uuid4()}",
            filename=filename,
            file_path=web_path,
            collection_id=collection.id,
            extractor_name=engine,
            extractor_config=kwargs,
            provenance={
                "upload_collection_id": collection.id,
                "stored_path": web_path,
            },
            commit=True,
        )

        from iarf_assistant.core.task_queue import TaskQueue
        queue = TaskQueue(current_app.instance_path, "core_knowledge")
        try:
            job_id = queue.enqueue("process_knowledge_upload", {
                "source_id": source.id,
                "user_id": current_user.id,
                "collection_id": collection.id,
                "collection_mode": collection.collection_mode,
                "file_path": str(final_path),
                "web_path": web_path,
                "filename": filename,
                "preserve_file": True,
                "engine": engine,
                "kwargs": kwargs,
            })
        finally:
            queue.close()

        response_data = {
            "success": True,
            "message": "File upload queued for canonical extraction.",
            "item": canonical_source_item(source),
            "job_id": job_id,
        }
        log_links = _admin_job_log_links()
        if log_links:
            response_data["log_links"] = log_links
        return jsonify(response_data)

    except Exception as e:
        db.session.rollback()
        logging.exception(f"Error processing upload for collection {collection_id}")
        if 'temp_save_path' in locals():
            safe_unlink(temp_save_path)
        return jsonify({"error": str(e)}), 500

@blueprint.route('/api/collections/<int:collection_id>/items', methods=['GET'])
@login_required
def get_collection_items(collection_id):
    """Returns all DataContext items for a specific collection."""
    collection = db.session.get(DataContextCollection, collection_id)
    if not collection or collection.user_id != current_user.id:
        return jsonify({"error": "Collection not found"}), 404
    
    # Sort items, perhaps by filename from metadata
    sorted_items = sorted(collection.items, key=lambda item: (item.json_metadata or {}).get('file_name', item.id))

    items = [item.to_dict() for item in sorted_items]
    return jsonify(items)

@blueprint.route('/api/jobs/<job_id>', methods=['GET'])
@login_required
def get_job_status(job_id):
    from iarf_assistant.core.task_queue import TaskQueue
    queue = TaskQueue(current_app.instance_path, "core_knowledge")
    try:
        status = queue.get_status(job_id)
    finally:
        queue.close()
    log_links = _admin_job_log_links()
    if log_links:
        status = {**status, "log_links": log_links}
    return jsonify(status)


@blueprint.route('/api/collections/<int:collection_id>', methods=['PUT'])
@login_required
def update_collection(collection_id):
    """Updates a collection's name or metadata."""
    collection = db.session.get(DataContextCollection, collection_id)
    if not collection or collection.user_id != current_user.id:
        return jsonify({"error": "Collection not found"}), 404
    
    data = request.json
    if 'name' in data:
        collection.name = data['name']
    if 'source_metadata' in data:
        collection.source_metadata = data['source_metadata']
    if 'collection_mode' in data:
        collection.collection_mode = data['collection_mode']
    
    try:
        db.session.commit()
        return jsonify(collection.to_dict())
    except Exception as e:
        db.session.rollback()
        logging.exception("Error updating collection.")
        return jsonify({"error": str(e)}), 500

@blueprint.route('/api/collections/<int:collection_id>', methods=['DELETE'])
@login_required
def delete_collection(collection_id):
    """Deletes a collection, its stored files, and all associated data context items (including embeddings), even if they belong to other collections."""
    collection = db.session.get(DataContextCollection, collection_id)
    if not collection or collection.user_id != current_user.id:
        return jsonify({"error": "Collection not found"}), 404
    
    try:
        # Delete associated files if a storage path exists
        if collection.storage_path:
            uploads_dir = uploads_root()
            collection_dir = uploads_dir / collection.storage_path
            if collection_dir.exists() and collection_dir.is_dir():
                # Add retry logic for Windows file locking issues (e.g., from OneDrive)
                for i in range(5):
                    try:
                        shutil.rmtree(collection_dir)
                        break # Success
                    except OSError:
                        if i < 4:
                            time.sleep(0.1 * (i + 1)) # Small delay before retrying
                        else:
                            raise # Re-raise after last attempt

        # Delete all data context items associated with this collection.
        # This is destructive and will remove items even if they are in other collections.
        items_to_delete = list(collection.items) # Create a copy to iterate over
        for item in items_to_delete:
            # Also delete associated embeddings
            if hasattr(current_app, 'rag_service'):
                current_app.rag_service.delete_embeddings_for_context(item.id)
            db.session.delete(item)
        
        db.session.delete(collection)
        db.session.commit()
        return jsonify({"success": True, "message": "Collection and all associated data have been deleted."})
    except Exception as e:
        db.session.rollback()
        logging.exception("Error deleting collection.")
        return jsonify({"error": str(e)}), 500

@blueprint.route('/api/collections/<int:collection_id>/export', methods=['GET'])
@login_required
def export_collection(collection_id):
    """Exports a collection to one or more Markdown files based on the new email DB schema."""
    collection = db.session.get(DataContextCollection, collection_id)
    if not collection or collection.user_id != current_user.id:
        return jsonify({"error": "Collection not found"}), 404

    provider = current_app.addon_manager.get_knowledge_provider(collection.source_type)
    if not provider:
        return jsonify({"error": f"No provider found for source type {collection.source_type}."}), 400

    try:
        md_header, content_blocks = provider.export_collection(collection)
    except Exception as e:
        logging.exception("Error exporting collection via provider.")
        return jsonify({"error": f"Export failed: {str(e)}"}), 500

    # --- Splitting Logic ---
    output_format = request.args.get('output', 'file', type=str)
    num_files = request.args.get('num_files', 1, type=int)
    part = request.args.get('part', 1, type=int)
    
    final_content = ""
    filename = ""

    if num_files <= 1 or not content_blocks:
        final_content = md_header + "".join(content_blocks)
        filename = secure_filename(f"collection_{collection.name.replace(' ', '_')}_{collection.id}.md")
    else:
        # Distribute blocks into chunks to balance file sizes
        chunks = [[] for _ in range(num_files)]
        chunk_sizes = [0] * num_files
        for block in content_blocks:
            min_idx = chunk_sizes.index(min(chunk_sizes))
            chunks[min_idx].append(block)
            chunk_sizes[min_idx] += len(block)
        
        if not (1 <= part <= num_files):
            return jsonify({"error": "Invalid part number."}), 400
        
        requested_blocks = chunks[part - 1]
        final_content = md_header + "".join(requested_blocks)
        filename = secure_filename(f"collection_{collection.name.replace(' ', '_')}_{collection.id}_part_{part}_of_{num_files}.md")

    if output_format == 'json':
        return jsonify({"content": final_content, "filename": filename})

    return Response(
        final_content,
        mimetype="text/markdown",
        headers={"Content-disposition": f"attachment; filename=\"{filename}\""}
    )

@blueprint.route('/api/collections/<int:collection_id>/enable-rag', methods=['POST'])
@login_required
def enable_rag_for_collection(collection_id):
    collection = db.session.get(DataContextCollection, collection_id)
    if not collection or collection.user_id != current_user.id:
        return Response(stream_with_context(iter([f"data: {json.dumps({'type': 'error', 'message': 'Collection not found.'})}\n\n"])), mimetype='text/event-stream')

    request_data = request.get_json(silent=True) or request.form.to_dict() or {}
    requested_model = request_data.get("embedding_model")
    requested_dimensions = request_data.get("dimensions")
    if requested_dimensions not in (None, ""):
        try:
            requested_dimensions = int(requested_dimensions)
        except (TypeError, ValueError):
            return Response(
                stream_with_context(iter([
                    f"data: {json.dumps({'type': 'error', 'message': 'Embedding dimensions must be an integer.'})}\n\n"
                ])),
                mimetype='text/event-stream',
            )

    def generate_stream():
        yield f"data: {json.dumps({'type': 'log', 'message': 'Building a versioned semantic projection...'})}\n\n"
        try:
            collection.collection_mode = 'vector'
            db.session.commit()

            if collection.knowledge_memberships:
                from iarf_assistant.core.knowledge_indexing import (
                    rebuild_knowledge_collection_index,
                )
                result = rebuild_knowledge_collection_index(
                    rag_service=current_app.rag_service,
                    user_id=current_user.id,
                    collection_id=collection.id,
                    model_id=requested_model,
                    dimensions=requested_dimensions,
                )
                progress_event = {
                    'type': 'progress',
                    'percent': 100,
                    'message': (
                        f"Activated generation {result['generation_id']} "
                        f"with {result['entry_count']} canonical units."
                    ),
                }
                yield f"data: {json.dumps(progress_event)}\n\n"
                yield f"data: {json.dumps({'type': 'finish', 'message': 'Semantic projection activated atomically.', 'result': result})}\n\n"
                return

            # Compatibility path for collections not yet imported into the
            # canonical source/version schema.
            items = collection.items
            total_items = len(items)
            if total_items == 0:
                yield f"data: {json.dumps({'type': 'finish', 'message': 'Semantic mode enabled. No canonical or legacy items are ready to index.'})}\n\n"
                return

            yield f"data: {json.dumps({'type': 'log', 'message': f'Indexing {total_items} legacy items pending canonical import.'})}\n\n"
            
            for i, item in enumerate(items):
                current_app.rag_service.generate_embeddings_for_context(item.id)
                progress = ((i + 1) / total_items) * 100
                yield f"data: {json.dumps({'type': 'progress', 'percent': progress, 'message': f'Indexed {i+1} of {total_items} items...'})}\n\n"
            
            yield f"data: {json.dumps({'type': 'finish', 'message': 'Legacy semantic index rebuilt; canonical import remains recommended.'})}\n\n"

        except Exception as e:
            db.session.rollback()
            logging.exception(f"Error enabling RAG for collection {collection_id}")
            yield f"data: {json.dumps({'type': 'error', 'message': f'An error occurred: {str(e)}'})}\n\n"

    return Response(stream_with_context(generate_stream()), mimetype='text/event-stream')

@blueprint.route('/api/collections/<int:collection_id>/sync', methods=['GET'])
@login_required
def build_collection_from_db(collection_id):
    """
    Populates a collection by querying the local email database.
    This is a streaming endpoint for real-time progress.
    """
    collection = db.session.get(DataContextCollection, collection_id)
    if not collection or collection.user_id != current_user.id:
        return Response(stream_with_context(iter([f"data: {json.dumps({'type': 'error', 'message': 'Collection not found.'})}\n\n"])), mimetype='text/event-stream')

    provider = current_app.addon_manager.get_knowledge_provider(collection.source_type)
    
    if not provider:
        return Response(stream_with_context(iter([f"data: {json.dumps({'type': 'error', 'message': f'No provider found for source type {collection.source_type}.'})}\n\n"])), mimetype='text/event-stream')

    def generate_build_stream():
        for update in provider.sync_collection_stream(collection):
            yield f"data: {update}\n\n"
            
    return Response(stream_with_context(generate_build_stream()), mimetype='text/event-stream')

@blueprint.route('/api/items/<int:item_id>', methods=['DELETE'])
@login_required
def delete_item(item_id):
    """Deletes a specific DataContext item (document) and its embeddings."""
    item = db.session.get(DataContext, item_id)
    if not item or item.user_id != current_user.id:
        return jsonify({"error": "Document not found"}), 404
        
    try:
        # Delete associated file if it's a document_upload and has file path
        if item.source_type == 'document_upload' and item.source_id and item.source_id.startswith('/uploads/'):
            base_uploads_dir = uploads_root()
            local_path = base_uploads_dir / item.source_id.removeprefix('/uploads/').removeprefix('uploads/')
            safe_unlink(local_path)
            
        # Delete embeddings from vector store
        if hasattr(current_app, 'rag_service'):
            current_app.rag_service.delete_embeddings_for_context(item.id)
            
        db.session.delete(item)
        db.session.commit()
        return jsonify({"success": True, "message": "Document deleted successfully."})
    except Exception as e:
        db.session.rollback()
        logging.exception("Error deleting item.")
        return jsonify({"error": str(e)}), 500

@blueprint.route('/api/clear-all-knowledge', methods=['POST'])
@login_required
def clear_all_knowledge():
    """Deletes all knowledge base data (collections, contexts, chunks, embeddings) for the current user."""
    try:
        # 1. Get all chunk IDs for the current user before deleting them from the main tables
        chunk_ids_to_delete = db.session.query(DataContextChunk.id)\
            .join(DataContext)\
            .filter(DataContext.user_id == current_user.id).all()
        chunk_ids_to_delete = [c[0] for c in chunk_ids_to_delete]

        # 2. Delete the corresponding embeddings from the SQLite-VEC table.
        if chunk_ids_to_delete:
            store = current_app.rag_service.get_main_store(user_id=current_user.id)
            store.delete(chunk_ids_to_delete)
                
        # 3. Clean up physical files (nuke the user's collections folder)
        collections_dir = uploads_root() / str(current_user.id) / 'collections'
        if collections_dir.exists() and collections_dir.is_dir():
            for i in range(5):
                try:
                    shutil.rmtree(collections_dir)
                    break
                except OSError:
                    if i < 4:
                        time.sleep(0.1 * (i + 1))
                    else:
                        logging.warning(f"Could not fully delete collections directory: {collections_dir}")

        # 4. Safely delete from Database using ORM to trigger cascades properly (fixes Foreign Key Error)
        contexts = DataContext.query.filter_by(user_id=current_user.id).all()
        for ctx in contexts:
            db.session.delete(ctx)
            
        collections = DataContextCollection.query.filter_by(user_id=current_user.id).all()
        for coll in collections:
            db.session.delete(coll)
        
        db.session.commit()
        logging.info(f"Cleared all knowledge data for user {current_user.id}.")
        return jsonify({"success": True, "message": "All knowledge base data has been cleared."})
    except Exception as e:
        db.session.rollback()
        logging.exception(f"Error clearing all knowledge data for user {current_user.id}.")
        return jsonify({"error": str(e)}), 500

@blueprint.route('/api/rag-search', methods=['POST'])
@login_required
def rag_search():
    """Performs a RAG search against specified collections."""
    data = request.json
    query_text = data.get('query')
    collection_ids = data.get('collection_ids')

    if not query_text or not collection_ids:
        return jsonify({"error": "Missing 'query' or 'collection_ids'"}), 400
    
    try:
        if "all" in collection_ids:
            collection_ids = [c.id for c in DataContextCollection.query.filter_by(user_id=current_user.id).all()]
        
        top_k_setting = db.session.get(Setting, 'rag_top_k')
        top_k = int(top_k_setting.value) if top_k_setting and top_k_setting.value else 5

        search_results = current_app.rag_service.search(
            query_text=query_text,
            collection_ids=collection_ids,
            top_k=top_k,
            user_id=current_user.id
        )
        return jsonify(search_results)
    except Exception as e:
        logging.exception("Error during RAG search API call.")
        return jsonify({"error": str(e)}), 500
