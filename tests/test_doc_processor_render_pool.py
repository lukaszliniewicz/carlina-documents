"""Process-level receipts, permits, cancellation, and output ownership."""

import json
import os
import subprocess
import sys
import time
from multiprocessing import Process, Queue
from pathlib import Path

import filelock
import fitz
import psutil
import pytest
from filelock import FileLock

from iarf_assistant.addons.doc_processor.parsers import pdf_render_pool
from iarf_assistant.addons.doc_processor.parsers.pdf_render_pool import (
    RenderCancelled,
    RenderConfig,
    RenderError,
    _slot_free,
    prepared_pages,
)

FAKE_WORKER = """
import json, sys, time, uuid
from pathlib import Path
from filelock import FileLock
lock = FileLock(sys.argv[1]); lock.acquire()
def send(value):
    print(json.dumps(value), flush=True)
send({'op': 'ready'})
init = json.loads(sys.stdin.readline())
settings = init['settings']; stage = Path(init['stage'])
time.sleep(settings.get('startup_delay', 0))
send({'op': 'initialized'})
for line in sys.stdin:
    msg = json.loads(line)
    if msg['op'] == 'stop': break
    if settings.get('mode') == 'error':
        send({'op': 'error', 'error_type': settings.get('error_type', 'ValueError')}); break
    markers = settings.get('markers')
    if markers:
        marker = Path(markers) / uuid.uuid4().hex
        marker.touch()
        gate = settings.get('gate')
        if gate == 'first':
            if len(list(Path(markers).iterdir())) == 1:
                while not (Path(markers).parent / 'release').exists(): time.sleep(.01)
        if gate == 'both':
            while len(list(Path(markers).iterdir())) < 2: time.sleep(.01)
    time.sleep(settings.get('render_delay', 0))
    path = stage / (uuid.uuid4().hex + '.png')
    path.write_bytes(b'fake-png')
    send({'op': 'result', 'page_index': msg['page_index'],
          'png_name': path.name, 'data_url_name': None,
          'crop_guard_expansions': [], 'render_seconds': 0.01,
          'encode_seconds': 0.0, 'png_bytes': 8, 'encoded_bytes': 0})
lock.release()
"""


def _fake_script(tmp_path):
    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER)
    return script


