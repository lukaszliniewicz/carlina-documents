"""Small, standalone PDF render child. Its stdout is a JSON-lines control channel."""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import queue
import signal
import site
import sys
import threading
import time
import uuid
from pathlib import Path


def _arm_parent_death(expected_parent_pid: int) -> None:
    """Make a Linux child exit even if native MuPDF work holds the GIL."""
    if sys.platform != "linux":
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno())
    if os.getppid() != expected_parent_pid:
        os._exit(0)


def _send(value: dict) -> None:
    try:
        sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    except BrokenPipeError, OSError:
        os._exit(0)


def _read_control(messages: queue.Queue) -> None:
    while True:
        line = sys.stdin.readline()
        if not line:
            # A dead parent must not leave a renderer holding a permit or CPU.
            os._exit(0)
        try:
            messages.put(json.loads(line))
        except ValueError, TypeError:
            messages.put({"op": "malformed"})


def _load_renderer():
    module_path = Path(__file__).with_name("pdf_rendering.py")
    spec = importlib.util.spec_from_file_location(
        "_carlina_pdf_rendering_child", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("RendererImportError")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.PdfImageRenderer


def main() -> int:
    # The parent holds admission.lock until this acknowledgement is visible.
    if len(sys.argv) not in (2, 3, 4):
        return 2
    if len(sys.argv) == 4:
        _arm_parent_death(int(sys.argv[3]))
    if len(sys.argv) >= 3:
        site.addsitedir(sys.argv[2])
    from filelock import FileLock

    slot_lock = FileLock(sys.argv[1])
    slot_lock.acquire()
    try:
        _send({"op": "ready"})
        messages: queue.Queue = queue.Queue()
        threading.Thread(target=_read_control, args=(messages,), daemon=True).start()
        try:
            import fitz

            fitz.TOOLS.mupdf_display_errors(False)
            renderer_type = _load_renderer()
            init = messages.get()
            if not isinstance(init, dict) or init.get("op") != "init":
                raise ValueError("InvalidControl")
            stage = Path(init["stage"])
            renderer = renderer_type(init["settings"], stage)
            document = fitz.open(init["source"])
            _send({"op": "initialized"})
            try:
                while True:
                    message = messages.get()
                    if not isinstance(message, dict):
                        raise TypeError("InvalidControl")
                    if message.get("op") == "stop":
                        return 0
                    if message.get("op") != "render":
                        raise ValueError("InvalidControl")
                    page_index = message["page_index"]
                    if type(page_index) is not int or page_index < 0:
                        raise ValueError("InvalidPageIndex")
                    crop_box = message.get("crop_box")
                    if crop_box is not None and not (
                        isinstance(crop_box, list)
                        and len(crop_box) == 4
                        and all(type(v) in (int, float) for v in crop_box)
                    ):
                        raise ValueError("InvalidCropBox")
                    renderer.metrics["crop_guard_expansions"] = []
                    started = time.monotonic()
                    png_path = Path(
                        renderer._render_page_to_png(document[page_index], crop_box)
                    )
                    render_seconds = time.monotonic() - started
                    if png_path.parent.resolve() != stage.resolve():
                        raise ValueError("InvalidOutput")
                    encoded_name = None
                    encoded_bytes = 0
                    encode_seconds = 0.0
                    if message.get("encode_metadata") is True:
                        started = time.monotonic()
                        data_url = renderer._image_file_to_data_url(str(png_path))
                        if data_url is None:
                            raise ValueError("EncodingFailed")
                        encoded_name = f"{uuid.uuid4().hex}.data-url"
                        encoded_path = stage / encoded_name
                        encoded_path.write_text(data_url, encoding="ascii")
                        encoded_bytes = encoded_path.stat().st_size
                        encode_seconds = time.monotonic() - started
                    _send(
                        {
                            "op": "result",
                            "page_index": page_index,
                            "png_name": png_path.name,
                            "data_url_name": encoded_name,
                            "crop_guard_expansions": renderer.metrics.get(
                                "crop_guard_expansions", []
                            ),
                            "render_seconds": render_seconds,
                            "encode_seconds": encode_seconds,
                            "png_bytes": png_path.stat().st_size,
                            "encoded_bytes": encoded_bytes,
                        }
                    )
            finally:
                document.close()
        except Exception as exc:  # noqa: BLE001 - isolate every child failure from IPC
            _send({"op": "error", "error_type": type(exc).__name__})
            return 1
    finally:
        slot_lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
