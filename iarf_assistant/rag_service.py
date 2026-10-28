import logging
import re
from datetime import datetime

from flask import has_app_context
from sqlalchemy import bindparam, text

from .core.vector_backend import SQLITE_VEC_AVAILABLE
from .core.vector_backend import numpy_module as _numpy
from .core.vector_backend import sqlite_vec_module as _sqlite_vec
from .extensions import db
from .llm_service import LLMService
from .models import (
    DataContext,
    DataContextChunk,
    KnowledgeIndexEntry,
    KnowledgeIndexGeneration,
    KnowledgeSourceUnit,
    Setting,
)

LEGACY_VECTOR_ROOT_PATTERN = re.compile(r"^(?:vec_main|fts_vec_main)_[1-9][0-9]*$")
if not SQLITE_VEC_AVAILABLE:
    logging.warning("The 'sqlite-vec' package is not installed. RAG functionality will be disabled.")


def resolve_embedding_profile(*args, **kwargs):
    from .embedding_profiles import resolve_embedding_profile as resolve

    return resolve(*args, **kwargs)

class IsolatedVectorStore:
    """
    An isolated, dimension-aware vector space built on sqlite-vec and FTS5.
    Automatically handles Hybrid Search (Dense + Sparse), RRF merging, and
    provider-specific embedding behavior through embedding profiles.
    """
    def __init__(self, namespace: str, dimensions: int, model: str, task_type: str = "search", user_id: int = None):
        self.namespace = namespace
        self.dimensions = dimensions
        self.model = model
        self.table_name = f"vec_{namespace}_{dimensions}"
        self.fts_table_name = f"fts_vec_{namespace}_{dimensions}"
        self.task_type = task_type
        from flask import has_request_context
        from flask_login import current_user
        
        if user_id is not None:
            self.user_id = user_id
        elif has_request_context() and getattr(current_user, 'is_authenticated', False):
            self.user_id = current_user.id
        else:
            self.user_id = None
        self.embedding_profile = resolve_embedding_profile(
            model=model,
            task_type=task_type,
            user_id=self.user_id,
        )

    def create_table(self):
        if not SQLITE_VEC_AVAILABLE: return
        # Create Dense Vector table
        db.session.execute(text(f"CREATE VIRTUAL TABLE IF NOT EXISTS {self.table_name} USING vec0(embedding float[{self.dimensions}]);"))
        # Create Sparse BM25 (FTS5) table
        db.session.execute(text(f"CREATE VIRTUAL TABLE IF NOT EXISTS {self.fts_table_name} USING fts5(text);"))

    def drop_table(self):
        if not SQLITE_VEC_AVAILABLE: return
        db.session.execute(text(f"DROP TABLE IF EXISTS {self.table_name};"))
        db.session.execute(text(f"DROP TABLE IF EXISTS {self.fts_table_name};"))

    def embed_documents(self, texts: list, titles: list[str] = None) -> list[list[float]]:
        """Generates document vectors using the configured embedding profile."""
        if not texts:
            return []
        np = _numpy()
        llm_service = LLMService(user_id=self.user_id)
        formatted_texts = self.embedding_profile.prepare_documents(texts=texts, titles=titles)
        kwargs = self.embedding_profile.request_kwargs_for_documents(dimensions=self.dimensions)

        provider_batch_size = self.embedding_profile.max_document_batch_size
        batch_size = (
            max(1, int(provider_batch_size))
            if provider_batch_size is not None
            else max(1, len(formatted_texts))
        )
        embeddings = []
        for start in range(0, len(formatted_texts), batch_size):
            request_inputs = formatted_texts[start:start + batch_size]
            resp = llm_service.embedding(
                model=self.model,
                input=request_inputs,
                embedding_profile=self.embedding_profile,
                source="knowledge_document_embedding",
                **kwargs
            )
            response_items = list(getattr(resp, "data", None) or [])
            if len(response_items) != len(request_inputs):
                raise RuntimeError(
                    "Embedding cardinality mismatch: provider returned "
                    f"{len(response_items)} vector(s) "
                    f"for {len(request_inputs)} independent document input(s)."
                )
            for item in response_items:
                emb_val = item.get("embedding") if isinstance(item, dict) else getattr(item, "embedding")
                vec = np.array(emb_val, dtype=np.float32)
                if len(vec) != self.dimensions:
                    raise RuntimeError(
                        f"Embedding provider returned {len(vec)} dimensions; "
                        f"index generation requires {self.dimensions}."
                    )
                if not np.all(np.isfinite(vec)):
                    raise RuntimeError("Embedding provider returned non-finite vector values.")
                embeddings.append(vec.tolist())
        if len(embeddings) != len(texts):
            raise RuntimeError(
                f"Embedding cardinality mismatch: {len(embeddings)} vectors "
                f"for {len(texts)} documents."
            )
        return embeddings

    def embed_query(self, query: str) -> list[float]:
        """Generates a query vector using the configured embedding profile."""
        np = _numpy()
        llm_service = LLMService(user_id=self.user_id)
        formatted_query = self.embedding_profile.prepare_query(query)
        kwargs = self.embedding_profile.request_kwargs_for_query(dimensions=self.dimensions)

        resp = llm_service.embedding(
            model=self.model,
            input=[formatted_query],
            embedding_profile=self.embedding_profile,
            source="knowledge_query_embedding",
            **kwargs
        )
        
        resp_item = resp.data[0]
        # Safely extract the embedding list
        emb_val = resp_item.get("embedding") if isinstance(resp_item, dict) else getattr(resp_item, "embedding")
        
        vec = np.array(emb_val, dtype=np.float32)
        if len(vec) != self.dimensions:
            raise RuntimeError(
                f"Embedding provider returned {len(vec)} query dimensions; "
                f"index generation requires {self.dimensions}."
            )
        if not np.all(np.isfinite(vec)):
            raise RuntimeError("Embedding provider returned non-finite query vector values.")
        return vec.tolist()

    def upsert(
        self,
        row_ids: list[int],
        texts: list,
        titles: list[str] = None,
        embeddings: list[list[float]] = None,
        *,
        include_lexical_text: bool = True,
    ):
        """Generates embeddings (if not provided) and securely bulk-inserts them into the vector AND FTS tables."""
        if not SQLITE_VEC_AVAILABLE or not row_ids: return
        if embeddings is None:
            embeddings = self.embed_documents(texts, titles)
        if not (
            len(row_ids)
            == len(texts)
            == len(embeddings)
        ):
            raise RuntimeError(
                "Vector upsert requires one row ID, text, and embedding per item."
            )
        
        # SQLite virtual tables don't always support INSERT OR REPLACE safely.
        # We explicitly DELETE existing rows before inserting to avoid UNIQUE constraint errors.
        self.delete(row_ids)

        # 1. Insert Dense Vectors
        np = _numpy()
        sqlite_vec = _sqlite_vec()
        vec_data = [
            {"id": r_id, "emb": sqlite_vec.serialize_float32(np.array(emb))}
            for r_id, emb in zip(row_ids, embeddings)
        ]
        db.session.execute(
            text(f"INSERT INTO {self.table_name}(rowid, embedding) VALUES (:id, :emb)"),
            vec_data
        )

        # 2. The legacy store keeps a plaintext FTS projection for backwards
        # compatibility. Canonical knowledge calls with include_lexical_text
        # false and performs lexical search over encrypted source units.
        if include_lexical_text:
            fts_data = [{"id": r_id, "text": txt} for r_id, txt in zip(row_ids, texts)]
            db.session.execute(
                text(f"INSERT INTO {self.fts_table_name}(rowid, text) VALUES (:id, :text)"),
                fts_data
            )

    def search_semantic(
        self,
        query_text: str,
        *,
        top_k: int = 5,
        allowed_row_ids: list[int] | None = None,
    ) -> list[tuple[int, float, float]]:
        """Vector-only search; never invokes or depends on the FTS projection."""
        if not SQLITE_VEC_AVAILABLE:
            return []
        top_k = max(1, int(top_k or 5))
        query_embedding = self.embed_query(query_text)
        np = _numpy()
        sqlite_vec = _sqlite_vec()
        params = {
            "query_vector": sqlite_vec.serialize_float32(
                np.array(query_embedding, dtype=np.float32)
            ),
            "top_k": top_k,
        }
        sql = f"""
            SELECT rowid, distance
            FROM {self.table_name}
            WHERE embedding MATCH :query_vector AND k = :top_k
        """
        allowed = [int(row_id) for row_id in (allowed_row_ids or [])]
        if allowed_row_ids is not None:
            if not allowed:
                return []
            sql += " AND rowid IN :allowed_row_ids"
            statement = text(sql).bindparams(
                bindparam("allowed_row_ids", expanding=True)
            )
            params["allowed_row_ids"] = allowed
        else:
            statement = text(sql)
        rows = db.session.execute(statement, params).fetchall()
        return [
            (
                int(rowid),
                1.0 / (1.0 + max(0.0, float(distance))),
                float(distance),
            )
            for rowid, distance in rows
        ]

    def delete(self, row_ids: list[int]):
        """Deletes vectors and FTS text by their rowid in batches to respect SQLite limitations."""
        if not SQLITE_VEC_AVAILABLE or not row_ids: return
        for i in range(0, len(row_ids), 900):
            chunk = row_ids[i:i+900]
            
            stmt_vec = text(f"DELETE FROM {self.table_name} WHERE rowid IN :ids").bindparams(bindparam('ids', expanding=True))
            db.session.execute(stmt_vec, {"ids": chunk})
            
            stmt_fts = text(f"DELETE FROM {self.fts_table_name} WHERE rowid IN :ids").bindparams(bindparam('ids', expanding=True))
            db.session.execute(stmt_fts, {"ids": chunk})

    def search(self, query_text: str, top_k: int = 5, filter_in_clause: str = None) -> list[tuple[int, float]]:
        """
        Performs a SOTA Hybrid Search:
        1. Embeds the query and runs a KNN search in sqlite-vec.
        2. Runs a BM25 keyword search in FTS5.
        3. Merges the results using Reciprocal Rank Fusion (RRF).
        Optionally applies a pre-formatted SQL 'IN' clause to filter the searchable domain.
        Returns a list of tuples: (rowid, rrf_score)
        """
        if not SQLITE_VEC_AVAILABLE: return []
        
        # We fetch extra candidates (top_k * 3) from both engines to ensure good overlap for RRF
        candidate_k = top_k * 3
        
        # --- 1. DENSE VECTOR SEARCH (Semantic) ---
        query_embedding = self.embed_query(query_text)
        np = _numpy()
        sqlite_vec = _sqlite_vec()
        vec_params = {"query_vector": sqlite_vec.serialize_float32(np.array(query_embedding)), "top_k": candidate_k}
        
        vec_sql = f"""
            SELECT rowid, distance
            FROM {self.table_name}
            WHERE embedding MATCH :query_vector AND k = :top_k
        """
        if filter_in_clause:
            vec_sql += f" AND {filter_in_clause}"
            
        vec_results = db.session.execute(text(vec_sql), vec_params).fetchall()

        # --- 2. SPARSE BM25 SEARCH (Keyword / FTS5) ---
        # Sanitize query for FTS5: strip punctuation, keep alphanumeric, join with OR for fuzzy matching
        clean_query = re.sub(r'[^\w\s]', ' ', query_text)
        words = [w for w in clean_query.split() if len(w) > 2] # Ignore tiny stop words
        fts_query = " OR ".join(words)
        
        fts_results = []
        if fts_query:
            fts_params = {"fts_query": fts_query, "top_k": candidate_k}
            # FTS5 uses 'rank' as the BM25 score (usually negative, lower is better in SQLite)
            fts_sql = f"""
                SELECT rowid, rank
                FROM {self.fts_table_name}
                WHERE {self.fts_table_name} MATCH :fts_query
            """
            if filter_in_clause:
                fts_sql += f" AND {filter_in_clause}"
            
            fts_sql += " ORDER BY rank ASC LIMIT :top_k"
            
            try:
                fts_results = db.session.execute(text(fts_sql), fts_params).fetchall()
            except Exception as e:
                logging.warning(f"FTS5 keyword search failed (query: {fts_query}): {e}")

        # --- 3. RECIPROCAL RANK FUSION (RRF) ---
        rrf_scores = {}
        k_rrf = 60 # Standard RRF constant
        
        # Rank vector results (distance: lower is better)
        for rank, (rowid, distance) in enumerate(vec_results):
            if rowid not in rrf_scores: rrf_scores[rowid] = 0.0
            rrf_scores[rowid] += 1.0 / (k_rrf + rank + 1)

        # Rank FTS results (rank: lower/more negative is better in SQLite FTS5)
        for rank, (rowid, bm25_score) in enumerate(fts_results):
            if rowid not in rrf_scores: rrf_scores[rowid] = 0.0
            rrf_scores[rowid] += 1.0 / (k_rrf + rank + 1)

        # Sort by RRF score descending (higher is better)
        sorted_results = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
        
        # Return top_k
        return sorted_results[:top_k]