def _tracked_processes(monkeypatch):
    spawned = []
    original_popen = subprocess.Popen

    def tracked_popen(*args, **kwargs):
        proc = original_popen(*args, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(pdf_render_pool.subprocess, "Popen", tracked_popen)
    return spawned


def _wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for test barrier")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CARLINA_PDF_RENDER_WORKERS", "0"),
        ("CARLINA_PDF_RENDER_WORKERS", "33"),
        ("CARLINA_PDF_RENDER_WORKERS", "1.0"),
        ("CARLINA_PDF_RENDER_WORKERS", "true"),
        ("CARLINA_PDF_RENDER_WINDOW", "0"),
        ("CARLINA_PDF_RENDER_WINDOW", "129"),
        ("CARLINA_PDF_RENDER_TIMEOUT_SECONDS", "0"),
        ("CARLINA_PDF_RENDER_TIMEOUT_SECONDS", "inf"),
        ("CARLINA_PDF_RENDER_TIMEOUT_SECONDS", "3601"),
    ],
)
def test_config_rejects_invalid_env(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        RenderConfig.from_env()


def test_config_rejects_bool_and_accepts_bounds():
    with pytest.raises(ValueError):
        RenderConfig(True, 1, 1)
    with pytest.raises(ValueError):
        RenderConfig(1, 1.5, 1)
    assert RenderConfig(32, 128, 3600).workers == 32


def test_render_error_preserves_code_and_uses_recoverable_allowlist():
    recoverable_codes = {
        "WorkerExited",
        "WorkerTimeout",
        "WorkerControlError",
        "PoolWaitTimeout",
    }
    for code in recoverable_codes:
        error = RenderError(code)
        assert str(error) == code
        assert error.code == code
        assert error.recoverable is True

    for code in ("MalformedWorkerResponse", "InvalidWorkerResult", "ValueError"):
        assert RenderError(code).recoverable is False
    assert RenderError("WorkerTimeout", recoverable=False).recoverable is False


def test_child_error_named_like_recoverable_code_stays_nonrecoverable(tmp_path, monkeypatch):
    spawned = _tracked_processes(monkeypatch)
    with pytest.raises(RenderError) as caught, prepared_pages(
        tmp_path / "unused.pdf",
        tmp_path / "out",
        [{"page_index": 0, "crop_box": None}],
        settings={"mode": "error", "error_type": "WorkerTimeout"},
        coordination_dir=tmp_path / "coord",
        config=RenderConfig(1, 1, 3),
        _worker_script=_fake_script(tmp_path),
    ) as pages:
        list(pages)

    assert caught.value.code == str(caught.value) == "WorkerTimeout"
    assert caught.value.phase == "page_render"
    assert caught.value.recoverable is False
    assert caught.value.attempts == 1
    assert len(spawned) == 1
    assert spawned[0].returncode is not None


def test_lookahead_order_and_early_close_ownership(tmp_path):
    script = _fake_script(tmp_path)
    seen = []
    metrics = []

    def requests():
        for page in range(9):
            seen.append(page)
            yield {"page_index": page, "crop_box": None}

    with prepared_pages(
        tmp_path / "unused.pdf",
        tmp_path / "out",
        requests(),
        settings={},
        coordination_dir=tmp_path / "coord",
        on_metrics=metrics.append,
        config=RenderConfig(2, 4, 3),
        _worker_script=script,
    ) as pages:
        first = next(pages)
        assert first["page_index"] == 0
        assert seen == [0, 1, 2, 3]
        assert _slot_free(tmp_path / "coord" / "slot-0.lock")
        assert _slot_free(tmp_path / "coord" / "slot-1.lock")
    assert Path(first["png_path"]).read_bytes() == b"fake-png"
    assert not list((tmp_path / "out").glob(".pdf-render-*"))
    assert list((tmp_path / "out").glob("*.png")) == [Path(first["png_path"])]
    assert len(metrics) == 1
    assert metrics[0]["pages"] == 4 and metrics[0]["incomplete"] == 0
    assert all(isinstance(value, (int, float)) for value in metrics[0].values())


def test_cancel_after_first_receipt_keeps_only_yielded_file(tmp_path):
    script = _fake_script(tmp_path)
    cancelled = False
    with prepared_pages(
        tmp_path / "unused.pdf",
        tmp_path / "out",
        [{"page_index": page, "crop_box": None} for page in range(3)],
        settings={},
        coordination_dir=tmp_path / "coord",
        should_cancel=lambda: "pause" if cancelled else False,
        config=RenderConfig(2, 3, 3),
        _worker_script=script,
    ) as pages:
        first = next(pages)
        cancelled = True
        with pytest.raises(RenderCancelled, match="pause"):
            next(pages)
    assert Path(first["png_path"]).read_bytes() == b"fake-png"
    assert list((tmp_path / "out").glob("*.png")) == [Path(first["png_path"])]
    assert not list((tmp_path / "out").glob(".pdf-render-*"))


def test_worker_error_timeout_and_cancel_release_slot(tmp_path, monkeypatch):
    script = _fake_script(tmp_path)
    coordinate = tmp_path / "coord"
    request = [{"page_index": 0, "crop_box": None}]
    spawned = _tracked_processes(monkeypatch)
    for settings, timeout, expected in [
        ({"mode": "error"}, 2, RenderError),
        ({"render_delay": 1}, 0.15, RenderError),
        ({"startup_delay": 1}, 0.15, RenderError),
    ]:
        before = len(spawned)
        with (
            pytest.raises(expected),
            prepared_pages(
                tmp_path / "unused.pdf",
                tmp_path / "out",
                request,
                settings=settings,
                coordination_dir=coordinate,
                config=RenderConfig(1, 1, timeout),
                _worker_script=script,
            ) as pages,
        ):
            list(pages)
        assert len(spawned) == before + 1
        assert _slot_free(coordinate / "slot-0.lock")
        assert all(proc.returncode is not None for proc in spawned)

    markers = tmp_path / "markers"
    markers.mkdir()
    with (
        pytest.raises(RenderCancelled, match="pause"),
        prepared_pages(
            tmp_path / "unused.pdf",
            tmp_path / "out",
            request,
            settings={"markers": str(markers), "gate": "first"},
            coordination_dir=coordinate,
            should_cancel=lambda: "pause" if any(markers.iterdir()) else False,
            config=RenderConfig(1, 1, 3),
            _worker_script=script,
        ) as pages,
    ):
        list(pages)
    assert _slot_free(coordinate / "slot-0.lock")
    assert all(proc.returncode is not None for proc in spawned)


def test_pre_ready_worker_exit_retries_once_and_then_renders(tmp_path, monkeypatch):
    script = tmp_path / "transient_worker.py"
    script.write_text(
        """
from pathlib import Path
import sys
attempts_file = Path(sys.argv[1]).parent / 'attempt-count'
attempts = int(attempts_file.read_text()) if attempts_file.exists() else 0
attempts_file.write_text(str(attempts + 1))
if attempts == 0:
    sys.stderr.write('RuntimeError: private page text /private/book.pdf\\n')
    sys.stderr.flush()
    raise SystemExit(29)
"""
        + FAKE_WORKER
    )
    spawned = _tracked_processes(monkeypatch)
    coordinate = tmp_path / "coord"
    with prepared_pages(
        tmp_path / "unused.pdf",
        tmp_path / "out",
        [{"page_index": 0, "crop_box": None}],
        settings={},
        coordination_dir=coordinate,
        config=RenderConfig(1, 1, 3),
        _worker_script=script,
    ) as pages:
        assert next(pages)["page_index"] == 0

    assert (coordinate / "attempt-count").read_text() == "2"
    assert len(spawned) == 2
    assert all(proc.returncode is not None for proc in spawned)
    assert spawned[0].returncode == 29
    assert _slot_free(coordinate / "slot-0.lock")


def test_persistent_pre_ready_exit_exhausts_three_attempts_with_safe_diagnostics(
    tmp_path, monkeypatch
):
    script = tmp_path / "persistent_exit_worker.py"
    script.write_text(
        """
import sys
from pathlib import Path
attempts_file = Path(sys.argv[1]).parent / 'attempt-count'
attempts = int(attempts_file.read_text()) if attempts_file.exists() else 0
attempts_file.write_text(str(attempts + 1))
sys.stderr.write('ValueError: private page text /private/synthetic.pdf\\n')
sys.stderr.flush()
raise SystemExit(23)
"""
    )
    spawned = _tracked_processes(monkeypatch)
    coordinate = tmp_path / "coord"

    with pytest.raises(RenderError) as caught, prepared_pages(
        tmp_path / "unused.pdf",
        tmp_path / "out",
        [{"page_index": 0, "crop_box": None}],
        settings={},
        coordination_dir=coordinate,
        config=RenderConfig(1, 1, 3),
        _worker_script=script,
    ) as pages:
        list(pages)

    error = caught.value
    assert error.code == str(error) == "WorkerExited"
    assert error.returncode == 23
    assert error.phase == "worker_ready"
    assert error.recoverable is True
    assert error.attempts == 3
    assert error.stderr_bytes == len(
        b"ValueError: private page text /private/synthetic.pdf\n"
    )
    assert error.stderr_exception == "ValueError"
    assert len(spawned) == 3
    assert all(proc.returncode == 23 for proc in spawned)
    assert (coordinate / "attempt-count").read_text() == "3"
    assert _slot_free(coordinate / "slot-0.lock")

    safe_values = (
        str(error),
        repr(error),
        repr(error.diagnostic),
        repr(vars(error)),
    )
    assert all(
        "/private/synthetic.pdf" not in value and "private page text" not in value
        for value in safe_values
    )


def test_malformed_ready_protocol_is_not_retried(tmp_path, monkeypatch):
    script = tmp_path / "malformed_ready_worker.py"
    script.write_text("import json\nprint(json.dumps({'op': 'almost-ready'}), flush=True)\n")
    spawned = _tracked_processes(monkeypatch)

    with pytest.raises(RenderError) as caught, prepared_pages(
        tmp_path / "unused.pdf",
        tmp_path / "out",
        [{"page_index": 0, "crop_box": None}],
        settings={},
        coordination_dir=tmp_path / "coord",
        config=RenderConfig(1, 1, 3),
        _worker_script=script,
    ) as pages:
        list(pages)

    assert caught.value.code == "InvalidWorkerReady"
    assert caught.value.phase == "worker_ready"
    assert caught.value.recoverable is False
    assert caught.value.attempts == 1
    assert len(spawned) == 1
    assert spawned[0].returncode is not None


def test_large_stderr_is_drained_bounded_and_never_exposed(tmp_path, monkeypatch):
    script = tmp_path / "large_stderr_worker.py"
    script.write_text(
        """
import os
payload = b'ValueError: PRIVATE_PAGE_TOKEN /private/synthetic.pdf\\n' * 16384
remaining = memoryview(payload)
while remaining:
    written = os.write(2, remaining)
    remaining = remaining[written:]
os._exit(31)
"""
    )
    captures = []
    original_start_reader = pdf_render_pool._start_reader

    def tracked_start_reader(proc):
        worker = original_start_reader(proc)
        captures.append(worker["stderr_capture"])
        return worker

    monkeypatch.setattr(pdf_render_pool, "_start_reader", tracked_start_reader)
    started = time.monotonic()
    with pytest.raises(RenderError) as caught, prepared_pages(
        tmp_path / "unused.pdf",
        tmp_path / "out",
        [{"page_index": 0, "crop_box": None}],
        settings={},
        coordination_dir=tmp_path / "coord",
        config=RenderConfig(1, 1, 4),
        _worker_script=script,
    ) as pages:
        list(pages)
    elapsed = time.monotonic() - started

    error = caught.value
    assert error.code == "WorkerExited"
    assert error.returncode == 31
    assert error.stderr_bytes == len(
        b"ValueError: PRIVATE_PAGE_TOKEN /private/synthetic.pdf\n"
    ) * 16384
    assert error.stderr_exception == "ValueError"
    assert error.attempts == 3
    assert len(captures) == 3
    assert all(
        capture.peak_retained_bytes <= pdf_render_pool._MAX_STDERR_CAPTURE_BYTES
        and capture.retained_bytes == 0
        for capture in captures
    )
    assert elapsed < 4

    safe_values = (str(error), repr(error), repr(error.diagnostic), repr(vars(error)))
    assert all(
        "PRIVATE_PAGE_TOKEN" not in value and "/private/synthetic.pdf" not in value
        for value in safe_values
    )


@pytest.mark.parametrize("cancel", [False, True])
def test_pre_ready_timeout_and_cancel_are_bounded_and_not_retried(
    tmp_path, monkeypatch, cancel
):
    marker = tmp_path / "worker-started"
    script = tmp_path / "sleeping_worker.py"
    script.write_text(
        "from pathlib import Path\n"
        "import time\n"
        f"Path({str(marker)!r}).touch()\n"
        "time.sleep(30)\n"
    )
    spawned = _tracked_processes(monkeypatch)
    coordinate = tmp_path / "coord"
    timeout = 3 if cancel else 0.2
    should_cancel = lambda: "pause" if cancel and marker.exists() else False
    started = time.monotonic()

    if cancel:
        with pytest.raises(RenderCancelled, match="pause"), prepared_pages(
            tmp_path / "unused.pdf",
            tmp_path / "out",
            [{"page_index": 0, "crop_box": None}],
            settings={},
            coordination_dir=coordinate,
            should_cancel=should_cancel,
            config=RenderConfig(1, 1, timeout),
            _worker_script=script,
        ) as pages:
            list(pages)
    else:
        with pytest.raises(RenderError) as caught, prepared_pages(
            tmp_path / "unused.pdf",
            tmp_path / "out",
            [{"page_index": 0, "crop_box": None}],
            settings={},
            coordination_dir=coordinate,
            config=RenderConfig(1, 1, timeout),
            _worker_script=script,
        ) as pages:
            list(pages)
        assert caught.value.code == "WorkerTimeout"
        assert caught.value.phase == "worker_ready"
        assert caught.value.attempts == 1

    assert time.monotonic() - started < 2
    assert marker.exists()
    assert len(spawned) == 1
    assert spawned[0].returncode is not None
    assert _slot_free(coordinate / "slot-0.lock")


def test_conflicting_cap_rejected_until_old_slot_releases(tmp_path):
    script = _fake_script(tmp_path)
    coordinate = tmp_path / "coord"
    coordinate.mkdir()
    (coordinate / "config.json").write_text('{"workers": 1}')
    held = FileLock(str(coordinate / "slot-0.lock"))
    held.acquire()
    try:
        with (
            pytest.raises(RenderError, match="ConflictingPoolLimit"),
            prepared_pages(
                tmp_path / "unused.pdf",
                tmp_path / "out",
                [{"page_index": 0, "crop_box": None}],
                settings={},
                coordination_dir=coordinate,
                config=RenderConfig(2, 1, 2),
                _worker_script=script,
            ) as pages,
        ):
            list(pages)
    finally:
        held.release()
    with prepared_pages(
        tmp_path / "unused.pdf",
        tmp_path / "out",
        [{"page_index": 0, "crop_box": None}],
        settings={},
        coordination_dir=coordinate,
        config=RenderConfig(2, 1, 2),
        _worker_script=script,
    ) as pages:
        assert len(list(pages)) == 1
    assert json.loads((coordinate / "config.json").read_text()) == {"workers": 2}


def _call_pool(script, root, coordinate, markers, gate, result_queue):
    try:
        with prepared_pages(
            root / "unused.pdf",
            root / f"out-{os.getpid()}",
            [{"page_index": 0, "crop_box": None}],
            settings={"markers": str(markers), "gate": gate},
            coordination_dir=coordinate,
            config=RenderConfig(1, 1, 5),
            _worker_script=script,
        ) as pages:
            result_queue.put(next(pages)["page_index"])
    except BaseException as exc:  # noqa: BLE001 - report child failure to test parent
        result_queue.put(type(exc).__name__)


def test_independent_callers_share_cap_and_separate_roots(tmp_path):
    script = _fake_script(tmp_path)
    markers = tmp_path / "same-markers"
    markers.mkdir()
    queue = Queue()
    a = Process(
        target=_call_pool,
        args=(script, tmp_path, tmp_path / "shared", markers, "first", queue),
    )
    b = Process(
        target=_call_pool,
        args=(script, tmp_path, tmp_path / "shared", markers, "first", queue),
    )
    a.start()
    try:
        _wait_until(lambda: len(list(markers.iterdir())) == 1)
        b.start()
        time.sleep(0.3)
        assert len(list(markers.iterdir())) == 1
        (tmp_path / "release").touch()
        a.join(5)
        b.join(5)
        assert a.exitcode == b.exitcode == 0
        assert sorted([queue.get(timeout=1), queue.get(timeout=1)]) == [0, 0]
        assert len(list(markers.iterdir())) == 2
    finally:
        for process in (a, b):
            if process.is_alive():
                process.terminate()
                process.join()

    separate = tmp_path / "different-markers"
    separate.mkdir()
    c = Process(
        target=_call_pool,
        args=(script, tmp_path, tmp_path / "root-c", separate, "both", queue),
    )
    d = Process(
        target=_call_pool,
        args=(script, tmp_path, tmp_path / "root-d", separate, "both", queue),
    )
    c.start()
    d.start()
    try:
        c.join(5)
        d.join(5)
        assert c.exitcode == d.exitcode == 0
        assert sorted([queue.get(timeout=1), queue.get(timeout=1)]) == [0, 0]
    finally:
        for process in (c, d):
            if process.is_alive():
                process.terminate()
                process.join()


def test_real_pdf_matches_lean_renderer_and_parent_eof_releases_lock(tmp_path):
    from iarf_assistant.addons.doc_processor.parsers.pdf_rendering import (
        PdfImageRenderer,
    )

    source = tmp_path / "source.pdf"
    document = fitz.open()
    page = document.new_page(width=300, height=400)
    page.insert_text((30, 40), "Synthetic render parity")
    document.save(source)
    document.close()
    direct_dir = tmp_path / "direct"
    direct_dir.mkdir()
    with fitz.open(source) as direct_document:
        direct = PdfImageRenderer({}, direct_dir)
        direct_png = Path(direct._render_page_to_png(direct_document[0], None))
        direct_url = direct._image_file_to_data_url(str(direct_png))
    receipts = []
    with prepared_pages(
        source,
        tmp_path / "out",
        [{"page_index": 0, "crop_box": None}],
        settings={},
        coordination_dir=tmp_path / "coord",
        encode_metadata=True,
        config=RenderConfig(1, 1, 10),
    ) as pages:
        receipts.extend(pages)
    assert Path(receipts[0]["png_path"]).read_bytes() == direct_png.read_bytes()
    assert Path(receipts[0]["data_url_path"]).read_text() == direct_url

    worker_script = (
        Path(__file__).parents[1]
        / "iarf_assistant/addons/doc_processor/parsers/pdf_render_worker.py"
    )
    slot = tmp_path / "orphan-slot.lock"
    proc = subprocess.Popen(
        [sys.executable, str(worker_script), str(slot)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert json.loads(proc.stdout.readline())["op"] == "ready"
        proc.stdin.write(
            (
                json.dumps(
                    {
                        "op": "init",
                        "source": str(source),
                        "stage": str(tmp_path / "out"),
                        "settings": {},
                    }
                )
                + "\n"
            ).encode()
        )
        proc.stdin.flush()
        assert json.loads(proc.stdout.readline())["op"] == "initialized"
        proc.stdin.close()  # Equivalent to the parent dying with an idle child.
        assert proc.wait(timeout=5) == 0
        assert _slot_free(slot)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_standalone_worker_with_forwarded_site_under_no_site_python(tmp_path):
    source = tmp_path / "source.pdf"
    document = fitz.open()
    document.new_page(width=100, height=100)
    document.save(source)
    document.close()
    stage = tmp_path / "stage"
    stage.mkdir()
    slot = tmp_path / "no-site-slot.lock"
    worker_script = (
        Path(__file__).parents[1]
        / "iarf_assistant/addons/doc_processor/parsers/pdf_render_worker.py"
    )
    host_site = Path(filelock.__file__).resolve().parent.parent
    proc = subprocess.Popen(
        [sys.executable, "-S", str(worker_script), str(slot), str(host_site)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert json.loads(proc.stdout.readline())["op"] == "ready"
        proc.stdin.write(
            (
                json.dumps(
                    {
                        "op": "init",
                        "source": str(source),
                        "stage": str(stage),
                        "settings": {},
                    }
                )
                + "\n"
            ).encode()
        )
        proc.stdin.flush()
        assert json.loads(proc.stdout.readline())["op"] == "initialized"
        proc.stdin.write(
            (
                json.dumps(
                    {
                        "op": "render",
                        "page_index": 0,
                        "crop_box": None,
                        "encode_metadata": False,
                    }
                )
                + "\n"
            ).encode()
        )
        proc.stdin.flush()
        result = json.loads(proc.stdout.readline())
        assert result["op"] == "result"
        assert (stage / result["png_name"]).is_file()
        proc.stdin.write(b'{"op":"stop"}\n')
        proc.stdin.flush()
        assert proc.wait(timeout=5) == 0
        assert _slot_free(slot)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux parent-death signal")
def test_parent_death_releases_slot_while_child_holds_gil(tmp_path):
    worker_script = (
        Path(__file__).parents[1]
        / "iarf_assistant/addons/doc_processor/parsers/pdf_render_worker.py"
    )
    slot = tmp_path / "parent-death-slot.lock"
    child_script = tmp_path / "blocked_child.py"
    child_script.write_text(
        """
import ctypes, importlib.util, json, os, site, sys
spec = importlib.util.spec_from_file_location('_render_worker', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module._arm_parent_death(int(sys.argv[4]))
site.addsitedir(sys.argv[3])
from filelock import FileLock
with FileLock(sys.argv[2]):
    print(json.dumps({'pid': os.getpid()}), flush=True)
    ctypes.PyDLL(None).sleep(60)
"""
    )
    parent_script = tmp_path / "blocked_parent.py"
    parent_script.write_text(
        """
import subprocess, sys, os
child = subprocess.Popen(
    [sys.executable, sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], str(os.getpid())],
    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
)
print(child.stdout.readline().decode().strip(), flush=True)
child.wait()
"""
    )
    host_site = Path(filelock.__file__).resolve().parent.parent
    parent = subprocess.Popen(
        [
            sys.executable,
            str(parent_script),
            str(child_script),
            str(worker_script),
            str(slot),
            str(host_site),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    child_pid = None
    child_created = None
    try:
        child_pid = json.loads(parent.stdout.readline())["pid"]
        child_created = psutil.Process(child_pid).create_time()
        assert not _slot_free(slot)
        sleep_deadline = time.monotonic() + 2
        while time.monotonic() < sleep_deadline:
            wait_channel = Path(f"/proc/{child_pid}/wchan").read_text().strip()
            if wait_channel in {"hrtimer_nanosleep", "do_nanosleep"}:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("Child did not enter native sleep")
        parent.kill()
        parent.wait(timeout=2)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                child = psutil.Process(child_pid)
                same_child = child.create_time() == child_created
                gone = not same_child or child.status() == psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:
                gone = True
            if gone and _slot_free(slot):
                break
            time.sleep(0.02)
        else:
            raise AssertionError("Child retained slot after parent death")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()
        if child_pid is not None:
            try:
                child = psutil.Process(child_pid)
                if (
                    child.create_time() == child_created
                    and child.status() != psutil.STATUS_ZOMBIE
                ):
                    child.kill()
            except psutil.NoSuchProcess:
                pass
