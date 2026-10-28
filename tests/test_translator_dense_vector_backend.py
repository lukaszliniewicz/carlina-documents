# pyright: strict
"""Focused acceptance checks for the production dense Translator backend."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from flask import Flask
from sqlalchemy import text

from iarf_assistant.addons.translator.embedding_projection import (
    TranslatorDenseVectorStore,
)
from iarf_assistant.core import vector_backend
from iarf_assistant.core.persistence.sqlite import configure_sqlite_engine
from iarf_assistant.extensions import db
from iarf_assistant.llm_service import LLMService

_NAMESPACE = "translator_emb_native_fixture"
_DIMENSIONS = 3
_ROW_VECTORS = {
    11: [1.0, 0.0, 0.0],
    22: [0.0, 1.0, 0.0],
    33: [-1.0, 0.0, 0.0],
}


@pytest.fixture
def dense_store(tmp_path: Path) -> Iterator[TranslatorDenseVectorStore]:
    application = Flask(__name__)
    application.config["SQLALCHEMY_DATABASE_URI"] = (
        f"sqlite:///{tmp_path / 'vectors.db'}"
    )
    application.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    application.config["SECRET_KEY"] = "translator-dense-vector-test"
    application.config["TESTING"] = True
    db.init_app(application)
    app_context = application.app_context()
    app_context.push()
    try:
        configure_sqlite_engine(db.engine)
        backend = TranslatorDenseVectorStore(
            user_id=7,
            model="synthetic/embedding",
            task_type="similarity",
            dimensions=_DIMENSIONS,
            session=db.session,
        )
        backend.namespace = _NAMESPACE
        backend.create(namespace=_NAMESPACE, dimensions=_DIMENSIONS)
        yield backend
    finally:
        db.session.remove()
        db.engine.dispose()
        app_context.pop()


def test_upsert_overwrite_witness_and_dense_only_table(
    dense_store: TranslatorDenseVectorStore,
) -> None:
    import sqlite_vec  # pyright: ignore[reportMissingTypeStubs]

    dense_store.upsert(list(_ROW_VECTORS), list(_ROW_VECTORS.values()))

    assert sorted(dense_store.row_ids(namespace=_NAMESPACE)) == [11, 22, 33]
    stored_vectors = {
        int(row_id): bytes(vector)
        for row_id, vector in dense_store.session.execute(
            text(f"SELECT rowid, embedding FROM vec_{_NAMESPACE}_{_DIMENSIONS}")
        ).all()
    }
    assert stored_vectors == {
        row_id: sqlite_vec.serialize_float32(vector)
        for row_id, vector in _ROW_VECTORS.items()
    }
    original_witness = dense_store.vector_witness(
        namespace=_NAMESPACE,
        row_ids=[11, 22, 33],
        dimensions=_DIMENSIONS,
    )

    dense_store.upsert([22], [[0.0, 0.0, 1.0]])
    overwritten_witness = dense_store.vector_witness(
        namespace=_NAMESPACE,
        row_ids=[11, 22, 33],
        dimensions=_DIMENSIONS,
    )
    assert overwritten_witness != original_witness
    assert sorted(dense_store.row_ids(namespace=_NAMESPACE)) == [11, 22, 33]

    dense_store.upsert([22], [_ROW_VECTORS[22]])
    assert (
        dense_store.vector_witness(
            namespace=_NAMESPACE,
            row_ids=[11, 22, 33],
            dimensions=_DIMENSIONS,
        )
        == original_witness
    )
    table_names = {
        str(name)
        for (name,) in dense_store.session.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'table'")
        ).all()
    }
    assert not any("fts" in name.lower() for name in table_names)


def test_rag_compatibility_names_share_the_common_lazy_backend() -> None:
    from iarf_assistant import rag_service

    assert rag_service.SQLITE_VEC_AVAILABLE is vector_backend.SQLITE_VEC_AVAILABLE
    rag_numpy = rag_service._numpy  # pyright: ignore[reportPrivateUsage]
    rag_sqlite_vec = rag_service._sqlite_vec  # pyright: ignore[reportPrivateUsage]
    assert rag_numpy is vector_backend.numpy_module
    assert rag_sqlite_vec is vector_backend.sqlite_vec_module
    assert rag_numpy() is vector_backend.numpy_module()
    assert rag_sqlite_vec() is vector_backend.sqlite_vec_module()


def test_semantic_search_filters_allowed_rows_and_skips_empty_provider_call(
    dense_store: TranslatorDenseVectorStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite_vec  # pyright: ignore[reportMissingTypeStubs]

    table = f"vec_{_NAMESPACE}_{_DIMENSIONS}"
    dense_store.session.execute(
        text(f"INSERT INTO {table}(rowid, embedding) VALUES (:id, :embedding)"),
        [
            {
                "id": row_id,
                "embedding": sqlite_vec.serialize_float32(vector),
            }
            for row_id, vector in _ROW_VECTORS.items()
        ],
    )

    provider_calls: list[tuple[str, list[str]]] = []

    def mock_embedding(
        self: Any,
        model: str,
        input: list[str],
        **_kwargs: Any,
    ) -> SimpleNamespace:
        provider_calls.append((model, input))
        return SimpleNamespace(data=[SimpleNamespace(embedding=_ROW_VECTORS[11])])

    monkeypatch.setattr(LLMService, "embedding", mock_embedding)

    results = dense_store.search_semantic(
        "synthetic query",
        namespace=_NAMESPACE,
        dimensions=_DIMENSIONS,
        model="synthetic/embedding",
        task_type="similarity",
        top_k=3,
        allowed_row_ids=[22, 33],
    )

    assert [row_id for row_id, _score, _distance in results] == [22, 33]
    assert 11 not in [row_id for row_id, _score, _distance in results]
    assert results[0][2] == pytest.approx(2**0.5, abs=1e-6)
    assert results[0][1] == pytest.approx(1 / (1 + 2**0.5), abs=1e-6)
    assert results[1][2] == pytest.approx(2.0, abs=1e-6)
    assert results[1][1] == pytest.approx(1 / 3, abs=1e-6)
    assert len(provider_calls) == 1

    assert (
        dense_store.search_semantic(
            "synthetic query",
            namespace=_NAMESPACE,
            dimensions=_DIMENSIONS,
            model="synthetic/embedding",
            task_type="similarity",
            top_k=3,
            allowed_row_ids=[],
        )
        == []
    )
    assert len(provider_calls) == 1
