"""Adversarial batching/cache review: identity scope, rotation, bounds, cancel.

Synthetic splitters only; sqlite dirs under tmp_path; no live data, no real
ONNX model (managed env lacks wtpsplit_lite, so stub parity only).
"""
from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

_ROOT = "iarf" + "_assistant"
_translator = f"{_ROOT}.addons.translator"
cache_module = import_module(f"{_translator}.segmentation_cache")
_analysis_contract = import_module(f"{_translator}.analysis_contract")
_block_contract = import_module(f"{_translator}.block_contract")
_source_planning = import_module(f"{_translator}.source_planning")
_source_segmentation = import_module(f"{_translator}.source_segmentation")
_sentence_segmenter = import_module(f"{_ROOT}.addons.sentence_segmenter")
_block_planner = import_module(f"{_translator}.block_planner")
_encryption = import_module(f"{_ROOT}.core.encryption")

private_witness = _analysis_contract.private_witness
BlockContractError = _block_contract.BlockContractError
AttestedDocumentSnapshot = _block_contract.AttestedDocumentSnapshot
SourceProviderAttestation = _block_contract.SourceProviderAttestation
segmentation_planner_settings = _source_planning.segmentation_planner_settings
manifest_spans = _source_segmentation.manifest_spans
segment_projection = _source_segmentation.segment_projection
SentenceSegmenterAddon = _sentence_segmenter.SentenceSegmenterAddon
planner = _block_planner
EncryptionService = _encryption.EncryptionService
InstallationKeyStore = _encryption.InstallationKeyStore


@pytest.fixture
def app():
    application = Flask(__name__)
    application.extensions["carlina_encryption"] = EncryptionService(InstallationKeyStore.ephemeral())
    with application.app_context():
        yield application


@pytest.fixture
def cache(app, tmp_path):
    holder = cache_module.SegmentationCache(tmp_path, 7)
    with holder.planning(lambda: pytest.fail("unexpected contention")):
        yield holder


def _profile(lang="en", stride=128):
    return {"splitter": {"runtime": "fp", "stride": stride}, "source_language": lang,
            "planner_settings": {"engine_contract": "c", "source_type": "document"},
            "adapter": "strict-offsets-v1"}


def test_grouping_change_hits_genuine_settings_change_misses(cache):
    calls = []

    def splitter(text):
        calls.append(text)
        return [text]

    base = {"engine_contract": "c", "source_type": "document",
            "translation_max_block_chars": 4000}
    profile_a = {"splitter": {"runtime": "fp"}, "source_language": "en",
                 "planner_settings": segmentation_planner_settings(base),
                 "adapter": "strict-offsets-v1"}
    assert cache.split("Hello. ", splitter, profile_a) == ["Hello. "]
    assert len(calls) == 1
    # Grouping-only change narrows to the same key: warm hit, no inference.
    profile_grouped = {"splitter": {"runtime": "fp"}, "source_language": "en",
                       "planner_settings": segmentation_planner_settings(
                           {**base, "translation_max_block_chars": 8000}),
                       "adapter": "strict-offsets-v1"}
    assert profile_grouped == profile_a
    assert cache.split("Hello. ", splitter, profile_grouped) == ["Hello. "]
    assert len(calls) == 1
    # Genuine segmentation setting change (stride) must miss.
    profile_stride = {"splitter": {"runtime": "fp", "stride": 64}, "source_language": "en",
                      "planner_settings": segmentation_planner_settings(base),
                      "adapter": "strict-offsets-v1"}
    assert cache.split("Hello. ", splitter, profile_stride) == ["Hello. "]
    assert len(calls) == 2
    # Text change and language change must miss.
    assert cache.split("Hello! ", splitter, profile_a) == ["Hello! "]
    assert cache.split("Hello. ", splitter, {**profile_a, "source_language": "pl"}) == ["Hello. "]
    assert len(calls) == 4


