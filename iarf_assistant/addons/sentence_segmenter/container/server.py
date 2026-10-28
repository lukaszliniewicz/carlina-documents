import logging
import os
from pathlib import Path

import numpy as np
import onnxruntime as ort
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from batch_protocol import MAX_BATCH_TEXT_CHARS, MAX_BATCH_TEXTS, MAX_BATCH_TOTAL_BYTES
from service_runtime import InferenceGate, execute_batch_split, loaded_identity
from wtpsplit_lite import SaT

app = FastAPI(title="Wtpsplit-Lite Microservice")
logger = logging.getLogger("uvicorn.error")

INFERENCE_GATE = InferenceGate()
INFERENCE_BATCH_SIZE = max(1, min(32, int(os.getenv("WTPSPLIT_BATCH_SIZE", "4"))))
CPU_THREADS = max(1, min(16, int(os.getenv("WTPSPLIT_CPU_THREADS", "4"))))

DEFAULT_THRESHOLD = 0.25
DEFAULT_MODEL_ID = "sat-12l-sm"
DEFAULT_ORT_PROVIDERS = ["CUDAExecutionProvider", "CPUExecutionProvider"]
REQUIRE_CUDA = str(os.getenv("WTPSPLIT_REQUIRE_CUDA", "1")).strip().lower() not in {"0", "false", "no", "off"}


def _preload_onnx_gpu_libraries() -> None:
    if not hasattr(ort, "preload_dlls"):
        return
    try:
        ort.preload_dlls(cuda=True, cudnn=True, directory="")
        logger.info("onnxruntime.preload_dlls(cuda=True, cudnn=True) completed.")
    except Exception as exc:  # noqa: BLE001 - GPU preload is optional.
        logger.warning("onnxruntime.preload_dlls failed: %s", exc)


_preload_onnx_gpu_libraries()


def _parse_provider_list(raw: str | None) -> list[str]:
    if raw is None:
        return []
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def _cuda_device_visible() -> bool:
    """Return whether Docker exposed an NVIDIA/WSL CUDA device."""

    return any(
        device.exists()
        for device in (
            Path("/dev/nvidiactl"),
            Path("/dev/nvidia0"),
            Path("/dev/dxg"),
        )
    )


def _resolve_provider_state() -> dict:
    available = list(ort.get_available_providers())
    requested = _parse_provider_list(os.getenv("WTPSPLIT_ORT_PROVIDERS"))
    if not requested:
        requested = list(DEFAULT_ORT_PROVIDERS)
    cuda_device_visible = _cuda_device_visible()

    if REQUIRE_CUDA and (
        "CUDAExecutionProvider" not in available or not cuda_device_visible
    ):
        raise RuntimeError(
            "CUDA is required but no usable CUDA provider/device is visible. "
            f"Available providers: {available}. "
            "Verify NVIDIA drivers, nvidia-container-toolkit, and onnxruntime-gpu dependencies."
        )

    selected = [
        provider
        for provider in requested
        if provider in available
        and (provider != "CUDAExecutionProvider" or cuda_device_visible)
    ]
    if "CUDAExecutionProvider" in requested and not cuda_device_visible:
        logger.info("No CUDA device is visible; selecting CPUExecutionProvider.")
    if "CPUExecutionProvider" in available and "CPUExecutionProvider" not in selected:
        selected.append("CPUExecutionProvider")
    if not selected and available:
        selected = [available[0]]

    return {
        "requested": requested,
        "available": available,
        "selected": selected,
        "cuda_device_visible": cuda_device_visible,
    }


MODEL_ID = str(os.getenv("WTPSPLIT_MODEL_ID") or DEFAULT_MODEL_ID).strip() or DEFAULT_MODEL_ID
PROVIDER_STATE = _resolve_provider_state()


def _load_sat_model() -> SaT:
    selected = list(PROVIDER_STATE.get("selected") or [])
    logger.info(
        "Loading SaT model '%s' (requested providers=%s, available providers=%s, selected providers=%s)",
        MODEL_ID,
        PROVIDER_STATE.get("requested"),
        PROVIDER_STATE.get("available"),
        selected,
    )
    options = ort.SessionOptions()
    options.intra_op_num_threads = CPU_THREADS
    options.inter_op_num_threads = 1
    return SaT(MODEL_ID, ort_providers=selected, ort_kwargs={"sess_options": options})


