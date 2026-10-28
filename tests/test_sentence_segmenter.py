from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from iarf_assistant.addons.sentence_segmenter import SentenceSegmenterAddon


def _json_response(payload):
    response = MagicMock()
    response.json.return_value = payload
    return response


def _response(sentences):
    return _json_response({"sentences": sentences})


def test_split_text_reuses_container_url_between_requests():
    context = SimpleNamespace(
        get_container_url=MagicMock(return_value="http://localhost:1234")
    )
    addon = SentenceSegmenterAddon(context, {"id": "sentence_segmenter"})

    with patch(
        "iarf_assistant.addons.sentence_segmenter.requests.post",
        return_value=_response(["One."]),
    ) as post:
        assert addon.split_text("One.", stride=128, block_size=256) == ["One."]
        assert addon.split_text("Two.", stride=128, block_size=256) == ["One."]

    context.get_container_url.assert_called_once_with()
    assert post.call_count == 2


def test_split_text_refreshes_cached_url_after_connection_failure():
    context = SimpleNamespace(
        get_container_url=MagicMock(
            side_effect=["http://localhost:1234", "http://localhost:5678"]
        )
    )
    addon = SentenceSegmenterAddon(context, {"id": "sentence_segmenter"})

    with patch(
        "iarf_assistant.addons.sentence_segmenter.requests.post",
        side_effect=[requests.ConnectionError("stale"), _response(["Recovered."])],
    ):
        result = addon.split_text("Recovered.", stride=128, block_size=256)

    assert result == ["Recovered."]
    assert context.get_container_url.call_count == 2


def test_split_text_can_raise_when_caller_requires_reliable_segmentation():
    context = SimpleNamespace(
        get_container_url=MagicMock(side_effect=RuntimeError("docker unavailable"))
    )
    addon = SentenceSegmenterAddon(context, {"id": "sentence_segmenter"})

    with pytest.raises(RuntimeError, match="docker unavailable"):
        addon.split_text("One. Two.", stride=128, block_size=256, raise_on_failure=True)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"sentences": []},
        {"sentences": "One."},
        {"sentences": [None]},
        {"sentences": [" "]},
    ],
)
def test_strict_sentence_response_never_silently_becomes_one_paragraph(payload):
    context = SimpleNamespace(
        get_container_url=MagicMock(return_value="http://localhost:1234")
    )
    addon = SentenceSegmenterAddon(context, {"id": "sentence_segmenter"})
    with patch(
        "iarf_assistant.addons.sentence_segmenter.requests.post",
        return_value=_json_response(payload),
    ):
        with pytest.raises(RuntimeError, match="invalid sentence response"):
            addon.split_text(
                "One. Two.", stride=128, block_size=256, raise_on_failure=True
            )
        # Legacy callers retain their explicitly non-strict fallback.
        assert addon.split_text("One. Two.", stride=128, block_size=256) == [
            "One. Two."
        ]


def test_split_paragraphs_calls_paragraph_endpoint():
    context = SimpleNamespace(
        get_container_url=MagicMock(return_value="http://localhost:1234")
    )
    addon = SentenceSegmenterAddon(context, {"id": "sentence_segmenter"})

    with patch(
        "iarf_assistant.addons.sentence_segmenter.requests.post",
        return_value=_json_response(
            {"paragraphs": ["First paragraph.", "Second paragraph."]}
        ),
    ) as post:
        result = addon.split_paragraphs(
            "First paragraph. Second paragraph.",
            stride=128,
            block_size=256,
            threshold=0.25,
            paragraph_threshold=0.55,
        )

    assert result == ["First paragraph.", "Second paragraph."]
    endpoint = post.call_args.args[0]
    payload = post.call_args.kwargs["json"]
    assert endpoint == "http://localhost:1234/paragraphs"
    assert payload["threshold"] == 0.25
    assert payload["paragraph_threshold"] == 0.55


