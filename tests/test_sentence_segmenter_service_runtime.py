from __future__ import annotations

import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "iarf_assistant/addons/sentence_segmenter/container/service_runtime.py"
spec = importlib.util.spec_from_file_location("sat_service_runtime_test", MODULE_PATH)
assert spec is not None and spec.loader is not None
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def test_gate_serializes_inference_and_rejects_overload_without_leaking_slots():
    gate = runtime.InferenceGate(max_waiting=1)
    active = Event()
    release = Event()
    second_entered = Event()
    def first():
        with gate.enter():
            active.set()
            assert release.wait(10)
    def second():
        with gate.enter():
            second_entered.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(first)
        assert active.wait(10)
        b = pool.submit(second)
        import time
        deadline = time.monotonic() + 5
        while not gate.status()["waiting_requests"] and time.monotonic() < deadline:
            time.sleep(.01)
        assert gate.status()["waiting_requests"] == 1
        assert not second_entered.is_set()
        with pytest.raises(RuntimeError, match="capacity_exhausted"), gate.enter():
            pytest.fail("overload reached inference")
        release.set()
        a.result(timeout=10)
        b.result(timeout=10)
    assert gate.status() == {"concurrency_limit": 1, "active_requests": 0,
                             "waiting_requests": 0, "completed_requests": 2}
    with pytest.raises(ValueError), gate.enter():
        raise ValueError("inference failed")
    with gate.enter():
        assert gate.status()["active_requests"] == 1


def test_identity_binds_loaded_artifacts_tokenizer_runtime_config_and_resources(tmp_path, monkeypatch):
    model = tmp_path / "model.onnx"
    server = tmp_path / "server.py"
    model.write_bytes(b"synthetic model")
    server.write_bytes(b"synthetic server")
    tokenizer = ["synthetic tokenizer"]
    sat = SimpleNamespace(
        model=SimpleNamespace(ort_session=SimpleNamespace(_model_path=str(model)),
                              config=SimpleNamespace(lookahead=3)),
        tokenizer=SimpleNamespace(tokenizer=SimpleNamespace(to_str=lambda: tokenizer[0])),
    )
    monkeypatch.setattr(runtime, "version", lambda _: "1.0")
    kwargs = dict(model_id="synthetic-model", providers=["CPUExecutionProvider"],
                  batch_size=4, threads=4, server_path=server)
    baseline = runtime.loaded_identity(sat, **kwargs)
    assert baseline == runtime.loaded_identity(sat, **kwargs)
    assert str(tmp_path) not in str(baseline)
    for change in ({"batch_size": 8}, {"threads": 8}, {"providers": ["CUDAExecutionProvider"]}):
        assert runtime.loaded_identity(sat, **{**kwargs, **change}) != baseline
    tokenizer[0] += " changed"
    assert runtime.loaded_identity(sat, **kwargs) != baseline
    tokenizer[0] = "synthetic tokenizer"
    model.write_bytes(b"changed model")
    assert runtime.loaded_identity(sat, **kwargs) != baseline
    model.unlink()
    with pytest.raises(FileNotFoundError):
        runtime.loaded_identity(sat, **kwargs)


class _ListSat:
    """Fake model with native list support; records exact call shapes."""

    def __init__(self, groups):
        self.groups = groups
        self.calls = []

    def split(self, texts, **kwargs):
        self.calls.append((texts, kwargs))
        assert isinstance(texts, list), "batched inference must receive a list"
        return iter(self.groups)


def _gate():
    return runtime.InferenceGate()


def test_batch_split_runs_one_gated_list_call_with_order():
    sat = _ListSat([[["Second."], ["First."]][1], ["Third."]])
    gate = _gate()
    result = runtime.execute_batch_split(
        sat, gate, ["First.", "Third."], stride=128, block_size=256,
    )
    assert result == [["First."], ["Third."]]
    assert len(sat.calls) == 1
    assert sat.calls[0][0] == ["First.", "Third."]
    assert sat.calls[0][1]["batch_size"] == 4
    assert gate.status()["active_requests"] == 0


def test_batch_split_consumes_lazy_results_inside_the_gate():
    seen = {}

    def lazy():
        seen["active"] = gate.status()["active_requests"]
        yield ["One.", "Two."]

    gate = _gate()
    sat = SimpleNamespace(split=lambda texts, **kwargs: lazy())
    result = runtime.execute_batch_split(
        sat, gate, ["One. Two."], stride=128, block_size=256,
    )
    assert result == [["One.", "Two."]]
    assert seen["active"] == 1


def test_batch_split_rejects_string_groups_without_char_coercion():
    sat = _ListSat([[["One."]], "Two. Three."])
    with pytest.raises(RuntimeError, match="invalid batch"):
        runtime.execute_batch_split(
            sat, _gate(), ["One.", "Two. Three."], stride=128, block_size=256,
        )


def test_batch_split_rejects_cardinality_mismatch():
    sat = _ListSat([[["Only."]]])
    with pytest.raises(RuntimeError, match="invalid batch"):
        runtime.execute_batch_split(
            sat, _gate(), ["One.", "Two."], stride=128, block_size=256,
        )


def test_batch_split_rejects_blank_sentences():
    sat = _ListSat([[[" "]]])
    with pytest.raises(RuntimeError, match="invalid batch"):
        runtime.execute_batch_split(
            sat, _gate(), ["One."], stride=128, block_size=256,
        )


def test_batch_split_has_no_per_text_fallback_and_releases_gate():
    def fail(texts, **kwargs):
        raise TypeError("no list support")

    gate = _gate()
    sat = SimpleNamespace(split=fail)
    with pytest.raises(TypeError):
        runtime.execute_batch_split(
            sat, gate, ["One.", "Two."], stride=128, block_size=256,
        )
    with gate.enter():
        assert gate.status()["active_requests"] == 1


def test_batch_identity_covers_the_shipped_contract(tmp_path, monkeypatch):
    model = tmp_path / "model.onnx"
    server = tmp_path / "server.py"
    protocol = tmp_path / "batch_protocol.py"
    model.write_bytes(b"synthetic model")
    server.write_bytes(b"synthetic server")
    protocol.write_bytes(b"synthetic protocol")
    sat = SimpleNamespace(
        model=SimpleNamespace(ort_session=SimpleNamespace(_model_path=str(model)),
                              config=SimpleNamespace(lookahead=3)),
        tokenizer=SimpleNamespace(tokenizer=SimpleNamespace(to_str=lambda: "tok")),
    )
    monkeypatch.setattr(runtime, "version", lambda _: "1.0")
    kwargs = dict(model_id="synthetic-model", providers=["CPUExecutionProvider"],
                  batch_size=4, threads=4, server_path=server)
    without = runtime.loaded_identity(sat, **kwargs)
    with_contract = runtime.loaded_identity(sat, protocol_path=protocol, **kwargs)
    assert with_contract != without
    assert with_contract == runtime.loaded_identity(sat, protocol_path=protocol, **kwargs)
    protocol.write_bytes(b"changed protocol")
    assert runtime.loaded_identity(sat, protocol_path=protocol, **kwargs) != with_contract
