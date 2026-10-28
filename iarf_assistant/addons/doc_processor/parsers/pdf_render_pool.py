"""Bounded, cross-process PDF render supervision without application imports."""

from __future__ import annotations

import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

import filelock
import psutil
from filelock import FileLock, Timeout

_RECOVERABLE_RENDER_CODES = frozenset(
    {"WorkerExited", "WorkerTimeout", "WorkerControlError", "PoolWaitTimeout"}
)
_RENDER_PHASES = frozenset(
    {
        "pool_wait",
        "worker_ready",
        "worker_initialization",
        "page_render",
        "worker_output",
    }
)
_SAFE_CHILD_EXCEPTION_NAMES = frozenset(
    {
        "AssertionError",
        "AttributeError",
        "BufferError",
        "EOFError",
        "EmptyFileError",
        "FzErrorArgument",
        "FzErrorFormat",
        "FzErrorGeneric",
        "IndexError",
        "ImportError",
        "KeyError",
        "MemoryError",
        "ModuleNotFoundError",
        "NotImplementedError",
        "OSError",
        "OverflowError",
        "PermissionError",
        "RuntimeError",
        "TimeoutError",
        "TypeError",
        "UnicodeDecodeError",
        "ValueError",
    }
)
_SAFE_EXCEPTION_LINE = re.compile(
    rb"^(?:[A-Za-z_][A-Za-z0-9_]*\.)*([A-Za-z_][A-Za-z0-9_]*):[ \t]"
)
_MAX_STDERR_CAPTURE_BYTES = 8192
_STDERR_READ_BYTES = 4096