sat = _load_sat_model()
ACTIVE_PROVIDERS = list(getattr(sat, "get_providers", lambda: PROVIDER_STATE.get("selected", []))())
if REQUIRE_CUDA and "CUDAExecutionProvider" not in ACTIVE_PROVIDERS:
    raise RuntimeError(
        "CUDA is required but the loaded session is not using CUDAExecutionProvider. "
        f"Active providers: {ACTIVE_PROVIDERS}."
    )

SEGMENTATION_IDENTITY = loaded_identity(
    sat, model_id=MODEL_ID, providers=ACTIVE_PROVIDERS, batch_size=INFERENCE_BATCH_SIZE,
    threads=CPU_THREADS, server_path=__file__,
    protocol_path=Path(__file__).with_name("batch_protocol.py"),
)


class SplitRequest(BaseModel):
    text: str = Field(max_length=1_000_000)
    stride: int = Field(default=128, ge=1, le=512)
    block_size: int = Field(default=256, ge=1, le=512)
    expected_identity: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    weighting: str = "hat"
    treat_newline_as_space: bool = True


class BatchSplitRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=MAX_BATCH_TEXTS)
    stride: int = Field(default=128, ge=1, le=512)
    block_size: int = Field(default=256, ge=1, le=512)
    expected_identity: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    weighting: str = "hat"
    treat_newline_as_space: bool = True


class BoundaryRequest(SplitRequest):
    threshold: float | None = None


class ParagraphRequest(SplitRequest):
    threshold: float | None = None
    paragraph_threshold: float | None = None


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "segmentation_identity": SEGMENTATION_IDENTITY,
        "resource_state": INFERENCE_GATE.status(),
        "model_id": MODEL_ID,
        "cuda_required": REQUIRE_CUDA,
        "cuda_active": "CUDAExecutionProvider" in ACTIVE_PROVIDERS,
        "providers": {
            "requested": PROVIDER_STATE.get("requested"),
            "available": PROVIDER_STATE.get("available"),
            "selected": PROVIDER_STATE.get("selected"),
            "active": ACTIVE_PROVIDERS,
            "cuda_device_visible": PROVIDER_STATE.get("cuda_device_visible"),
        },
    }

@app.post("/split")
def split_text(req: SplitRequest):
    if req.expected_identity is not None and req.expected_identity != SEGMENTATION_IDENTITY["fingerprint"]:
        raise HTTPException(status_code=409, detail="segmentation_identity_changed")
    try:
        with INFERENCE_GATE.enter():
            sentences = sat.split(
                req.text, stride=req.stride, block_size=req.block_size,
                weighting=req.weighting, treat_newline_as_space=req.treat_newline_as_space,
                batch_size=INFERENCE_BATCH_SIZE,
            )
        return {"sentences": sentences, "segmentation_identity": SEGMENTATION_IDENTITY["fingerprint"]}
    except RuntimeError as exc:
        if str(exc) == "segmentation_capacity_exhausted":
            raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "2"}) from exc
        raise HTTPException(status_code=500, detail="segmentation_failed") from exc
    except Exception as exc:
        logger.exception("Splitting failed")
        raise HTTPException(status_code=500, detail="segmentation_failed") from exc


