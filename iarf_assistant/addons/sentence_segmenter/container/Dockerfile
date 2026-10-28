FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WTPSPLIT_MODEL_ID=sat-12l-sm \
    WTPSPLIT_ORT_PROVIDERS=CUDAExecutionProvider,CPUExecutionProvider \
    WTPSPLIT_REQUIRE_CUDA=0 \
    LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib:/usr/local/lib/python3.12/site-packages/nvidia/cuda_runtime/lib:/usr/local/lib/python3.12/site-packages/nvidia/cuda_nvrtc/lib:/usr/local/lib/python3.12/site-packages/nvidia/curand/lib:/usr/local/lib/python3.12/site-packages/nvidia/cufft/lib:/usr/local/nvidia/lib:/usr/local/nvidia/lib64

# Install an x86-64 Linux dependency set with verified artifacts. wtpsplit-lite
# is intentionally locked separately and installed without dependencies so it
# cannot replace the CUDA-capable ONNX Runtime with the CPU distribution.
COPY requirements.lock wtpsplit-requirements.lock ./
RUN pip install --require-hashes -r requirements.lock \
    && pip install --no-deps --require-hashes -r wtpsplit-requirements.lock

# Pre-download the ONNX model into the Docker image so service startup is fast.
# We install wtpsplit-lite with --no-deps above to avoid pulling CPU-only onnxruntime.
RUN python - <<'PY'
import os
from pathlib import Path
import onnxruntime as ort
from wtpsplit_lite import SaT

model_id = os.getenv("WTPSPLIT_MODEL_ID", "sat-12l-sm")
if hasattr(ort, "preload_dlls"):
    try:
        ort.preload_dlls(cuda=True, cudnn=True, directory="")
        print("onnxruntime.preload_dlls(cuda=True, cudnn=True) completed.")
    except Exception as exc:
        print(f"onnxruntime.preload_dlls warning: {exc}")
requested = [
    item.strip() for item in os.getenv("WTPSPLIT_ORT_PROVIDERS", "").split(",") if item.strip()
]
available = ort.get_available_providers()
cuda_visible = any(
    device.exists()
    for device in (
        Path("/dev/nvidiactl"),
        Path("/dev/nvidia0"),
        Path("/dev/dxg"),
    )
)
providers = [
    provider
    for provider in requested
    if provider in available
    and (provider != "CUDAExecutionProvider" or cuda_visible)
]
if not providers and "CPUExecutionProvider" in available:
    providers = ["CPUExecutionProvider"]

print(f"Downloading {model_id} with providers={providers} (available={available})...")
SaT(model_id, ort_providers=providers)
print("Model cache warmup complete.")
PY

# Copy the server script and the shared batch contract.
COPY server.py service_runtime.py batch_protocol.py ./

EXPOSE 8000

# Start the FastAPI server
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--limit-concurrency", "16"]