class RAGService:
    """Manages the main application vector embeddings and exposes isolated stores to addons."""
    def __init__(self):
        self.db_path = None
        self.is_available = SQLITE_VEC_AVAILABLE

    def init_app(self, app):
        self.db_path = app.config['SQLALCHEMY_DATABASE_URI'].replace('sqlite:///', '')
        if self.is_available:
            logging.info(f"RAGService initialized with DB at {self.db_path}")

    def _get_app_config(self):
        model = 'openai/text-embedding-3-small'
        dim = 768
        chunk_size = 5000
        chunk_overlap = 500
        task_type = 'search'
        try:
            if has_app_context():
                m_set = db.session.get(Setting, 'rag_embedding_model')
                d_set = db.session.get(Setting, 'rag_embedding_dimensions')
                cs_set = db.session.get(Setting, 'rag_chunk_size')
                co_set = db.session.get(Setting, 'rag_chunk_overlap')
                t_set = db.session.get(Setting, 'rag_task_type')
                
                if m_set and m_set.value: model = m_set.value
                if d_set and d_set.value: dim = int(d_set.value)
                if cs_set and cs_set.value: chunk_size = int(cs_set.value)
                if co_set and co_set.value: chunk_overlap = int(co_set.value)
                if t_set and t_set.value: task_type = t_set.value
        except Exception:
            pass
        return model, dim, chunk_size, chunk_overlap, task_type

    def create_isolated_store(self, namespace: str, dimensions: int, model: str, task_type: str = "search", user_id: int = None) -> IsolatedVectorStore:
        """Factory method used by Addons to generate isolated vector spaces."""
        store = IsolatedVectorStore(namespace, dimensions, model, task_type, user_id=user_id)
        store.create_table()
        return store

    def get_main_store(self, user_id: int = None) -> IsolatedVectorStore:
        model, dim, _, _, task_type = self._get_app_config()
        store = IsolatedVectorStore("main", dim, model, task_type=task_type, user_id=user_id)
        store.create_table()
        return store

    def get_knowledge_generation_store(
        self,
        generation: KnowledgeIndexGeneration,
    ) -> IsolatedVectorStore:
        signature = dict(generation.signature_json or {})
        model = str(signature.get("embedding_model") or "").strip()
        try:
            dimensions = int(signature.get("dimensions"))
        except (TypeError, ValueError):
            raise ValueError("Knowledge index signature requires integer dimensions.") from None
        if not model or dimensions <= 0:
            raise ValueError(
                "Knowledge index signature requires embedding_model and dimensions."
            )
        task = str(signature.get("task_type") or "search").strip().lower()
        task_type = {
            "search result": "search",
            "question answering": "qa",
            "fact checking": "fact",
            "code retrieval": "code",
        }.get(task, task)
        namespace = "knowledge_" + re.sub(
            r"[^a-zA-Z0-9_]",
            "_",
            generation.id,
        )
        store = IsolatedVectorStore(
            namespace,
            dimensions,
            model,
            task_type=task_type,
            user_id=generation.user_id,
        )
        store.create_table()
        return store

    def build_knowledge_generation(
        self,
        generation_id: str,
        *,
        batch_size: int = 100,
    ) -> KnowledgeIndexGeneration:
        """Build a generation in isolation and mark ready only after validation."""
        from iarf_assistant.core.knowledge_fabric import (
            build_index_generation,
            mark_index_generation_failed,
            mark_index_generation_ready,
        )

        generation = db.session.get(KnowledgeIndexGeneration, generation_id)
        if generation is None:
            raise ValueError("Knowledge index generation is unavailable.")
        if generation.status != "building":
            raise ValueError("Only a building knowledge generation can be built.")
        # The generation row is the durable audit boundary for the build.  It
        # must survive a rollback in the first provider batch so the failure is
        # observable and can never be mistaken for an absent/not-yet-started
        # build.
        db.session.commit()
        try:
            build_index_generation(
                generation.id,
                mark_ready=False,
                commit=False,
            )
            db.session.flush()
            entries = (
                KnowledgeIndexEntry.query
                .filter_by(generation_id=generation.id)
                .order_by(KnowledgeIndexEntry.ordinal.asc())
                .all()
            )
            store = self.get_knowledge_generation_store(generation)
            batch_size = max(1, min(int(batch_size or 100), 500))
            for start in range(0, len(entries), batch_size):
                batch = entries[start:start + batch_size]
                units = KnowledgeSourceUnit.query.filter(
                    KnowledgeSourceUnit.id.in_([
                        entry.source_unit_id for entry in batch
                    ])
                ).all()
                unit_map = {unit.id: unit for unit in units}
                ordered_units = [unit_map[entry.source_unit_id] for entry in batch]
                texts = [unit.content for unit in ordered_units]
                titles = [
                    unit.source_version.source.title or "none"
                    for unit in ordered_units
                ]
                embeddings = store.embed_documents(texts, titles)
                store.upsert(
                    [entry.id for entry in batch],
                    texts,
                    titles,
                    embeddings=embeddings,
                    include_lexical_text=False,
                )
                db.session.commit()

            vector_count = int(
                db.session.execute(
                    text(f"SELECT count(*) FROM {store.table_name}")
                ).scalar()
                or 0
            )
            lexical_copy_count = int(
                db.session.execute(
                    text(f"SELECT count(*) FROM {store.fts_table_name}")
                ).scalar()
                or 0
            )
            if vector_count != len(entries):
                raise RuntimeError(
                    f"Generation vector count is {vector_count}; expected {len(entries)}."
                )
            if lexical_copy_count != 0:
                raise RuntimeError(
                    "Canonical generation unexpectedly copied plaintext into FTS."
                )
            return mark_index_generation_ready(
                generation.id,
                health={
                    "semantic_projection_ready": True,
                    "vector_count": vector_count,
                    "plaintext_fts_row_count": lexical_copy_count,
                    "embedding_model": (
                        generation.signature_json or {}
                    ).get("embedding_model"),
                    "dimensions": (
                        generation.signature_json or {}
                    ).get("dimensions"),
                },
                commit=True,
            )
        except Exception as exc:
            db.session.rollback()
            mark_index_generation_failed(
                generation.id,
                exc,
                details={"phase": "semantic_projection_build"},
                commit=True,
            )
            raise

    def semantic_search_knowledge(
        self,
        *,
        generation_id: str,
        query: str,
        unit_ids: list[str],
        limit: int,
        user_id: int,
    ) -> list[dict]:
        generation = db.session.get(KnowledgeIndexGeneration, generation_id)
        if (
            generation is None
            or generation.user_id != user_id
            or generation.status != "active"
        ):
            return []
        entries = KnowledgeIndexEntry.query.filter(
            KnowledgeIndexEntry.generation_id == generation.id,
            KnowledgeIndexEntry.source_unit_id.in_(unit_ids),
        ).all()
        if not entries:
            return []
        entry_map = {entry.id: entry for entry in entries}
        store = self.get_knowledge_generation_store(generation)
        rows = store.search_semantic(
            query,
            top_k=max(1, int(limit or 5)),
            allowed_row_ids=list(entry_map),
        )
        return [
            {
                "unit_id": entry_map[row_id].source_unit_id,
                "score": score,
                "distance": distance,
                "generation_id": generation.id,
            }
            for row_id, score, distance in rows
            if row_id in entry_map
        ]

    def query_knowledge(
        self,
        *,
        user_id: int,
        query: str = "",
        strategy: str = "auto",
        collection_ids: list[int] | None = None,
        source_ids: list[str] | None = None,
        filters: dict | None = None,
        limit: int = 10,
        include_content: bool = False,
        snippet_chars: int = 1200,
        fields: list[str] | None = None,
        order_by: str = "relevance",
        program: dict | None = None,
    ) -> dict:
        """Run the typed planner over active canonical semantic projections."""
        from iarf_assistant.core.knowledge_fabric import (
            query_knowledge,
        )

        normalized_collection_ids = [
            int(item)
            for item in (collection_ids or [])
            if str(item).lower() != "all"
        ]
        generation_query = KnowledgeIndexGeneration.query.filter_by(
            user_id=user_id,
            projection_type="hybrid",
            status="active",
        )
        if normalized_collection_ids:
            generation_query = generation_query.filter(
                KnowledgeIndexGeneration.collection_id.in_(
                    normalized_collection_ids
                )
            )
        generations = generation_query.order_by(
            KnowledgeIndexGeneration.activated_at.desc(),
        ).all()

        semantic_backend = None
        if generations:
            def semantic_backend(**kwargs):
                fused: dict[str, dict] = {}
                for generation in generations:
                    rows = self.semantic_search_knowledge(
                        generation_id=generation.id,
                        query=kwargs.get("query") or "",
                        unit_ids=kwargs.get("unit_ids") or [],
                        limit=kwargs.get("limit") or limit,
                        user_id=user_id,
                    )
                    # Active generations may use different embedding models or
                    # dimensions. Fuse ranks, never incomparable raw cosine
                    # scores, across those spaces.
                    for rank, row in enumerate(rows, start=1):
                        unit_id = str(row.get("unit_id") or "")
                        if not unit_id:
                            continue
                        payload = fused.setdefault(unit_id, {
                            "unit_id": unit_id,
                            "score": 0.0,
                            "generation_ids": [],
                        })
                        payload["score"] += 1.0 / (60 + rank)
                        payload["generation_ids"].append(generation.id)
                return list(fused.values())

        result = query_knowledge(
            user_id=user_id,
            query=query,
            strategy=strategy,
            collection_ids=normalized_collection_ids,
            source_ids=source_ids,
            filters=filters,
            limit=limit,
            include_content=include_content,
            snippet_chars=snippet_chars,
            fields=fields,
            order_by=order_by,
            semantic_search=semantic_backend,
            program=program,
        )
        result.setdefault("diagnostics", {})["active_index_generation_ids"] = [
            generation.id for generation in generations
        ]
        result["diagnostics"]["active_index_generation_id"] = (
            generations[0].id if len(generations) == 1 else None
        )
        result["diagnostics"]["active_index_signatures"] = [
            {
                "generation_id": generation.id,
                "signature_hash": generation.signature_hash,
                "embedding_model": (
                    generation.signature_json or {}
                ).get("embedding_model"),
                "dimensions": (
                    generation.signature_json or {}
                ).get("dimensions"),
                "lexical_schema_version": (
                    generation.signature_json or {}
                ).get("lexical_schema_version"),
            }
            for generation in generations
        ]
        result["diagnostics"]["semantic_projection_available"] = (
            bool(generations)
        )
        return result

    def _semantic_chunk_document(self, text: str, chunk_size: int, chunk_overlap: int, base_meta: dict) -> list[dict]:
        """
        SOTA Markdown-Aware Chunker: Splits by paragraph blocks to prevent shearing tables/code.
        Tracks active Markdown Headers and Page Numbers, prepending them to every chunk to prevent context loss.
        """
        final_chunks = []
        
        # Split by double newlines to keep structural units (paragraphs, tables) intact
        blocks = re.split(r'\n\n+', text)
        
        current_page = base_meta.get('page', 'Unknown')
        active_headers = {}
        current_chunk_text = ""
        
        def get_prefix():
            hierarchy = " > ".join([active_headers[l] for l in sorted(active_headers.keys())])
            parts = []
            if current_page != 'Unknown': parts.append(f"Page: {current_page}")
            if hierarchy: parts.append(f"Section: {hierarchy}")
            return f"[{' | '.join(parts)}]\n\n" if parts else ""
            
        def commit_chunk(text_to_commit):
            if not text_to_commit.strip(): return
            meta = base_meta.copy()
            meta['page'] = current_page
            hierarchy = " > ".join([active_headers[l] for l in sorted(active_headers.keys())])
            if hierarchy: meta['section'] = hierarchy
            
            # If a single structural block (e.g. a massive table) exceeds chunk size, force a hard split
            if len(text_to_commit) > chunk_size:
                sub_chunks = self._recursive_split_text(text_to_commit, chunk_size, chunk_overlap)
                for sc in sub_chunks:
                    final_chunks.append({'text': get_prefix() + sc.strip(), 'meta': meta})
            else:
                final_chunks.append({'text': get_prefix() + text_to_commit.strip(), 'meta': meta})

        for block in blocks:
            # Check for page markers generated by our extractor (e.g., <!-- Page 5 -->)
            page_match = re.match(r'<!--\s*Page\s+(\d+)\s*-->', block.strip(), re.IGNORECASE)
            if page_match:
                current_page = page_match.group(1)
                # Remove the marker from the text so it doesn't waste LLM tokens
                block = re.sub(r'<!--\s*Page\s+\d+\s*-->\n*', '', block, flags=re.IGNORECASE).strip()
                if not block: continue
                
            # Check for headers to maintain narrative context
            header_match = re.match(r'^(#{1,6})\s+(.*)', block.strip())
            if header_match:
                level = len(header_match.group(1))
                title = header_match.group(2).strip()
                active_headers[level] = title
                # Clear lower level headers
                for k in list(active_headers.keys()):
                    if k > level: del active_headers[k]
                    
            # Check if adding this block exceeds our chunk budget
            if len(current_chunk_text) + len(block) + 2 > chunk_size and current_chunk_text:
                commit_chunk(current_chunk_text)
                current_chunk_text = block # Start new chunk
            else:
                current_chunk_text += ("\n\n" if current_chunk_text else "") + block
                
        if current_chunk_text:
            commit_chunk(current_chunk_text)
            
        return final_chunks

    def recreate_virtual_table(self):
        """Drops all legacy 'main' and FTS tables regardless of dimension, then rebuilds the current one."""
        if not self.is_available: return
        try:
            # Drop vec_main and fts_vec_main tables 
            # (Filtering by 'CREATE VIRTUAL TABLE%' prevents accidental deletion of backing shadow tables like _chunks)
            tables = db.session.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE 'CREATE VIRTUAL TABLE%'"
            )).fetchall()
            
            for (table_name,) in tables:
                if not isinstance(table_name, str) or not LEGACY_VECTOR_ROOT_PATTERN.fullmatch(
                    table_name
                ):
                    continue
                quoted_name = '"' + table_name.replace('"', '""') + '"'
                db.session.execute(text(f"DROP TABLE IF EXISTS {quoted_name};"))
            
            store = self.get_main_store()
            store.create_table()
            logging.info(f"VSS virtual tables reset. Active table is now '{store.table_name}'.")
        except Exception:
            logging.exception("Failed to reset VSS virtual tables.")
            raise

    def _recursive_split_text(self, text: str, chunk_size: int, chunk_overlap: int, separators: list[str] = None) -> list[str]:
        """Robust recursive character text splitter to ensure semantic coherence."""
        if separators is None:
            separators = ["\n\n", "\n", ". ", " ", ""]
            
        if not text: return []
        if len(text) <= chunk_size: return [text]
            
        separator = separators[-1]
        for s in separators:
            if s == "":
                separator = s
                break
            if s in text:
                separator = s
                break
                
        splits = text.split(separator) if separator else list(text)
        
        # If a single split is still too large, force next separator
        if len(splits) == 1 and separator != "" and len(text) > chunk_size:
            next_separators = separators[separators.index(separator) + 1:]
            return self._recursive_split_text(text, chunk_size, chunk_overlap, next_separators)
            
        chunks = []
        current_chunk = []
        current_len = 0
        
        for s in splits:
            # Recursively split oversized single items.
            if separator != "" and len(s) > chunk_size:
                if current_chunk:
                    chunks.append(separator.join(current_chunk))
                    current_chunk = []
                    current_len = 0
                next_separators = separators[separators.index(separator) + 1:]
                chunks.extend(self._recursive_split_text(s, chunk_size, chunk_overlap, next_separators))
                continue
            
            s_len = len(s) + (len(separator) if current_len > 0 else 0)
            if current_len + s_len > chunk_size and current_chunk:
                chunks.append(separator.join(current_chunk))
                
                # Calculate overlap
                overlap_chunk = []
                overlap_len = 0
                for rev_s in reversed(current_chunk):
                    rev_s_len = len(rev_s) + (len(separator) if overlap_len > 0 else 0)
                    if overlap_len + rev_s_len <= chunk_overlap:
                        overlap_chunk.insert(0, rev_s)
                        overlap_len += rev_s_len
                    else:
                        break
                current_chunk = overlap_chunk
                current_len = overlap_len
                
            current_chunk.append(s)
            current_len += len(s) + (len(separator) if current_len > 0 else 0)
            
        if current_chunk:
            chunks.append(separator.join(current_chunk))
            
        return chunks

    def generate_embeddings_for_context(self, data_context_id):
        """Chunks content, creates relational metadata, and batch-upserts vectors."""
        if not self.is_available: return

        data_context = db.session.get(DataContext, data_context_id)
        if not data_context: return
        user_id = data_context.user_id

        logging.info(f"Generating embeddings for DataContext ID: {data_context_id}")
        self.delete_embeddings_for_context(data_context_id)
        db.session.commit()

        model, dim, chunk_size, chunk_overlap, _ = self._get_app_config()

        title_context = "none"
        body_context = ""
        metadata = data_context.json_metadata or {}
        
        if data_context.source_type == 'email_thread':
            try:
                from iarf_assistant.addons.email.models import EmailThread
            except ImportError:
                logging.warning(f"Email addon is not installed. Skipping email_thread {data_context.source_id}.")
                return

            thread = db.session.get(EmailThread, data_context.source_id)
            if thread:
                title_context = thread.subject
                texts_to_process = []
                for message in thread.messages:
                    body_to_use = message.llm_cleaned_body_md if message.llm_cleaned_body_md else message.body_md
                    combined_text = f"{thread.subject}\n{body_to_use}".strip()
                    texts_to_process.append({
                        'text': combined_text,
                        'meta': {
                            'source': 'email_message',
                            'message_id': message.id,
                            'thread_id': thread.id,
                            'subject': thread.subject,
                            'date': message.date.isoformat() if message.date else None,
                            'from': message.sender_email
                        }
                    })
        else:
            title_context = metadata.get('file_name', 'none')
            body_context = data_context.content
            texts_to_process = [{'text': body_context, 'meta': metadata}]

        if not texts_to_process: return

        try:
            # Inject collection attribution
            collection_name = data_context.collections[0].name if data_context.collections else "Unknown"
            for t_data in texts_to_process:
                t_data['meta']['collection_name'] = collection_name

            # 1. Apply Semantic Markdown Chunking
            final_chunks = []
            for t_data in texts_to_process:
                chunks = self._semantic_chunk_document(t_data['text'], chunk_size, chunk_overlap, t_data['meta'])
                final_chunks.extend(chunks)

            if not final_chunks: return

            store = self.get_main_store(user_id=user_id)
            batch_size = 100 # LiteLLM / Provider sweet spot for embedding batches

            # 2. Batch Processing Loop
            for i in range(0, len(final_chunks), batch_size):
                batch = final_chunks[i:i+batch_size]
                
                texts = [c_data['text'] for c_data in batch]
                titles = [title_context] * len(texts)
                
                # A) Precompute embeddings BEFORE opening/flushing DB transaction
                embeddings = store.embed_documents(texts, titles)
                
                # B) Create relational DB entries to get row IDs
                new_chunks_db = []
                for c_data in batch:
                    new_chunk = DataContextChunk(
                        data_context_id=data_context_id,
                        chunk_text=c_data['text'],
                        json_metadata=c_data['meta']
                    )
                    db.session.add(new_chunk)
                    new_chunks_db.append(new_chunk)
                
                db.session.flush() # Force ID generation
                row_ids = [c.id for c in new_chunks_db]
                
                # C) Execute Vector Upsert
                store.upsert(row_ids, texts, titles, embeddings=embeddings)
                
                # D) Commit after each batch to free DB locks
                db.session.commit()
            logging.info(f"Successfully chunked and stored {len(final_chunks)} vectors for DataContext: {data_context_id}")

        except Exception as e:
            db.session.rollback()
            logging.exception(f"Error generating embeddings for DataContext {data_context_id}: {e}")

    def delete_embeddings_for_context(self, data_context_id):
        if not self.is_available: return
        chunk_ids = [c.id for c in DataContextChunk.query.filter_by(data_context_id=data_context_id).all()]
        if not chunk_ids: return

        try:
            data_context = db.session.get(DataContext, data_context_id)
            user_id = data_context.user_id if data_context else None
            DataContextChunk.query.filter_by(data_context_id=data_context_id).delete(synchronize_session=False)
            store = self.get_main_store(user_id=user_id)
            store.delete(chunk_ids)
        except Exception as e:
            logging.exception(f"Error deleting embeddings for DataContext ID {data_context_id}: {e}")
            raise

    def delete_all_embeddings(self):
        if not self.is_available: return
        try:
            db.session.query(DataContextChunk).delete()
            self.recreate_virtual_table()
            db.session.commit()
        except Exception:
            db.session.rollback()
            logging.exception("Error deleting all embeddings.")

    def search(self, query_text, collection_ids=None, top_k=5, user_id=None):
        if not self.is_available: return []
        try:
            store = self.get_main_store(user_id=user_id)
            filter_clause = None

            if collection_ids and "all" not in collection_ids:
                safe_ids = [int(cid) for cid in collection_ids]
                if not safe_ids: return [] 
                ids_string = ",".join(map(str, safe_ids))
                user_check = f" AND dc.user_id = {user_id}" if user_id else ""
                filter_clause = f"""
                    rowid IN (
                        SELECT dc_chunk.id
                        FROM data_context_chunk dc_chunk
                        JOIN data_context dc ON dc.id = dc_chunk.data_context_id
                        JOIN data_context_collection_association dcca ON dcca.context_id = dc.id
                        WHERE dcca.collection_id IN ({ids_string}){user_check}
                    )
                """
            else:
                # Security Fix: Enforce user isolation if no specific collections are requested
                if user_id:
                    filter_clause = f"""
                        rowid IN (
                            SELECT dc_chunk.id
                            FROM data_context_chunk dc_chunk
                            JOIN data_context dc ON dc.id = dc_chunk.data_context_id
                            WHERE dc.user_id = {user_id}
                        )
                    """

            results = store.search(query_text, top_k=top_k, filter_in_clause=filter_clause)
            if not results: return []
            
            chunk_ids = [row[0] for row in results]
            chunks = DataContextChunk.query.filter(DataContextChunk.id.in_(chunk_ids)).all()
            chunk_map = {chunk.id: chunk for chunk in chunks}

            sorted_formatted_chunks = []
            for cid in chunk_ids:
                chunk_obj = chunk_map.get(cid)
                if chunk_obj:
                    meta = chunk_obj.json_metadata or {}
                    meta_parts = []
                    if meta.get('collection_name'): meta_parts.append(f"Collection: \"{meta['collection_name']}\"")
                    if meta.get('file_name'): meta_parts.append(f"File: {meta['file_name']}")
                    if meta.get('page') and meta.get('page') != 'Unknown': meta_parts.append(f"Page: {meta['page']}")
                    if meta.get('section'): meta_parts.append(f"Section: {meta['section']}")
                    if meta.get('subject'): meta_parts.append(f"Subject: \"{meta['subject']}\"")
                    if meta.get('from'): meta_parts.append(f"From: {meta['from']}")
                    if meta.get('date'):
                        try:
                            date_obj = datetime.fromisoformat(meta['date'])
                            meta_parts.append(f"Date: {date_obj.strftime('%d %b %Y')}")
                        except: pass
                    
                    metadata_str = " | ".join(meta_parts)
                    # Text already includes prefixes from the chunker, so just wrap it nicely
                    formatted_chunk = f"### SOURCE DOCUMENT METADATA: [{metadata_str}] ###\n{chunk_obj.chunk_text}"
                    sorted_formatted_chunks.append(formatted_chunk)
            return sorted_formatted_chunks
        except Exception as e:
            logging.exception(f"Error during RAG search: {e}")
            return []