class RenderError(Exception):
    """A renderer failed; messages intentionally omit document content and paths."""

    def __init__(
        self,
        code: str,
        *,
        returncode: int | None = None,
        phase: str | None = None,
        recoverable: bool | None = None,
        stderr_bytes: int = 0,
        stderr_exception: str | None = None,
        attempts: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.returncode = returncode if type(returncode) is int else None
        self.phase = phase if phase in _RENDER_PHASES else None
        self.recoverable = (
            code in _RECOVERABLE_RENDER_CODES
            if recoverable is None
            else bool(recoverable and code in _RECOVERABLE_RENDER_CODES)
        )
        self.stderr_bytes = max(0, stderr_bytes) if type(stderr_bytes) is int else 0
        self.stderr_exception = (
            stderr_exception
            if stderr_exception in _SAFE_CHILD_EXCEPTION_NAMES
            else None
        )
        self.attempts = attempts if type(attempts) is int and attempts > 0 else None

    @property
    def diagnostic(self) -> dict:
        """Return bounded, content-free worker diagnostics."""
        return {
            "code": self.code,
            "returncode": self.returncode,
            "phase": self.phase,
            "recoverable": self.recoverable,
            "stderr_bytes": self.stderr_bytes,
            "stderr_exception": self.stderr_exception,
            "attempts": self.attempts,
        }


class RenderCancelled(BaseException):
    """The caller requested a pause or stop."""


@dataclass(frozen=True)
class RenderConfig:
    workers: int
    window: int
    timeout_seconds: float

    def __post_init__(self) -> None:
        if type(self.workers) is not int or not 1 <= self.workers <= 32:
            raise ValueError("InvalidRenderWorkerCount")
        if type(self.window) is not int or not 1 <= self.window <= 128:
            raise ValueError("InvalidRenderWindow")
        if (
            type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 3600
        ):
            raise ValueError("InvalidRenderTimeout")

    @classmethod
    def from_env(cls) -> RenderConfig:
        def count(name: str, default: int) -> int:
            value = os.environ.get(name)
            if value is None:
                return default
            if not value.isdecimal():
                raise ValueError("InvalidRenderConfig")
            return int(value)

        value = os.environ.get("CARLINA_PDF_RENDER_TIMEOUT_SECONDS")
        try:
            timeout = 120.0 if value is None else float(value)
        except ValueError as exc:
            raise ValueError("InvalidRenderConfig") from exc
        return cls(
            workers=count(
                "CARLINA_PDF_RENDER_WORKERS", min(4, os.process_cpu_count() or 1)
            ),
            window=count("CARLINA_PDF_RENDER_WINDOW", 32),
            timeout_seconds=timeout,
        )


def _check_cancel(callback) -> None:
    if callback is None:
        return
    action = callback()
    if action:
        raise RenderCancelled(str(action) if isinstance(action, str) else "stop")


class _BoundedStderrCapture:
    """Drain all child stderr while retaining only a small, private tail."""

    def __init__(self, limit: int = _MAX_STDERR_CAPTURE_BYTES) -> None:
        self._limit = max(1, limit)
        self._tail = bytearray()
        self._byte_count = 0
        self._peak_retained_bytes = 0
        self._lock = threading.Lock()

    def drain(self, stream) -> None:
        try:
            fd = stream.fileno()
            while True:
                try:
                    chunk = os.read(fd, _STDERR_READ_BYTES)
                except InterruptedError:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                with self._lock:
                    self._byte_count += len(chunk)
                    if len(chunk) >= self._limit:
                        self._tail[:] = chunk[-self._limit :]
                    else:
                        self._tail.extend(chunk)
                        overflow = len(self._tail) - self._limit
                        if overflow > 0:
                            del self._tail[:overflow]
                    self._peak_retained_bytes = max(
                        self._peak_retained_bytes, len(self._tail)
                    )
        except (OSError, ValueError):
            return

    def take_summary(self) -> tuple[int, str | None]:
        with self._lock:
            byte_count = self._byte_count
            tail = bytes(self._tail)
            self._tail.clear()
        exception_name = None
        for line in reversed(tail.splitlines()):
            match = _SAFE_EXCEPTION_LINE.match(line)
            if match is None:
                continue
            candidate = match.group(1).decode("ascii")
            if candidate in _SAFE_CHILD_EXCEPTION_NAMES:
                exception_name = candidate
                break
        return byte_count, exception_name

    def clear(self) -> None:
        with self._lock:
            self._tail.clear()

    @property
    def retained_bytes(self) -> int:
        with self._lock:
            return len(self._tail)

    @property
    def peak_retained_bytes(self) -> int:
        with self._lock:
            return self._peak_retained_bytes


def _stop_process(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        with suppress(ProcessLookupError):
            proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                proc.kill()
            proc.wait(timeout=1)
    else:
        proc.wait(timeout=1)


def _send(
    proc: subprocess.Popen,
    message: dict,
    *,
    phase: str,
    worker: dict | None = None,
) -> None:
    try:
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode()
        assert proc.stdin is not None
        proc.stdin.write(data)
        proc.stdin.flush()
    except (BrokenPipeError, OSError, ValueError, TypeError) as exc:
        error = RenderError(
            "WorkerControlError", returncode=proc.poll(), phase=phase
        )
        if worker is not None:
            error._worker_ref = worker
        raise error from exc


def _start_reader(proc: subprocess.Popen) -> dict:
    messages: queue.Queue[bytes] = queue.Queue(maxsize=2)
    stop_event = threading.Event()
    stderr_capture = _BoundedStderrCapture()

    def read_stdout() -> None:
        assert proc.stdout is not None
        while not stop_event.is_set():
            try:
                line = proc.stdout.readline(1024 * 1024 + 1)
            except OSError, ValueError:
                line = b""
            while not stop_event.is_set():
                try:
                    messages.put(line, timeout=0.1)
                    break
                except queue.Full:
                    continue
            if not line:
                return

    reader = threading.Thread(target=read_stdout, daemon=True)
    stderr_reader = threading.Thread(
        target=stderr_capture.drain, args=(proc.stderr,), daemon=True
    )
    worker = {
        "proc": proc,
        "messages": messages,
        "stop_event": stop_event,
        "reader": reader,
        "stderr_capture": stderr_capture,
        "stderr_reader": stderr_reader,
        "pending": None,
        "deadline": None,
    }
    try:
        reader.start()
        stderr_reader.start()
    except BaseException:
        stop_event.set()
        with suppress(BaseException):
            _stop_process(proc)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                with suppress(OSError, ValueError):
                    stream.close()
        if reader.ident is not None:
            reader.join(timeout=1)
        if stderr_reader.ident is not None:
            stderr_reader.join(timeout=1)
        stderr_capture.clear()
        raise
    return worker


def _worker_error(worker: dict, code: str, phase: str) -> RenderError:
    proc = worker["proc"]
    error = RenderError(code, returncode=proc.poll(), phase=phase)
    error._worker_ref = worker
    return error


def _attach_worker_diagnostics(error: RenderError, worker: dict) -> None:
    proc = worker["proc"]
    error.returncode = proc.returncode if type(proc.returncode) is int else error.returncode
    error.phase = error.phase or "worker_ready"
    if error.attempts is None:
        attempts = worker.get("attempts")
        error.attempts = attempts if type(attempts) is int and attempts > 0 else None
    error.stderr_bytes, error.stderr_exception = worker[
        "stderr_capture"
    ].take_summary()
    error.__dict__.pop("_worker_ref", None)


def _discard_worker_diagnostics(worker: dict) -> None:
    worker["stderr_capture"].clear()


def _read_line(worker: dict, deadline: float, cancel, *, phase: str):
    while True:
        _check_cancel(cancel)
        if time.monotonic() >= deadline:
            raise _worker_error(worker, "WorkerTimeout", phase)
        try:
            raw = worker["messages"].get(timeout=min(0.1, deadline - time.monotonic()))
        except queue.Empty:
            continue
        if not raw:
            raise _worker_error(worker, "WorkerExited", phase)
        if len(raw) > 1024 * 1024 or not raw.endswith(b"\n"):
            raise _worker_error(worker, "OversizeWorkerResponse", phase)
        try:
            value = json.loads(raw)
        except (UnicodeError, ValueError) as exc:
            raise _worker_error(
                worker, "MalformedWorkerResponse", phase
            ) from exc
        if not isinstance(value, dict):
            raise _worker_error(worker, "MalformedWorkerResponse", phase)
        if value.get("op") == "error":
            kind = value.get("error_type", "WorkerError")
            error = RenderError(
                kind if isinstance(kind, str) and kind.isidentifier() else "WorkerError",
                returncode=worker["proc"].poll(),
                phase=phase,
                recoverable=False,
            )
            error._worker_ref = worker
            raise error
        return value


def _stop(worker: dict) -> None:
    proc = worker["proc"]
    worker["stop_event"].set()
    try:
        _stop_process(proc)
    finally:
        worker["reader"].join(timeout=1)
        worker["stderr_reader"].join(timeout=1)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                with suppress(OSError, ValueError):
                    stream.close()
        worker["reader"].join(timeout=1)
        worker["stderr_reader"].join(timeout=1)


def _stop_after_failure(error: BaseException, worker: dict) -> None:
    try:
        _stop(worker)
    except Exception:  # noqa: BLE001, S110 - keep cleanup from masking the active error
        # Cleanup must not replace the renderer failure or cancellation.
        pass
    if isinstance(error, RenderError):
        _attach_worker_diagnostics(error, worker)
    else:
        _discard_worker_diagnostics(worker)


def _stop_unstarted_process(proc: subprocess.Popen) -> None:
    try:
        _stop_process(proc)
    except Exception:  # noqa: BLE001, S110 - preserve the original launch failure
        pass
    finally:
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                with suppress(OSError, ValueError):
                    stream.close()


def _slot_free(path: Path) -> bool:
    lock = FileLock(str(path), timeout=0)
    try:
        lock.acquire(timeout=0)
    except Timeout:
        return False
    else:
        lock.release()
        return True


def _coordinate_cap(directory: Path, workers: int) -> None:
    config_path = directory / "config.json"
    if config_path.exists():
        try:
            old = json.loads(config_path.read_text(encoding="ascii"))["workers"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RenderError("InvalidPoolConfig") from exc
        if type(old) is not int or not 1 <= old <= 32:
            raise RenderError("InvalidPoolConfig")
        if old == workers:
            return
        if any(not _slot_free(directory / f"slot-{slot}.lock") for slot in range(old)):
            raise RenderError("ConflictingPoolLimit")
    temporary = directory / f"config-{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(json.dumps({"workers": workers}), encoding="ascii")
        temporary.replace(config_path)
    finally:
        temporary.unlink(missing_ok=True)


def _acquire_admission(lock: FileLock, deadline: float, cancel) -> None:
    while True:
        _check_cancel(cancel)
        if time.monotonic() >= deadline:
            raise RenderError("PoolWaitTimeout", phase="pool_wait")
        try:
            lock.acquire(timeout=0)
            return
        except Timeout:
            if time.monotonic() >= deadline:
                raise RenderError("PoolWaitTimeout", phase="pool_wait")
            time.sleep(0.1)


def _launch_one(
    directory: Path,
    config: RenderConfig,
    source: Path,
    stage: Path,
    settings: dict,
    cancel,
    deadline: float,
    metrics: dict,
    *,
    wait_if_busy: bool,
    worker_script: Path,
):
    admission = FileLock(str(directory / "admission.lock"), timeout=0)
    started_wait = time.monotonic()
    startup = None
    attempt_count = 0
    while True:
        _acquire_admission(admission, deadline, cancel)
        proc = None
        worker = None
        try:
            _coordinate_cap(directory, config.workers)
            slot = next(
                (
                    n
                    for n in range(config.workers)
                    if _slot_free(directory / f"slot-{n}.lock")
                ),
                None,
            )
            if slot is not None:
                metrics["pool_wait_seconds"] += time.monotonic() - started_wait
                startup = time.monotonic()
                slot_path = directory / f"slot-{slot}.lock"
                while True:
                    proc = None
                    worker = None
                    attempt_count += 1
                    try:
                        proc = subprocess.Popen(
                            [
                                sys.executable,
                                str(worker_script),
                                str(slot_path),
                                str(Path(filelock.__file__).resolve().parent.parent),
                                str(os.getpid()),
                            ],
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            bufsize=0,
                        )
                        worker = _start_reader(proc)
                        worker["attempts"] = attempt_count
                        ready = _read_line(
                            worker, deadline, cancel, phase="worker_ready"
                        )
                        if ready.get("op") != "ready":
                            raise _worker_error(
                                worker, "InvalidWorkerReady", "worker_ready"
                            )
                    except BaseException as exc:
                        if worker is not None:
                            _stop_after_failure(exc, worker)
                        elif proc is not None:
                            _stop_unstarted_process(proc)
                        if isinstance(exc, RenderError):
                            exc.attempts = attempt_count
                            if (
                                exc.code == "WorkerExited"
                                and exc.phase == "worker_ready"
                                and attempt_count < 3
                                and time.monotonic() < deadline
                                and _slot_free(slot_path)
                            ):
                                _check_cancel(cancel)
                                continue
                        raise
                    break
                break
            if not wait_if_busy:
                metrics["pool_wait_seconds"] += time.monotonic() - started_wait
                return None
        finally:
            admission.release()
        if time.monotonic() >= deadline:
            raise RenderError("PoolWaitTimeout", phase="pool_wait")
        _check_cancel(cancel)
        time.sleep(0.1)
    try:
        _send(
            proc,
            {
                "op": "init",
                "source": str(source),
                "stage": str(stage),
                "settings": settings,
            },
            phase="worker_initialization",
            worker=worker,
        )
        initialized = _read_line(
            worker, deadline, cancel, phase="worker_initialization"
        )
        if initialized.get("op") != "initialized":
            raise _worker_error(
                worker, "InvalidWorkerInitialization", "worker_initialization"
            )
        metrics["worker_startup_seconds"] += time.monotonic() - startup
        return worker
    except BaseException as exc:
        if worker is not None:
            _stop_after_failure(exc, worker)
        raise


def _sample_rss(workers, metrics: dict) -> None:
    total = 0
    for worker in workers:
        try:
            total += psutil.Process(worker["proc"].pid).memory_info().rss
        except psutil.Error, OSError:
            pass
    metrics["peak_pool_rss_bytes"] = max(metrics["peak_pool_rss_bytes"], total)


def _validate_request(request):
    if not isinstance(request, dict) or type(request.get("page_index")) is not int:
        raise RenderError("InvalidRenderRequest")
    page_index = request["page_index"]
    if page_index < 0:
        raise RenderError("InvalidRenderRequest")
    crop = request.get("crop_box")
    if crop is not None and not (
        isinstance(crop, list)
        and len(crop) == 4
        and all(type(v) in (int, float) and math.isfinite(v) for v in crop)
    ):
        raise RenderError("InvalidRenderRequest")
    return {"page_index": page_index, "crop_box": crop}


def _receive_result(workers, expected_index: int, results: dict, cancel, metrics):
    while expected_index not in results:
        _check_cancel(cancel)
        _sample_rss(workers, metrics)
        pending = [worker for worker in workers if worker["pending"] is not None]
        if not pending:
            raise RenderError("MissingWorkerResult")
        now = time.monotonic()
        timed_out = next(
            (worker for worker in pending if now >= worker["deadline"]), None
        )
        if timed_out is not None:
            raise _worker_error(timed_out, "WorkerTimeout", "page_render")
        found = False
        for worker in pending:
            if worker["messages"].empty():
                continue
            value = _read_line(
                worker, worker["deadline"], cancel, phase="page_render"
            )
            if (
                value.get("op") != "result"
                or value.get("page_index") != worker["pending"][1]["page_index"]
            ):
                raise _worker_error(worker, "InvalidWorkerResult", "page_render")
            results[worker["pending"][0]] = (value, worker)
            worker["pending"] = None
            worker["deadline"] = None
            found = True
        if not found:
            time.sleep(min(0.05, *(w["deadline"] - now for w in pending)))


@contextmanager
def prepared_pages(
    source_path,
    output_folder,
    requests,
    *,
    settings,
    coordination_dir,
    encode_metadata=False,
    should_cancel=None,
    on_metrics=None,
    config=None,
    _worker_script=None,
):
    """Yield ordered file receipts, keeping at most one outstanding page per child."""
    config = config or RenderConfig.from_env()
    if not isinstance(config, RenderConfig):
        raise TypeError("InvalidRenderConfig")
    # Verify JSON compatibility before a child is spawned.
    try:
        json.dumps(settings)
    except (TypeError, ValueError) as exc:
        raise RenderError("InvalidRenderSettings") from exc
    output = Path(output_folder)
    output.mkdir(parents=True, exist_ok=True)
    coordinate = Path(coordination_dir)
    coordinate.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".pdf-render-", dir=output))
    worker_script = Path(
        _worker_script or Path(__file__).with_name("pdf_render_worker.py")
    ).resolve()

    def iterate():
        request_iter = iter(requests)
        try:
            while True:
                _check_cancel(should_cancel)
                raw_chunk = list(islice(request_iter, config.window))
                if not raw_chunk:
                    break
                metrics = {
                    "pool_wait_seconds": 0.0,
                    "worker_startup_seconds": 0.0,
                    "preparation_wall_seconds": 0.0,
                    "worker_render_seconds_sum": 0.0,
                    "worker_encode_seconds_sum": 0.0,
                    "pages": 0,
                    "png_bytes": 0,
                    "encoded_bytes": 0,
                    "peak_pool_rss_bytes": 0,
                    "incomplete": 0,
                }
                started = time.monotonic()
                workers = []
                results = {}
                prepared = []
                next_dispatch = 0
                failure = None
                try:
                    chunk = [_validate_request(item) for item in raw_chunk]
                    deadline = time.monotonic() + config.timeout_seconds
                    for _ in range(min(config.workers, len(chunk))):
                        try:
                            worker = _launch_one(
                                coordinate,
                                config,
                                Path(source_path),
                                stage,
                                settings,
                                should_cancel,
                                deadline,
                                metrics,
                                wait_if_busy=not workers,
                                worker_script=worker_script,
                            )
                            if worker is None:
                                break
                            workers.append(worker)
                        except RenderError as exc:
                            if str(exc) == "PoolWaitTimeout" and workers:
                                break
                            raise
                    while next_dispatch < min(len(workers), len(chunk)):
                        worker = workers[next_dispatch]
                        worker["pending"] = (next_dispatch, chunk[next_dispatch])
                        worker["deadline"] = time.monotonic() + config.timeout_seconds
                        _send(
                            worker["proc"],
                            {
                                "op": "render",
                                **chunk[next_dispatch],
                                "encode_metadata": bool(encode_metadata),
                            },
                            phase="page_render",
                            worker=worker,
                        )
                        next_dispatch += 1
                    for index in range(len(chunk)):
                        _receive_result(workers, index, results, should_cancel, metrics)
                        value, worker = results.pop(index)
                        png_name = value.get("png_name")
                        sidecar_name = value.get("data_url_name")
                        if (
                            not isinstance(png_name, str)
                            or Path(png_name).name != png_name
                        ):
                            raise RenderError("InvalidWorkerOutput")
                        if sidecar_name is not None and (
                            not isinstance(sidecar_name, str)
                            or Path(sidecar_name).name != sidecar_name
                        ):
                            raise RenderError("InvalidWorkerOutput")
                        png = stage / png_name
                        if not png.is_file():
                            raise RenderError("MissingWorkerOutput")
                        sidecar = None
                        if sidecar_name is not None:
                            sidecar = stage / sidecar_name
                            if not sidecar.is_file():
                                raise RenderError("MissingWorkerOutput")
                        receipt = {
                            "page_index": value["page_index"],
                            "crop_guard_expansions": value.get(
                                "crop_guard_expansions", []
                            ),
                            "render_seconds": float(value["render_seconds"]),
                            "encode_seconds": float(value["encode_seconds"]),
                            "png_bytes": int(value["png_bytes"]),
                            "encoded_bytes": int(value["encoded_bytes"]),
                        }
                        prepared.append((receipt, png, sidecar))
                        metrics["pages"] += 1
                        metrics["worker_render_seconds_sum"] += receipt[
                            "render_seconds"
                        ]
                        metrics["worker_encode_seconds_sum"] += receipt[
                            "encode_seconds"
                        ]
                        metrics["png_bytes"] += receipt["png_bytes"]
                        metrics["encoded_bytes"] += receipt["encoded_bytes"]
                        if next_dispatch < len(chunk):
                            worker["pending"] = (next_dispatch, chunk[next_dispatch])
                            worker["deadline"] = (
                                time.monotonic() + config.timeout_seconds
                            )
                            _send(
                                worker["proc"],
                                {
                                    "op": "render",
                                    **chunk[next_dispatch],
                                    "encode_metadata": bool(encode_metadata),
                                },
                                phase="page_render",
                                worker=worker,
                            )
                            next_dispatch += 1
                except BaseException as exc:
                    failure = exc
                    raise
                finally:
                    # Work completed after cancellation but without a received receipt
                    # cannot be counted accurately; mark that bounded sample incomplete.
                    metrics["incomplete"] = int(metrics["pages"] < len(raw_chunk))
                    _sample_rss(workers, metrics)
                    for worker in workers:
                        if getattr(failure, "_worker_ref", None) is worker:
                            _stop_after_failure(failure, worker)
                        elif failure is None:
                            try:
                                _stop(worker)
                            finally:
                                _discard_worker_diagnostics(worker)
                        else:
                            try:
                                _stop(worker)
                            except Exception:  # noqa: BLE001, S110 - preserve active failure
                                pass
                            _discard_worker_diagnostics(worker)
                    metrics["preparation_wall_seconds"] = time.monotonic() - started
                    if on_metrics is not None:
                        on_metrics(metrics.copy())
                # No worker holds a slot while the caller handles page receipts.
                for receipt, png, sidecar in prepared:
                    _check_cancel(should_cancel)
                    destination = output / f"docproc_{uuid.uuid4().hex}.png"
                    try:
                        png.replace(destination)
                    except OSError:
                        raise RenderError("OutputMoveError") from None
                    receipt["png_path"] = str(destination)
                    if sidecar is not None:
                        sidecar_destination = (
                            output / f"docproc_{uuid.uuid4().hex}.data-url"
                        )
                        try:
                            sidecar.replace(sidecar_destination)
                        except OSError:
                            destination.unlink(missing_ok=True)
                            raise RenderError("OutputMoveError") from None
                        receipt["data_url_path"] = str(sidecar_destination)
                    yield receipt
        finally:
            shutil.rmtree(stage)

    iterator = iterate()
    try:
        yield iterator
    finally:
        iterator.close()
        # If the iterator was never started, its finally block did not run.
        if stage.exists():
            shutil.rmtree(stage)
