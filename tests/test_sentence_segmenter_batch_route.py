"""Behavioral tests for the container /split_batch route with stubbed deps."""
from __future__ import annotations

import importlib.util
from importlib import import_module
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = "iarf" + "_assistant"
CONTAINER = Path(
    import_module(f"{_ROOT}.addons.sentence_segmenter.container.batch_protocol").__file__
).parent


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv("WTPSPLIT_REQUIRE_CUDA", "0")
    model = tmp_path / "model.onnx"
    model.write_bytes(b"synthetic model")

    runtime = _load_module("sat_batch_route_runtime", CONTAINER / "service_runtime.py")
    monkeypatch.setattr(runtime, "version", lambda _: "1.0")
    sys.modules["service_runtime"] = runtime
    protocol = _load_module("sat_batch_route_protocol", CONTAINER / "batch_protocol.py")
    sys.modules["batch_protocol"] = protocol

    calls = []

    class FakeSaT:
        def __init__(self, model_id, ort_providers=None, ort_kwargs=None):
            self.model = SimpleNamespace(
                ort_session=SimpleNamespace(_model_path=str(model)),
                config=SimpleNamespace(lookahead=3),
            )
            self.tokenizer = SimpleNamespace(
                tokenizer=SimpleNamespace(to_str=lambda: "tok")
            )

        def get_providers(self):
            return ["CPUExecutionProvider"]

        def split(self, texts, **kwargs):
            calls.append((texts, kwargs))
            return iter([[f"{index}."] for index in range(len(texts))])

    wtpsplit = types.ModuleType("wtpsplit_lite")
    wtpsplit.SaT = FakeSaT
    monkeypatch.setitem(sys.modules, "wtpsplit_lite", wtpsplit)

    ort = types.ModuleType("onnxruntime")
    ort.get_available_providers = lambda: ["CPUExecutionProvider"]

    class SessionOptions:
        def __init__(self):
            self.intra_op_num_threads = 1
            self.inter_op_num_threads = 1

    ort.SessionOptions = SessionOptions
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)

    numpy_stub = types.ModuleType("numpy")
    numpy_stub.asarray = lambda values, dtype=float: list(values)
    monkeypatch.setitem(sys.modules, "numpy", numpy_stub)

    fastapi = types.ModuleType("fastapi")
    routes = {}

    class FastAPI:
        def __init__(self, **kwargs):
            pass

        def get(self, path):
            def deco(fn):
                routes[("GET", path)] = fn
                return fn

            return deco

        def post(self, path):
            def deco(fn):
                routes[("POST", path)] = fn
                return fn

            return deco

    class HTTPException(Exception):
        def __init__(self, status_code, detail=None, headers=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail
            self.headers = headers

    fastapi.FastAPI = FastAPI
    fastapi.HTTPException = HTTPException
    monkeypatch.setitem(sys.modules, "fastapi", fastapi)

    pydantic = types.ModuleType("pydantic")

    def Field(default=None, **kwargs):
        return default

    class BaseModel:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    pydantic.Field = Field
    pydantic.BaseModel = BaseModel
    monkeypatch.setitem(sys.modules, "pydantic", pydantic)

    module = _load_module("sat_batch_route_server", CONTAINER / "server.py")
    handler = routes[("POST", "/split_batch")]
    return SimpleNamespace(
        module=module, handler=handler, calls=calls,
        HTTPException=HTTPException, runtime=runtime, protocol=protocol,
    )


def _request(texts, **overrides):
    payload = {"texts": texts, "stride": 128, "block_size": 256,
               "weighting": "hat", "treat_newline_as_space": True,
               "expected_identity": None}
    payload.update(overrides)
    return SimpleNamespace(**payload)


def test_batch_route_segments_one_gated_call_with_identity_echo(server):
    fingerprint = server.module.SEGMENTATION_IDENTITY["fingerprint"]
    response = server.handler(_request(["Alpha.", "Beta."], expected_identity=fingerprint))
    assert response["sentences_batch"] == [["0."], ["1."]]
    assert response["segmentation_identity"] == fingerprint
    assert len(server.calls) == 1
    assert server.calls[0][0] == ["Alpha.", "Beta."]
    assert server.module.INFERENCE_GATE.status()["completed_requests"] == 1


def test_batch_route_rejects_identity_drift_without_inference(server):
    fingerprint = "d" * 64
    with pytest.raises(server.HTTPException) as excinfo:
        server.handler(_request(["Alpha."], expected_identity=fingerprint))
    assert excinfo.value.status_code == 409
    assert server.calls == []


def test_batch_route_bounds_are_fail_closed(server):
    with pytest.raises(server.HTTPException) as excinfo:
        server.handler(_request(["   "]))
    assert excinfo.value.status_code == 422
    big = "x" * (server.protocol.MAX_BATCH_TEXT_CHARS + 1)
    with pytest.raises(server.HTTPException) as excinfo:
        server.handler(_request([big]))
    assert excinfo.value.status_code == 413
    heavy = "z" * 600_000
    with pytest.raises(server.HTTPException) as excinfo:
        server.handler(_request([heavy, heavy]))
    assert excinfo.value.status_code == 413
    assert server.calls == []


def test_batch_route_serializes_concurrent_batches(server, monkeypatch):
    """Overload with no waiting room is 429 while one batch holds the gate.

    The production gate allows queued waiters, so the overload path is
    exercised with a deliberately configured max_waiting=0 gate (production
    gate untouched). Thread errors are captured, and release/join run under
    finally so no 10s timeout ever stands in for the assertions.
    """
    monkeypatch.setattr(
        server.module, "INFERENCE_GATE", server.runtime.InferenceGate(max_waiting=0)
    )
    entered = threading.Event()
    release = threading.Event()
    errors = []

    def blocking(texts, **kwargs):
        entered.set()
        assert release.wait(10)
        return iter([[f"{index}."] for index in range(len(texts))])

    server.module.sat.split = blocking

    def run(payload):
        try:
            server.handler(payload)
        except Exception as exc:  # captured for assertion, never silent
            errors.append(exc)

    first = threading.Thread(target=run, args=(_request(["One."]),), daemon=True)
    first.start()
    try:
        assert entered.wait(10)
        with pytest.raises(server.HTTPException) as excinfo:
            server.handler(_request(["Two."]))
        assert excinfo.value.status_code == 429
    finally:
        release.set()
        first.join(timeout=10)
    assert not first.is_alive()
    assert errors == []
    # The gate slot was released: a later batch succeeds.
    assert server.handler(_request(["Three."]))["sentences_batch"] == [["0."]]