def test_second_project_owner_isolated(app, tmp_path):
    text = "Shared. "
    first = cache_module.SegmentationCache(tmp_path, 7)
    with first.planning(lambda: None):
        assert first.split(text, lambda t: [t], _profile()) == ["Shared. "]
        first.flush()
    second = cache_module.SegmentationCache(tmp_path, 9)
    calls = []
    with second.planning(lambda: None):
        out = second.split(text, lambda t: (calls.append(t), [t])[1], _profile())
    assert out == ["Shared. "]
    assert calls == [text]
    assert (second.hits, second.misses) == (0, 1)


def test_tampered_receipt_is_remiss_and_owner_scoped(app, tmp_path):
    import sqlite3

    holder = cache_module.SegmentationCache(tmp_path, 7)
    with holder.planning(lambda: None):
        holder.split("One. Two.", lambda _: ["One.", "Two."], _profile())
        holder.flush()
    with sqlite3.connect(holder.path) as db:
        row = db.execute("SELECT cache_key, ends FROM offsets").fetchone()
        assert row is not None
        db.execute("UPDATE offsets SET receipt='tampered'")
        db.commit()
    calls = []
    later = cache_module.SegmentationCache(tmp_path, 7)
    with later.planning(lambda: None):
        def batch(texts):
            calls.append(list(texts))
            return [["One.", "Two."] for _ in texts]

        out = later.split_many(["One. Two."], lambda t: pytest.fail("must use batch miss path"),
                               _profile(), batch_splitter=batch)
    assert out == [["One. ", "Two."]]
    assert calls == [["One. Two."]]
    assert (later.hits, later.misses) == (0, 1)


def test_exact_ordered_unicode_offsets_with_whitespace(cache):
    projection = "  Cześć.  Jak się masz?  \n  Dobrze.  "
    sentences = ["Cześć.", "Jak się masz?", "Dobrze."]

    def splitter(_):
        return list(sentences)

    out = cache.split(projection, splitter, _profile())
    # Spans retain surrounding whitespace: lossless coverage of the projection.
    assert "".join(out) == projection
    key = private_witness({"owner": 7, "profile": _profile(), "projection": projection},
                          purpose=cache_module.PURPOSE)
    row = cache.connection.execute("SELECT ends FROM offsets WHERE cache_key=?", (key,)).fetchone()
    assert row is None  # still pending until flush
    cache.flush()
    import json

    row = cache.connection.execute("SELECT ends FROM offsets WHERE cache_key=?", (key,)).fetchone()
    ends = json.loads(row[0])
    spans = manifest_spans(projection, ends)
    assert [projection[s:e] for s, e in spans] == out
    assert spans[0][0] == 0 and spans[-1][1] == len(projection)
    assert [part.strip() for part in out] == sentences
    # Order preserved on second project-independent warm read.
    assert cache.split(projection, lambda _: pytest.fail("must hit"), _profile()) == out


def test_generator_mid_iteration_failure_releases_gate_and_poison_free(cache):
    def exploding(_):
        yield "One."
        raise RuntimeError("lazy inference died")

    with pytest.raises(BlockContractError):
        cache.split_many(["One. Two."], exploding, _profile())
    # Nothing staged: a later valid call must run inference exactly once.
    calls = []

    def batch(texts):
        calls.append(list(texts))
        return [["One.", "Two."] for _ in texts]

    out = cache.split_many(["One. Two."], lambda t: pytest.fail("must use batch miss path"),
                           _profile(), batch_splitter=batch)
    assert out == [["One. ", "Two."]]
    assert calls == [["One. Two."]]


def test_cancellation_at_batch_boundary_aborts_without_poison(cache):
    seen = []

    def batch(texts):
        seen.append(list(texts))
        return [[t] for t in texts]

    def cancel_after_first(done, total):
        if done >= 1:
            raise RuntimeError("cancelled at boundary")

    texts = [f"Item {i}. " for i in range(6)]
    with pytest.raises(RuntimeError, match="cancelled at boundary"):
        cache.split_many(texts, lambda t: [t], _profile(),
                         batch_splitter=batch, on_progress=cancel_after_first)
    # First chunk was valid and may be cached; nothing malformed may be stored.
    progress = []
    out = cache.split_many(texts, lambda t: [t], _profile(), batch_splitter=batch,
                           on_progress=lambda d, t: progress.append((d, t)))
    assert out == [[t] for t in texts]
    assert progress and progress[-1] == (len(texts), len(texts))
    assert all(done <= total for done, total in progress)


