"""Offline, content-preserving SaT engine for the native segmentation worker."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import math
import os
import sys
import threading
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Literal, NotRequired, Protocol, TypedDict, cast

from carlina.segmentation.native_protocol import (
    JsonValue,
    NativeRequest,
    NativeSegmentationError,
    check_deadline,
)
from carlina.segmentation.sat_assets import (
    SatAssetsError,
    SatModelAssets,
    verified_sat_assets,
)

_CONTRACT = "carlina.sat-native.cpu.v1"
_MODEL_ID = "segment-any-text/sat-12l-sm"
_CPU_PROVIDER = "CPUExecutionProvider"
_BATCH_SIZE = 4
_INTRA_OP_THREADS = 4
_INTER_OP_THREADS = 1
_DEFAULT_THRESHOLD = 0.25
_BOUNDARY_INDEX_CONVENTION = (
    "index is the character before the next sentence; split occurs after index "
    "plus following whitespace"
)

_REQUIRED_RUNTIME_VERSIONS: dict[str, str] = {
    "wtpsplit-lite": "0.2.0",
    "onnxruntime": "1.26.0",
    "numpy": "2.5.2",
    "tokenizers": "0.21.1",
    "huggingface-hub": "0.32.6",
}
_RUNTIME_MODULES: dict[str, str] = {
    "wtpsplit_lite": "wtpsplit-lite",
    "onnxruntime": "onnxruntime",
    "numpy": "numpy",
    "tokenizers": "tokenizers",
    "huggingface_hub": "huggingface-hub",
}


class _OrtSessionOptions(Protocol):
    intra_op_num_threads: int
    inter_op_num_threads: int


class _OnnxRuntimeModule(Protocol):
    def SessionOptions(self) -> _OrtSessionOptions: ...


class _WtpsplitLiteModule(Protocol):
    SaT: _SatConstructor


class _SatConstructor(Protocol):
    # Preserve the third-party constructor's public names; protocol signatures
    # have no implementation body for Vulture to associate with those parameters.
    def __call__(
        self,
        model_name_or_path: Path,  # noqa: V107
        *,
        tokenizer_name_or_path: Path,  # noqa: V107
        ort_providers: list[str],  # noqa: V107
        ort_kwargs: dict[str, object],  # noqa: V107
    ) -> SatModel: ...


class _ParagraphSplitOptions(TypedDict):
    stride: int
    block_size: int
    weighting: Literal["hat"]
    treat_newline_as_space: Literal[True]
    batch_size: int
    do_paragraph_segmentation: Literal[True]
    threshold: NotRequired[float]
    paragraph_threshold: NotRequired[float]


class _Session(Protocol):
    _model_path: str | None


class _LoadedModel(Protocol):
    ort_session: _Session


class _TokenizerSerialization(Protocol):
    def to_str(self) -> str: ...


class _LoadedTokenizer(Protocol):
    tokenizer: _TokenizerSerialization


class SatModel(Protocol):
    """The narrow SaT surface required by the native worker."""

    model: _LoadedModel
    tokenizer: _LoadedTokenizer

    def get_providers(self) -> list[str]: ...

    def split(
        self,
        text: str | list[str],
        *,
        stride: int,
        block_size: int,
        weighting: str,
        treat_newline_as_space: bool,
        batch_size: int,
        # Passed through _ParagraphSplitOptions; preserve the public keyword.
        do_paragraph_segmentation: bool = False,  # noqa: V107
        threshold: float | None = None,
        paragraph_threshold: float | None = None,
    ) -> object: ...

    def predict_proba(
        self,
        text: str,
        *,
        stride: int,
        block_size: int,
        weighting: str,
        batch_size: int,
    ) -> object: ...


def _native_error(code: str) -> NativeSegmentationError:
    return NativeSegmentationError(code)


def _check_request_deadline(deadline_at: float) -> None:
    check_deadline(deadline_at, now=time.time())


def _copy_json_value(value: JsonValue) -> JsonValue:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Identity values must be finite JSON numbers.")
        return value
    if isinstance(value, list):
        return [_copy_json_value(item) for item in value]
    return {key: _copy_json_value(item) for key, item in value.items()}


def _copy_identity(identity: dict[str, JsonValue]) -> dict[str, JsonValue]:
    copied = cast(dict[str, JsonValue], _copy_json_value(identity))
    fingerprint = copied.get("fingerprint")
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint)
    ):
        raise ValueError("Identity must contain a SHA-256 fingerprint.")
    return copied


def _materialize(value: object) -> list[object]:
    if isinstance(value, str | bytes | dict) or not isinstance(value, Iterable):
        raise _native_error("segmentation_response_invalid")
    try:
        return list(cast(Iterable[object], value))
    except NativeSegmentationError:
        raise


def _source_text(request: NativeRequest) -> str:
    if len(request.texts) != 1:
        raise _native_error("segmentation_request_invalid")
    text = request.texts[0]
    if not text.strip():
        raise _native_error("segmentation_request_invalid")
    return text


def _validate_sentences(value: object, source: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise _native_error("segmentation_response_invalid")
    sentences = cast(list[object], value)
    if any(
        not isinstance(sentence, str) or not sentence.strip() for sentence in sentences
    ):
        raise _native_error("segmentation_response_invalid")
    result = cast(list[str], sentences)
    if "".join(result) != source:
        raise _native_error("segmentation_response_invalid")
    return result


def _validate_batch(value: object, sources: tuple[str, ...]) -> list[list[str]]:
    groups = _materialize(value)
    if len(groups) != len(sources):
        raise _native_error("segmentation_response_invalid")
    result: list[list[str]] = []
    for group, source in zip(groups, sources, strict=True):
        if not isinstance(group, list) or not group:
            raise _native_error("segmentation_response_invalid")
        raw_sentences = cast(list[object], group)
        if any(
            not isinstance(sentence, str) or not sentence.strip()
            for sentence in raw_sentences
        ):
            raise _native_error("segmentation_response_invalid")
        sentences = cast(list[str], raw_sentences)
        if "".join(sentences) != source:
            raise _native_error("segmentation_response_invalid")
        result.append(list(sentences))
    return result


def _validate_paragraph_groups(value: object, source: str) -> list[list[str]]:
    groups = _materialize(value)
    result: list[list[str]] = []
    flattened: list[str] = []
    for group in groups:
        if not isinstance(group, list):
            raise _native_error("segmentation_response_invalid")
        raw_sentences = cast(list[object], group)
        if any(
            not isinstance(sentence, str) or not sentence.strip()
            for sentence in raw_sentences
        ):
            raise _native_error("segmentation_response_invalid")
        sentences = cast(list[str], raw_sentences)
        result.append(list(sentences))
        flattened.extend(sentences)
    if not groups or "".join(flattened) != source:
        raise _native_error("segmentation_response_invalid")
    return result


def _validated_threshold(value: float | None, *, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        raise _native_error("segmentation_request_invalid")
    threshold = float(value)
    if not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise _native_error("segmentation_request_invalid")
    return threshold


def _probability_values(value: object, source_length: int) -> list[float]:
    if isinstance(value, tuple):
        if not value:
            raise _native_error("segmentation_response_invalid")
        value = cast(object, value[0])

    pending: list[object] = [value]
    probabilities: list[float] = []
    while pending:
        item = pending.pop()
        tolist = getattr(item, "tolist", None)
        if callable(tolist):
            item = tolist()
        if isinstance(item, list | tuple):
            pending.extend(reversed(cast(Sequence[object], item)))
            continue
        if isinstance(item, bool) or not isinstance(item, int | float):
            raise _native_error("segmentation_response_invalid")
        probability = float(item)
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise _native_error("segmentation_response_invalid")
        probabilities.append(probability)

    if len(probabilities) != source_length:
        raise _native_error("segmentation_response_invalid")
    return probabilities


def _sentence_spans(text: str, indices: list[int]) -> list[dict[str, JsonValue]]:
    spans: list[dict[str, JsonValue]] = []
    offset = 0
    for index in indices:
        split_at = index + 1
        while split_at < len(text) and text[split_at].isspace():
            split_at += 1
        if split_at > offset and text[offset:split_at].strip():
            spans.append({"start": offset, "end": split_at})
        offset = max(offset, split_at)
    if offset < len(text) and text[offset:].strip():
        spans.append({"start": offset, "end": len(text)})
    return spans


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime_modules() -> tuple[dict[str, ModuleType], dict[str, str]]:
    versions: dict[str, str] = {}
    for distribution, expected in _REQUIRED_RUNTIME_VERSIONS.items():
        actual = importlib.metadata.version(distribution)
        if actual != expected:
            raise _native_error("segmentation_runtime_unavailable") from None
        versions[distribution] = actual

    try:
        prefix = Path(sys.prefix).resolve(strict=True)
    except OSError, RuntimeError:
        raise _native_error("segmentation_runtime_unavailable") from None

    modules: dict[str, ModuleType] = {}
    for module_name in _RUNTIME_MODULES:
        module = importlib.import_module(module_name)
        origin = getattr(module, "__file__", None)
        if not isinstance(origin, str):
            raise _native_error("segmentation_runtime_unavailable") from None
        try:
            module_path = Path(origin).resolve(strict=True)
        except OSError, RuntimeError:
            raise _native_error("segmentation_runtime_unavailable") from None
        if not module_path.is_relative_to(prefix):
            raise _native_error("segmentation_runtime_unavailable") from None
        modules[module_name] = module
    return modules, versions


def _session_options(onnxruntime: ModuleType) -> _OrtSessionOptions:
    runtime = cast(_OnnxRuntimeModule, onnxruntime)
    options = runtime.SessionOptions()
    options.intra_op_num_threads = _INTRA_OP_THREADS
    options.inter_op_num_threads = _INTER_OP_THREADS
    return options


def _new_sat_model(
    wtpsplit_lite: ModuleType,
    onnxruntime: ModuleType,
    assets: SatModelAssets,
) -> SatModel:
    module = cast(_WtpsplitLiteModule, wtpsplit_lite)
    constructor = module.SaT
    options = _session_options(onnxruntime)
    return constructor(
        assets.model_dir,
        tokenizer_name_or_path=assets.tokenizer_dir / "tokenizer.json",
        ort_providers=[_CPU_PROVIDER],
        ort_kwargs={"sess_options": options},
    )


def _loaded_model_identity(
    sat: SatModel,
    assets: SatModelAssets,
    runtime: dict[str, str],
) -> dict[str, JsonValue]:
    try:
        expected_path = (assets.model_dir / "model_optimized.onnx").resolve(strict=True)
        model_path_attribute = "_model_path"
        raw_model_path = getattr(sat.model.ort_session, model_path_attribute, None)
        if not isinstance(raw_model_path, str) or not raw_model_path:
            raise _native_error("segmentation_model_invalid") from None
        actual_path = Path(raw_model_path).resolve(strict=True)
        if actual_path != expected_path:
            raise _native_error("segmentation_model_invalid") from None
    except NativeSegmentationError:
        raise
    except OSError, RuntimeError, TypeError, ValueError:
        raise _native_error("segmentation_model_invalid") from None

    providers = sat.get_providers()
    if providers != [_CPU_PROVIDER]:
        raise _native_error("segmentation_runtime_unavailable") from None

    tokenizer_serialization = sat.tokenizer.tokenizer.to_str()
    tokenizer_sha256 = hashlib.sha256(
        tokenizer_serialization.encode("utf-8")
    ).hexdigest()
    body: dict[str, JsonValue] = {
        "contract": _CONTRACT,
        "model_id": _MODEL_ID,
        "asset_fingerprint": assets.fingerprint,
        "tokenizer_sha256": tokenizer_sha256,
        "runtime": dict(runtime),
        "providers": list(providers),
        "batch_size": _BATCH_SIZE,
        "threads": {"intra_op": _INTRA_OP_THREADS, "inter_op": _INTER_OP_THREADS},
        "engine_sha256": _sha256_file(Path(__file__)),
        "protocol_sha256": _sha256_file(Path(__file__).with_name("native_protocol.py")),
    }
    canonical = json.dumps(
        body, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return {"fingerprint": hashlib.sha256(canonical).hexdigest(), **body}


class NativeSatEngine:
    """Serialize inference and enforce source-preserving, offline responses."""

    def __init__(self, sat: SatModel, identity: dict[str, JsonValue]) -> None:
        self._sat = sat
        self._identity = _copy_identity(identity)
        try:
            providers = sat.get_providers()
        except NativeSegmentationError:
            raise
        except Exception:
            raise _native_error("segmentation_runtime_unavailable") from None
        if providers != [_CPU_PROVIDER]:
            raise _native_error("segmentation_runtime_unavailable") from None
        self._providers = list(providers)
        self._lock = threading.Lock()

    @classmethod
    def load(cls, cache_root: Path) -> NativeSatEngine:
        """Load only verified pinned assets and the worker-owned offline runtime."""
        try:
            assets = verified_sat_assets(cache_root)
        except SatAssetsError as exc:
            code = (
                "segmentation_model_missing"
                if exc.code == "model_assets_missing"
                else "segmentation_model_invalid"
            )
            raise _native_error(code) from None
        except Exception:
            raise _native_error("segmentation_runtime_unavailable") from None

        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        try:
            modules, runtime = _runtime_modules()
            sat = _new_sat_model(
                modules["wtpsplit_lite"], modules["onnxruntime"], assets
            )
            identity = _loaded_model_identity(sat, assets, runtime)
            return cls(sat, identity)
        except NativeSegmentationError:
            raise
        except Exception:
            raise _native_error("segmentation_runtime_unavailable") from None

    def health(self) -> dict[str, JsonValue]:
        """Return a defensive, content-free snapshot of loaded engine identity."""
        providers = list(self._providers)
        return {
            "status": "ok",
            "backend": "native",
            "model_id": _MODEL_ID,
            "segmentation_identity": cast(
                dict[str, JsonValue], _copy_json_value(self._identity)
            ),
            "providers": {
                "requested": [_CPU_PROVIDER],
                "selected": list(providers),
                "active": list(providers),
            },
            "cuda_required": False,
            "cuda_active": False,
            "resource_state": {"concurrency_limit": 1},
        }

    def execute(self, request: NativeRequest) -> dict[str, JsonValue]:
        """Execute one parsed RPC request without retaining text in engine state."""
        _check_request_deadline(request.deadline_at)
        if request.operation == "health":
            return self.health()

        fingerprint = self._identity["fingerprint"]
        if (
            request.expected_identity is not None
            and request.expected_identity != fingerprint
        ):
            raise _native_error("segmentation_identity_changed") from None
        if request.operation not in {
            "split",
            "split_batch",
            "paragraphs",
            "boundary-probabilities",
        }:
            raise _native_error("segmentation_request_invalid") from None

        try:
            with self._lock:
                _check_request_deadline(request.deadline_at)
                try:
                    response = self._execute_locked(request)
                except NativeSegmentationError:
                    _check_request_deadline(request.deadline_at)
                    raise
                except Exception:
                    _check_request_deadline(request.deadline_at)
                    raise _native_error("segmentation_inference_failed") from None
                _check_request_deadline(request.deadline_at)
                return response
        except NativeSegmentationError:
            raise

    def _execute_locked(self, request: NativeRequest) -> dict[str, JsonValue]:
        if request.operation == "split":
            text = _source_text(request)
            sentences = self._sat.split(
                text,
                stride=request.stride,
                block_size=request.block_size,
                weighting="hat",
                treat_newline_as_space=True,
                batch_size=_BATCH_SIZE,
            )
            return {
                "sentences": cast(JsonValue, _validate_sentences(sentences, text)),
                "segmentation_identity": self._identity["fingerprint"],
            }

        if request.operation == "split_batch":
            if not request.texts or any(not text.strip() for text in request.texts):
                raise _native_error("segmentation_request_invalid")
            batch = self._sat.split(
                list(request.texts),
                stride=request.stride,
                block_size=request.block_size,
                weighting="hat",
                treat_newline_as_space=True,
                batch_size=_BATCH_SIZE,
            )
            return {
                "sentences_batch": cast(
                    JsonValue, _validate_batch(batch, request.texts)
                ),
                "segmentation_identity": self._identity["fingerprint"],
            }

        if request.operation == "paragraphs":
            text = _source_text(request)
            kwargs: _ParagraphSplitOptions = {
                "stride": request.stride,
                "block_size": request.block_size,
                "weighting": "hat",
                "treat_newline_as_space": True,
                "batch_size": _BATCH_SIZE,
                "do_paragraph_segmentation": True,
            }
            if request.threshold is not None:
                kwargs["threshold"] = _validated_threshold(
                    request.threshold, default=_DEFAULT_THRESHOLD
                )
            if request.paragraph_threshold is not None:
                kwargs["paragraph_threshold"] = _validated_threshold(
                    request.paragraph_threshold, default=_DEFAULT_THRESHOLD
                )
            groups = _validate_paragraph_groups(self._sat.split(text, **kwargs), text)
            paragraphs = [
                joined for group in groups if (joined := "".join(group).strip())
            ]
            return {
                "paragraphs": cast(JsonValue, paragraphs),
                "sentence_paragraphs": cast(JsonValue, groups),
                "segmentation_identity": self._identity["fingerprint"],
            }

        if request.operation == "boundary-probabilities":
            text = _source_text(request)
            threshold = _validated_threshold(
                request.threshold, default=_DEFAULT_THRESHOLD
            )
            raw_probabilities = self._sat.predict_proba(
                text,
                stride=request.stride,
                block_size=request.block_size,
                weighting="hat",
                batch_size=_BATCH_SIZE,
            )
            probabilities = _probability_values(raw_probabilities, len(text))
            indices = [
                index
                for index, probability in enumerate(probabilities)
                if probability > threshold
            ]
            boundaries: list[dict[str, JsonValue]] = [
                {"index": index, "probability": probabilities[index]}
                for index in indices
            ]
            return {
                "threshold": threshold,
                "probabilities": cast(JsonValue, probabilities),
                "boundaries": cast(JsonValue, boundaries),
                "sentence_spans": cast(JsonValue, _sentence_spans(text, indices)),
                "boundary_index_convention": _BOUNDARY_INDEX_CONVENTION,
                "segmentation_identity": self._identity["fingerprint"],
            }

        raise _native_error("segmentation_request_invalid")