@app.post("/split_batch")
def split_batch(req: BatchSplitRequest):
    if req.expected_identity is not None and req.expected_identity != SEGMENTATION_IDENTITY["fingerprint"]:
        raise HTTPException(status_code=409, detail="segmentation_identity_changed")
    total = 0
    for index, text in enumerate(req.texts):
        if not text.strip():
            raise HTTPException(status_code=422, detail="batch_item_blank")
        if len(text) > MAX_BATCH_TEXT_CHARS:
            raise HTTPException(status_code=413, detail="batch_item_too_large")
        total += len(text.encode("utf-8"))
        if total > MAX_BATCH_TOTAL_BYTES:
            raise HTTPException(status_code=413, detail="batch_total_too_large")
    try:
        batch = execute_batch_split(
            sat, INFERENCE_GATE, list(req.texts),
            stride=req.stride, block_size=req.block_size,
            weighting=req.weighting,
            treat_newline_as_space=req.treat_newline_as_space,
            batch_size=INFERENCE_BATCH_SIZE,
        )
        return {"sentences_batch": batch, "segmentation_identity": SEGMENTATION_IDENTITY["fingerprint"]}
    except RuntimeError as exc:
        if str(exc) == "segmentation_capacity_exhausted":
            raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "2"}) from exc
        raise HTTPException(status_code=500, detail="segmentation_failed") from exc
    except Exception as exc:
        logger.exception("Batch splitting failed")
        raise HTTPException(status_code=500, detail="segmentation_failed") from exc


def _join_text_parts(parts: list[str]) -> str:
    return "".join(str(part or "") for part in parts).strip()


def _paragraphs_from_split_output(split_output) -> list[str]:
    if not isinstance(split_output, list):
        text = str(split_output or "").strip()
        return [text] if text else []

    paragraphs = []
    for paragraph in split_output:
        if isinstance(paragraph, list):
            text = _join_text_parts(paragraph)
        else:
            text = str(paragraph or "").strip()
        if text:
            paragraphs.append(text)
    return paragraphs


@app.post("/paragraphs")
def split_paragraphs(req: ParagraphRequest):
    try:
        kwargs = {
            "stride": req.stride,
            "block_size": req.block_size,
            "weighting": req.weighting,
            "treat_newline_as_space": req.treat_newline_as_space,
            "do_paragraph_segmentation": True,
        }
        if req.threshold is not None:
            kwargs["threshold"] = float(req.threshold)
        if req.paragraph_threshold is not None:
            kwargs["paragraph_threshold"] = float(req.paragraph_threshold)
        with INFERENCE_GATE.enter():
            paragraph_sentences = sat.split(req.text, batch_size=INFERENCE_BATCH_SIZE, **kwargs)
        return {
            "paragraphs": _paragraphs_from_split_output(paragraph_sentences),
            "sentence_paragraphs": paragraph_sentences,
        }
    except Exception as exc:  # noqa: BLE001 - normalize engine failures for callers.
        logger.exception("Paragraph splitting failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _boundary_spans(text: str, indices: list[int]) -> list[dict]:
    spans = []
    offset = 0
    for index in indices:
        split_at = int(index) + 1
        while split_at < len(text) and text[split_at].isspace():
            split_at += 1
        if split_at > offset and text[offset:split_at].strip():
            spans.append({"start": offset, "end": split_at})
        offset = max(offset, split_at)
    if offset < len(text) and text[offset:].strip():
        spans.append({"start": offset, "end": len(text)})
    return spans


@app.post("/boundary-probabilities")
def boundary_probabilities(req: BoundaryRequest):
    try:
        with INFERENCE_GATE.enter():
            probs = sat.predict_proba(
                req.text, stride=req.stride, block_size=req.block_size,
                weighting=req.weighting, batch_size=INFERENCE_BATCH_SIZE,
            )
        if isinstance(probs, tuple):
            probs = probs[0]
        probs = np.asarray(probs, dtype=float).reshape(-1)
        threshold = DEFAULT_THRESHOLD if req.threshold is None else float(req.threshold)
        indices = [int(i) for i, value in enumerate(probs) if float(value) > threshold]
        boundaries = [
            {"index": index, "probability": float(probs[index])}
            for index in indices
        ]
        return {
            "threshold": threshold,
            "boundary_index_convention": "index is the character before the next sentence; split occurs after index plus following whitespace",
            "probabilities": [float(value) for value in probs],
            "boundaries": boundaries,
            "sentence_spans": _boundary_spans(req.text, indices),
        }
    except Exception as exc:  # noqa: BLE001 - normalize engine failures for callers.
        logger.exception("Boundary prediction failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