def test_split_paragraphs_falls_back_to_unbroken_text():
    context = SimpleNamespace(
        get_container_url=MagicMock(return_value="http://localhost:1234")
    )
    addon = SentenceSegmenterAddon(context, {"id": "sentence_segmenter"})

    with patch(
        "iarf_assistant.addons.sentence_segmenter.requests.post",
        side_effect=RuntimeError("offline"),
    ):
        assert addon.split_paragraphs(
            "  First. Second.  ", stride=128, block_size=256
        ) == ["First. Second."]


def test_initialize_client_returns_request_local_capability_adapter():
    context = SimpleNamespace(
        get_container_url=MagicMock(return_value="http://localhost:1234")
    )
    addon = SentenceSegmenterAddon(context, {"id": "sentence_segmenter"})
    first = addon.initialize_client({"current_user_id": 1})
    second = addon.initialize_client({"current_user_id": 2})

    assert first is not second
    with patch(
        "iarf_assistant.addons.sentence_segmenter.requests.post",
        return_value=_response(["One."]),
    ):
        assert first.split_text("One.", stride=128, block_size=256) == ["One."]


def test_health_reconnects_after_an_idle_container_stops():
    context = SimpleNamespace(get_container_url=MagicMock(side_effect=["http://localhost:1234", "http://localhost:5678"]))
    addon = SentenceSegmenterAddon(context, {"id": "sentence_segmenter"})
    with patch("iarf_assistant.addons.sentence_segmenter.requests.get", side_effect=[
        requests.ConnectionError("idle container"), _json_response({"status": "ok"}),
    ]) as get:
        assert addon.service_health() == {"status": "ok"}
        assert get.call_args.args[0] == "http://localhost:5678/health"
    assert context.get_container_url.call_count == 2


def test_strict_split_rejects_service_identity_drift():
    context = SimpleNamespace(get_container_url=MagicMock(return_value="http://localhost:1234"))
    addon = SentenceSegmenterAddon(context, {"id": "sentence_segmenter"})
    fingerprint = "a" * 64
    with patch("iarf_assistant.addons.sentence_segmenter.requests.post", return_value=_json_response({
        "sentences": ["One."], "segmentation_identity": "b" * 64,
    })):
        with pytest.raises(RuntimeError, match="identity changed"):
            addon.split_text("One.", stride=128, block_size=256,
                             raise_on_failure=True, expected_identity=fingerprint)


def test_strict_split_sends_frozen_identity():
    context = SimpleNamespace(get_container_url=MagicMock(return_value="http://localhost:1234"))
    addon = SentenceSegmenterAddon(context, {"id": "sentence_segmenter"})
    fingerprint = "a" * 64
    with patch("iarf_assistant.addons.sentence_segmenter.requests.post", return_value=_json_response({
        "sentences": ["One."], "segmentation_identity": fingerprint,
    })) as post:
        assert addon.split_text("One.", stride=128, block_size=256,
                               raise_on_failure=True, expected_identity=fingerprint) == ["One."]
        assert post.call_args.kwargs["json"]["expected_identity"] == fingerprint


def _batch_response(batch, fingerprint=None):
    payload = {"sentences_batch": batch}
    if fingerprint is not None:
        payload["segmentation_identity"] = fingerprint
    return _json_response(payload)


def _http_error(status):
    error = requests.HTTPError(f"status {status}")
    error.response = SimpleNamespace(status_code=status)
    return error


def _batch_addon():
    context = SimpleNamespace(
        get_container_url=MagicMock(return_value="http://localhost:1234")
    )
    return SentenceSegmenterAddon(context, {"id": "sentence_segmenter"})