def test_malformed_batch_does_not_poison_cache(cache):
    with pytest.raises(BlockContractError):
        cache.split_many(["A. ", "B. "], lambda t: [t], _profile(),
                         batch_splitter=lambda texts: [["only-one"]])
    import sqlite3

    with sqlite3.connect(cache.path) as db:
        assert db.execute("SELECT COUNT(*) FROM offsets").fetchone()[0] == 0
    assert cache.pending == {}
    out = cache.split_many(["A. ", "B. "], lambda t: [t], _profile(),
                           batch_splitter=lambda texts: [[t] for t in texts])
    assert out == [["A. "], ["B. "]]


def _batch_addon():
    context = SimpleNamespace(get_container_url=MagicMock(return_value="http://localhost:1234"))
    return SentenceSegmenterAddon(context, {"id": "sentence_segmenter"})


def _ok_response(batch):
    response = MagicMock()
    response.json.return_value = {"sentences_batch": batch}
    response.headers = {}
    return response


def _http_error(status, retry_after=None):
    import requests

    error = requests.HTTPError(f"status {status}")
    error.response = SimpleNamespace(status_code=status,
                                     headers={"Retry-After": retry_after} if retry_after else {})
    return error


def test_split_texts_429_retries_bounded_then_succeeds():
    addon = _batch_addon()
    good = _ok_response([["One.", "Two."]])
    first = MagicMock()
    first.status_code = 429
    first.headers = {"Retry-After": "0"}
    first.raise_for_status.side_effect = _http_error(429, "0")
    with patch(f"{_sentence_segmenter.__name__}.requests.post",
               side_effect=[first, good]) as post, patch(
               f"{_sentence_segmenter.__name__}.time.sleep") as nap:
        out = addon.split_texts(["One. Two."], stride=128, block_size=256, raise_on_failure=True)
    assert out == [["One.", "Two."]]
    assert post.call_count == 2
    assert nap.call_count == 1


def test_split_texts_429_exhausted_raises_not_valid():
    addon = _batch_addon()
    bad = MagicMock()
    bad.status_code = 429
    bad.headers = {}
    bad.raise_for_status.side_effect = _http_error(429)
    with patch(f"{_sentence_segmenter.__name__}.requests.post", return_value=bad) as post, patch(
            f"{_sentence_segmenter.__name__}.time.sleep") as nap:
        with pytest.raises(Exception, match="429"):
            addon.split_texts(["One."], stride=128, block_size=256, raise_on_failure=True)
        # Default budget is exactly 3 attempts with 2 backoff sleeps.
        assert post.call_count == 3
        assert nap.call_count == 2
        # Lenient path keeps input whole; never splits guesses.
        assert addon.split_texts(["One."], stride=128, block_size=256) == [["One."]]


def test_split_texts_409_never_retries_or_falls_back():
    addon = _batch_addon()
    bad = MagicMock()
    bad.status_code = 409
    bad.headers = {}
    bad.raise_for_status.side_effect = _http_error(409)
    with patch(f"{_sentence_segmenter.__name__}.requests.post", return_value=bad) as post, patch(
            f"{_sentence_segmenter.__name__}.time.sleep") as nap:
        with pytest.raises(Exception, match="409"):
            addon.split_texts(["One."], stride=128, block_size=256, raise_on_failure=True)
    assert post.call_count == 1
    assert nap.call_count == 0


def _planner_snapshot(html):
    import hashlib

    from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION

    digest = hashlib.sha256(html.encode()).hexdigest()
    return AttestedDocumentSnapshot(
        id="doc-1", document_id="doc-1", output_html=html, content_hash=digest,
        semantic_content_hash=digest, metadata={}, provenance={},
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )


