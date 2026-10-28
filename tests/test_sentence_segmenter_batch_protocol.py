"""Focused tests for the bounded batch segmentation contract."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from importlib import import_module
_ROOT = "iarf" + "_assistant"
protocol = import_module(f"{_ROOT}.addons.sentence_segmenter.container.batch_protocol")
protocol_again = import_module(protocol.__name__)


def test_protocol_module_has_single_identity():
    assert protocol is protocol_again


def test_validate_rejects_shape_problems():
    with pytest.raises(ValueError):
        protocol.validate_batch_texts("not-a-list")
    with pytest.raises(ValueError):
        protocol.validate_batch_texts([])
    with pytest.raises(ValueError):
        protocol.validate_batch_texts(["ok"] * 33)
    with pytest.raises(ValueError):
        protocol.validate_batch_texts(["fine", 7])
    with pytest.raises(ValueError):
        protocol.validate_batch_texts(["fine", "   "])


def test_validate_enforces_byte_bounds_not_char_counts():
    # 300k CJK chars are ~900KB: under the char ceiling but near the byte cap.
    heavy = "x" * 600_000
    assert protocol.validate_batch_texts([heavy]) == [heavy]
    with pytest.raises(ValueError, match="bytes"):
        protocol.validate_batch_texts([heavy, heavy])
    with pytest.raises(ValueError, match="characters"):
        protocol.validate_batch_texts(["y" * (protocol.MAX_BATCH_TEXT_CHARS + 1)])


def test_chunking_preserves_order_and_bounds():
    texts = [f"Sentence {index}." for index in range(40)]
    chunks = protocol.chunk_texts(texts)
    assert [len(chunk) for chunk in chunks] == [32, 8]
    assert [text for chunk in chunks for text in chunk] == texts
    heavy = ["z" * 600_000, "z" * 600_000, "small."]
    chunked = protocol.chunk_texts(heavy)
    assert [len(chunk) for chunk in chunked] == [1, 2]
    assert [text for chunk in chunked for text in chunk] == heavy


def test_response_checks_reject_malformed_without_fallback():
    good = {"sentences_batch": [["One.", "Two."], ["Three."]]}
    assert protocol.check_batch_response(good, 2) == [["One.", "Two."], ["Three."]]
    for payload in (
        {"sentences_batch": [["One."]]},
        {"sentences_batch": [["One."], ["Two."], ["Three."]]},
        {"sentences_batch": "One."},
        {"nope": []},
        [],
    ):
        with pytest.raises(RuntimeError, match="invalid batch"):
            protocol.check_batch_response(payload, 2)
    # A bare string group must fail, never splinter into characters.
    with pytest.raises(RuntimeError, match="invalid batch"):
        protocol.check_batch_response({"sentences_batch": [["One."], "Two. Three."]}, 2)
    with pytest.raises(RuntimeError, match="invalid batch"):
        protocol.check_batch_response({"sentences_batch": [[" "]]}, 1)


def test_unsupported_is_a_runtime_error():
    assert issubclass(protocol.SplitBatchUnsupported, RuntimeError)


ADDON = Path(protocol.__file__).resolve().parent.parent


def test_server_uses_the_shared_batch_contract():
    server = (ADDON / "container/server.py").read_text()
    assert "from batch_protocol import MAX_BATCH_TEXT_CHARS, MAX_BATCH_TEXTS, MAX_BATCH_TOTAL_BYTES" in server
    assert "@app.post(\"/split_batch\")" in server
    assert "segmentation_identity_changed" in server
    assert 'protocol_path=Path(__file__).with_name("batch_protocol.py")' in server


def test_batch_protocol_ships_inside_the_container_build():
    dockerfile = (ADDON / "container/Dockerfile").read_text()
    copies = re.findall(r"^COPY\s+(.+?)\s+\./$", dockerfile, re.MULTILINE)
    assert copies, "no COPY to image root"
    assert any("batch_protocol.py" in line.split() for line in copies)


def test_docker_build_context_includes_copied_service_files():
    container = ADDON / "container"
    rules = {
        line.strip()
        for line in (container / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "*" in rules  # The service deliberately uses an explicit allowlist.
    sources = []
    for line in (container / "Dockerfile").read_text().splitlines():
        if line.startswith("COPY "):
            parts = line.split()
            assert parts[-1] == "./"
            sources.extend(parts[1:-1])
    assert "batch_protocol.py" in sources
    for source in sources:
        assert (container / source).is_file(), source
        assert f"!{source}" in rules, f"Docker context excludes {source}"


def test_runtime_fingerprint_covers_the_batch_contract():
    runtime = (ADDON / "container/service_runtime.py").read_text()
    assert "protocol_sha256" in runtime
    assert "def execute_batch_split(" in runtime


def test_translator_chunk_contract_matches_without_cross_addon_imports():
    import ast

    cache_module = import_module(f"{_ROOT}.addons.translator.segmentation_cache")

    assert cache_module.SENTENCE_BATCH_MAX_ITEMS == protocol.MAX_BATCH_TEXTS
    assert cache_module.SENTENCE_BATCH_MAX_BYTES == protocol.MAX_BATCH_TOTAL_BYTES
    translator = ADDON.parent / "translator"
    for name in ("segmentation_cache.py", "source_planning.py", "block_planner.py"):
        tree = ast.parse((translator / name).read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        assert not any(
            part == "sentence_segmenter" for module in imported for part in module.split(".")
        ), (name, sorted(imported))
    assert cache_module._chunk_for_inference(
        [f"Sentence {index}." for index in range(40)]
    ) == protocol.chunk_texts([f"Sentence {index}." for index in range(40)])