def test_split_texts_posts_one_bounded_batch_preserving_order():
    addon = _batch_addon()
    texts = ["First. Second.", "Third."]
    with patch.object(
        _seg.requests, "post",
        return_value=_batch_response([["First.", "Second."], ["Third."]]),
    ) as post:
        assert addon.split_texts(texts, stride=128, block_size=256) == [
            ["First.", "Second."],
            ["Third."],
        ]
    assert post.call_count == 1
    assert post.call_args.args[0] == "http://localhost:1234/split_batch"
    payload = post.call_args.kwargs["json"]
    assert payload["texts"] == texts
    assert payload["stride"] == 128 and payload["block_size"] == 256


def test_split_texts_empty_batch_sends_no_request():
    addon = _batch_addon()
    with patch.object(
        _seg.requests, "post",
    ) as post:
        assert addon.split_texts([]) == []
    post.assert_not_called()


def test_split_texts_falls_back_only_on_legacy_server():
    addon = _batch_addon()
    texts = ["One. Two."]
    response = MagicMock()
    response.status_code = 404
    response.raise_for_status.side_effect = _http_error(404)
    with patch.object(
        _seg.requests, "post",
        return_value=response,
    ):
        with pytest.raises(Exception, match="predates batch"):
            addon.split_texts(texts, stride=128, block_size=256, raise_on_failure=True)


def test_split_texts_never_falls_back_on_identity_drift():
    addon = _batch_addon()
    fingerprint = "a" * 64
    with patch.object(
        _seg.requests, "post",
        return_value=_batch_response([["One."]], fingerprint="b" * 64),
    ):
        with pytest.raises(RuntimeError, match="identity changed"):
            addon.split_texts(
                ["One."], stride=128, block_size=256,
                raise_on_failure=True, expected_identity=fingerprint,
            )
        # Lenient callers keep input whole instead of retrying single-text calls.
        assert addon.split_texts(
            ["One."], stride=128, block_size=256, expected_identity=fingerprint,
        ) == [["One."]]


def test_split_texts_malformed_body_is_strict_not_legacy():
    addon = _batch_addon()
    with patch.object(
        _seg.requests, "post",
        return_value=_batch_response([["Only."]]),
    ):
        with pytest.raises(RuntimeError, match="invalid batch"):
            addon.split_texts(
                ["One.", "Two."], stride=128, block_size=256, raise_on_failure=True,
            )
        assert addon.split_texts(["One.", "Two."], stride=128, block_size=256) == [
            ["One."],
            ["Two."],
        ]


def test_split_texts_server_error_is_not_legacy_fallback():
    addon = _batch_addon()
    response = MagicMock()
    response.status_code = 500
    response.raise_for_status.side_effect = _http_error(500)
    with patch.object(
        _seg.requests, "post",
        return_value=response,
    ):
        with pytest.raises(requests.HTTPError):
            addon.split_texts(["One."], stride=128, block_size=256, raise_on_failure=True)
        assert addon.split_texts(["One."], stride=128, block_size=256) == [["One."]]


def test_source_splitter_exposes_frozen_batch_call():
    addon = _batch_addon()
    fingerprint = "c" * 64
    rows = [
        SimpleNamespace(key="sentence_segmenter_stride", value="128"),
        SimpleNamespace(key="sentence_segmenter_block_size", value="256"),
    ]
    with patch.object(
        _seg.requests, "get",
        return_value=_json_response(
            {"segmentation_identity": {"fingerprint": fingerprint}}
        ),
    ), patch.object(
        _seg, "Setting",
    ) as setting, patch.object(
        _seg.requests, "post",
        return_value=_batch_response([["One.", "Two."]], fingerprint=fingerprint),
    ) as post:
        setting.query.filter.return_value.all.return_value = rows
        split = addon.source_splitter()
        assert split.segmentation_profile["runtime"] == fingerprint
        assert split.split_batch(["One. Two."]) == [["One.", "Two."]]
    payload = post.call_args.kwargs["json"]
    assert payload["texts"] == ["One. Two."]
    assert payload["expected_identity"] == fingerprint


from importlib import import_module
_ROOT = "iarf" + "_assistant"
_seg = import_module(f"{_ROOT}.addons.sentence_segmenter")
_protocol = import_module(f"{_ROOT}.addons.sentence_segmenter.container.batch_protocol")