def test_block_planner_batch_matches_per_text_order():
    html = "<html><body><p>Alpha. Beta.</p><p>Gamma.</p><p>Alpha. Beta.</p></body></html>"
    snapshot = _planner_snapshot(html)
    attestation = SourceProviderAttestation(
        provider_addon_id="doc-processor", provider_capability_id="document.processing",
        owner_id="owner-1", resource_id=snapshot.document_id,
    )
    mapping = {"Alpha. Beta.": ["Alpha.", "Beta."], "Gamma.": ["Gamma."]}
    per_text_calls, batch_calls = [], []

    def per_text(projection):
        per_text_calls.append(projection)
        return list(mapping[projection])

    def batch(projections):
        batch_calls.append(list(projections))
        return [list(mapping[text]) for text in projections]

    per_text_plan = planner.plan_document_blocks(
        snapshot, project_id="p1", source_attestation=attestation,
        source_language="en", sentence_segmenter=per_text,
    )
    assert len(per_text_calls) == 2
    per_text_calls.clear()
    batch_plan = planner.plan_document_blocks(
        snapshot, project_id="p1", source_attestation=attestation,
        source_language="en", sentence_segmenter=per_text,
        sentence_batch_segmenter=batch,
    )
    assert batch_plan.to_dict() == per_text_plan.to_dict()
    assert len(batch_calls) == 1
    assert sorted(batch_calls[0]) == sorted(set(batch_calls[0]))
    assert per_text_calls == []


def test_block_planner_batch_malformed_fails_closed():
    html = "<html><body><p>One. Two.</p></body></html>"
    snapshot = _planner_snapshot(html)
    attestation = SourceProviderAttestation(
        provider_addon_id="doc-processor", provider_capability_id="document.processing",
        owner_id="owner-1", resource_id=snapshot.document_id,
    )
    with pytest.raises(BlockContractError, match="source-sentence-segmentation"):
        planner.plan_document_blocks(
            snapshot, project_id="p1", source_attestation=attestation,
            source_language="en", sentence_segmenter=lambda p: [p],
            sentence_batch_segmenter=lambda texts: [["only-one"]],
        )
    with pytest.raises(BlockContractError, match="source-sentence-segmentation-invalid"):
        planner.plan_document_blocks(
            snapshot, project_id="p1", source_attestation=attestation,
            source_language="en", sentence_segmenter=lambda p: [p],
            sentence_batch_segmenter=lambda texts: ["coerced-string"],
        )
    with pytest.raises(BlockContractError, match="source-sentence-segmentation-invalid"):
        planner.plan_document_blocks(
            snapshot, project_id="p1", source_attestation=attestation,
            source_language="en", sentence_segmenter=lambda p: [p],
            sentence_batch_segmenter="not-callable",
        )


def _legacy_pair():
    full = {"engine_contract": "c", "source_type": "document",
            "translation_max_block_chars": 5500}
    narrowed = {"engine_contract": "c", "source_type": "document"}
    identity = {"runtime": "fp", "stride": 128}
    legacy = {"splitter": identity, "source_language": "en",
              "planner_settings": dict(full), "adapter": "strict-offsets-v1"}
    current = {"splitter": identity, "source_language": "en",
               "planner_settings": dict(narrowed), "adapter": "strict-offsets-v1"}
    return legacy, current


def test_old5500_row_is_cold_without_legacy_fallback(app, tmp_path):
    """Seeds the exact prior full profile; narrowed read without fallback misses."""
    holder = cache_module.SegmentationCache(tmp_path, 7)
    legacy, current = _legacy_pair()
    with holder.planning(lambda: None):
        assert holder.split("Old. Text. ", lambda t: ["Old.", "Text."], legacy) == ["Old. ", "Text. "]
        holder.flush()
    calls = []
    later = cache_module.SegmentationCache(tmp_path, 7)
    with later.planning(lambda: None):
        def batch(texts):
            calls.append(list(texts))
            return [["Old.", "Text."] for _ in texts]

        out = later.split_many(["Old. Text. "], lambda t: pytest.fail("must use batch miss path"),
                               current, batch_splitter=batch)
    assert out == [["Old. ", "Text. "]]
    assert calls == [["Old. Text. "]]
    assert (later.hits, later.misses) == (0, 1)
    assert (later.hits, later.misses) == (0, 1)


def test_old5500_row_promotes_with_exact_legacy_profile(app, tmp_path):
    """Same seed + exact legacy fallback hits, runs no inference, migrates key."""
    import sqlite3

    holder = cache_module.SegmentationCache(tmp_path, 7)
    legacy, current = _legacy_pair()
    with holder.planning(lambda: None):
        assert holder.split("Old. Text. ", lambda t: ["Old.", "Text."], legacy) == ["Old. ", "Text. "]
        holder.flush()
    later = cache_module.SegmentationCache(tmp_path, 7)
    with later.planning(lambda: None):
        out = later.split_many(["Old. Text. "], lambda t: pytest.fail("must not infer"),
                               current, batch_splitter=lambda t: pytest.fail("must not infer"),
                               legacy_profile=legacy)
        assert out == [["Old. ", "Text. "]]
        assert (later.hits, later.misses) == (1, 0)
        later.flush()
    with sqlite3.connect(later.path) as db:
        keys = {row[0] for row in db.execute("SELECT cache_key FROM offsets")}
    active = private_witness({"owner": 7, "profile": current, "projection": "Old. Text. "},
                             purpose=cache_module.PURPOSE)
    assert active in keys and len(keys) == 2


def test_legacy_with_changed_runtime_does_not_promote(cache):
    """A legacy profile that narrows differently (stride change) stays cold."""
    legacy, current = _legacy_pair()
    drifted = {**legacy, "splitter": {"runtime": "fp", "stride": 64}}
    calls = []

    def batch(texts):
        calls.append(list(texts))
        return [["New.", "Text."] for _ in texts]

    out = cache.split_many(["New. Text. "], lambda t: pytest.fail("must use batch miss path"),
                           current, batch_splitter=batch,
                           legacy_profile=drifted)
    assert out == [["New. ", "Text. "]]
    assert calls == [["New. Text. "]]


def test_marker_without_name_triggers_typed_fallback(cache):
    """Duck-typed marker contract works without the legacy class name."""

    class LegacyServer(RuntimeError):
        carlina_batch_unsupported = True

    seen = []

    def old_server(texts):
        raise LegacyServer("predates batch")

    out = cache.split_many(["A. ", "B. "], lambda t: (seen.append(t), [t])[1], _profile(),
                           batch_splitter=old_server)
    assert out == [["A. "], ["B. "]]
    assert seen == ["A. ", "B. "]


def test_retry_after_nan_inf_negative_and_close_semantics():
    import requests

    addon = _batch_addon()

    def _resp(retry_after):
        response = MagicMock()
        response.status_code = 429
        response.headers = {"Retry-After": retry_after}
        error = requests.HTTPError("status 429")
        error.response = SimpleNamespace(status_code=429)
        response.raise_for_status.side_effect = error
        return response

    good = _ok_response([["One."]])
    for header, expected_sleep in (("nan", 2.0), ("inf", 2.0), ("-3", 0.0), ("1.5", 1.5)):
        first, second = _resp(header), _resp(header)
        with patch(f"{_sentence_segmenter.__name__}.requests.post",
                   side_effect=[first, second, good]) as post, patch(
                   f"{_sentence_segmenter.__name__}.time.sleep") as nap:
            # Two 429s then success with default budget: exactly 3 attempts.
            assert addon.split_texts(["One."], stride=128, block_size=256,
                                     raise_on_failure=True) == [["One."]]
        assert post.call_count == 3
        assert [call.args[0] for call in nap.call_args_list] == [expected_sleep, expected_sleep]
        assert first.close.call_count == 1
        assert second.close.call_count == 1
        assert good.close.call_count == 0


def test_retry_budget_zero_means_single_attempt_no_sleep():
    import requests

    addon = _batch_addon()
    bad = MagicMock()
    bad.status_code = 429
    bad.headers = {}
    error = requests.HTTPError("status 429")
    error.response = SimpleNamespace(status_code=429)
    bad.raise_for_status.side_effect = error
    with patch(f"{_sentence_segmenter.__name__}.requests.post", return_value=bad) as post, patch(
            f"{_sentence_segmenter.__name__}.time.sleep") as nap:
        with pytest.raises(requests.HTTPError):
            addon.split_texts(["One."], stride=128, block_size=256,
                              raise_on_failure=True, max_429_retries=0)
    assert post.call_count == 1
    assert nap.call_count == 0
    assert bad.close.call_count == 0