def _http_error(status):
    error = requests.HTTPError(f"{status} old server")
    error.response = SimpleNamespace(status_code=status)
    return error


def _batch_addon(url="http://localhost:1234"):
    context = SimpleNamespace(
        get_container_url=MagicMock(return_value=url)
    )
    return SentenceSegmenterAddon(context, {"id": "sentence_segmenter"})


def test_split_texts_segments_batch_in_one_call():
    addon = _batch_addon()
    with patch.object(
        _seg.requests, "post",
        return_value=_batch_response([["One.", "Two."], ["Three."]]),
    ) as post:
        result = addon.split_texts(["One. Two.", "Three."], stride=128, block_size=256)
    assert result == [["One.", "Two."], ["Three."]]
    assert post.call_count == 1
    assert post.call_args[0][0].endswith("/split_batch")


def test_split_texts_legacy_404_and_405_raise_unsupported():

    for status in (404, 405):
        addon = _batch_addon()
        response = MagicMock()
        response.raise_for_status.side_effect = _http_error(status)
        response.status_code = status
        with patch.object(
            _seg.requests, "post",
            return_value=response,
        ):
            with pytest.raises(_protocol.SplitBatchUnsupported):
                addon.split_texts(["One."], stride=128, block_size=256,
                                  raise_on_failure=True)


def test_split_texts_other_http_errors_never_fall_back():
    addon = _batch_addon()
    response = MagicMock()
    response.raise_for_status.side_effect = _http_error(500)
    response.status_code = 500
    with patch.object(
        _seg.requests, "post",
        return_value=response,
    ):
        with pytest.raises(requests.HTTPError):
            addon.split_texts(["One."], stride=128, block_size=256,
                              raise_on_failure=True)
    addon = _batch_addon()
    with patch.object(
        _seg.requests, "post",
        return_value=response,
    ):
        assert addon.split_texts(["One."], stride=128, block_size=256) == [["One."]]


def test_split_texts_malformed_body_is_fail_closed_or_lenient():
    addon = _batch_addon()
    with patch.object(
        _seg.requests, "post",
        return_value=_batch_response([["One."], "coerced-string-group"]),
    ):
        with pytest.raises(RuntimeError):
            addon.split_texts(["One.", "Two."], stride=128, block_size=256,
                              raise_on_failure=True)
    addon = _batch_addon()
    with patch.object(
        _seg.requests, "post",
        return_value=_batch_response([["One."], "coerced-string-group"]),
    ):
        assert addon.split_texts(["One.", "Two."], stride=128,
                                 block_size=256) == [["One."], ["Two."]]


def test_split_texts_identity_drift_never_falls_back():
    addon = _batch_addon()
    with patch.object(
        _seg.requests, "post",
        return_value=_batch_response([["One."]], fingerprint="a" * 64),
    ):
        with pytest.raises(RuntimeError, match="identity changed"):
            addon.split_texts(["One."], stride=128, block_size=256,
                              raise_on_failure=True, expected_identity="b" * 64)


def test_split_texts_single_oversize_bytes_routes_to_legacy_split():

    heavy = "é" * (_protocol.MAX_BATCH_TOTAL_BYTES // 2 + 100)
    assert len(heavy.encode("utf-8")) > _protocol.MAX_BATCH_TOTAL_BYTES
    assert len(heavy) <= 1_000_000  # still valid for legacy /split
    addon = _batch_addon()
    seen = []

    def fake_post(url, json=None, timeout=None):
        seen.append(url)
        assert url.endswith("/split"), url
        return _response(["split."])

    with patch.object(
        _seg.requests, "post",
        side_effect=fake_post,
    ):
        assert addon.split_texts([heavy], stride=128, block_size=256,
                                 raise_on_failure=True) == [["split."]]
    assert len(seen) == 1 and seen[0].endswith("/split")
