from __future__ import annotations

import hashlib
import io
import json
import tarfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import pytest

from scripts import collect_documents_notices as collector


def _archive(*, licence_path: str | None = "info/licenses/LICENSE") -> bytes:
    payload = io.BytesIO()
    members = {
        "info/index.json": json.dumps(
            {"name": "example", "version": "1.2", "build": "h0_0", "subdir": "noarch"}
        ).encode(),
        "info/recipe/meta.yaml": b"source:\n  url: https://example.invalid/source\n",
    }
    if licence_path is not None:
        members[licence_path] = b"Synthetic licence\xff\n"
    with tarfile.open(fileobj=payload, mode="w:bz2") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return payload.getvalue()


def _report(payload: bytes, *, platform: str = "linux-64") -> dict[str, object]:
    digest = hashlib.sha256(payload).hexdigest()
    return {
        "format": "carlina.documents-release-inventory",
        "schema_version": 1,
        "application_profile": "documents",
        "source_revision": "a" * 40,
        "application_version": "0.1.0.post204.dev3",
        "wheel_sha256": "b" * 64,
        "platform": platform,
        "components": [
            {
                "name": "example",
                "version": "1.2",
                "bom-ref": "documents:example",
                "purl": "pkg:conda/example@1.2",
                "hashes": [{"alg": "SHA-256", "content": digest}],
                "properties": [
                    {"name": "carlina:build", "value": "h0_0"},
                    {"name": "carlina:subdir", "value": "noarch"},
                    {"name": "carlina:environment", "value": "documents"},
                ],
                "externalReferences": [
                    {
                        "type": "distribution",
                        "url": "https://conda.anaconda.org/conda-forge/noarch/example-1.2-h0_0.tar.bz2",
                    }
                ],
            },
            {"bom-ref": "other:example", "purl": "pkg:pypi/example@1.2"},
            {"bom-ref": "bundled:example", "name": "bundled-child"},
        ],
    }


def _write(path: Path, report: dict[str, object]) -> Path:
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def _cached(tmp_path: Path) -> tuple[Path, Path, Path]:
    payload = _archive()
    inventory = _write(tmp_path / "inventory.json", _report(payload))
    cache = tmp_path / "cache"
    cache.mkdir()
    archive = cache / f"{hashlib.sha256(payload).hexdigest()}.tar.bz2"
    archive.write_bytes(payload)
    return inventory, cache, archive


def _component(report: dict[str, object]) -> dict[str, object]:
    return cast(list[dict[str, object]], report["components"])[0]


def test_linux_windows_contexts_share_verified_archive(tmp_path: Path) -> None:
    payload = _archive()
    linux = _write(tmp_path / "linux.json", _report(payload))
    windows = _write(tmp_path / "windows.json", _report(payload, platform="win-64"))
    inputs, requests = collector.load_requests([linux, windows])
    assert [item.platform for item in inputs] == ["linux-64", "win-64"]
    assert len(requests) == 1
    assert len(requests[0].uses) == 2
    assert {use.inventory_sha256 for use in requests[0].uses} == {
        hashlib.sha256(path.read_bytes()).hexdigest() for path in (linux, windows)
    }


@pytest.mark.parametrize(
    "field", ["source_revision", "wheel_sha256", "application_version"]
)
def test_mixed_artifacts_rejected(tmp_path: Path, field: str) -> None:
    payload = _archive()
    linux = _write(tmp_path / "linux.json", _report(payload))
    report = _report(payload, platform="win-64")
    report[field] = {"source_revision": "c" * 40, "wheel_sha256": "c" * 64}.get(
        field, "0.1.0.post205"
    )
    with pytest.raises(collector.NoticeCollectionError):
        collector.load_requests([linux, _write(tmp_path / "windows.json", report)])


@pytest.mark.parametrize(
    "url",
    [
        "http://conda.anaconda.org/conda-forge/noarch/example-1.2-h0_0.tar.bz2",
        "https://example.invalid/conda-forge/noarch/example-1.2-h0_0.tar.bz2",
        "https://conda.anaconda.org:443/conda-forge/noarch/example-1.2-h0_0.tar.bz2",
        "https://user:secret@conda.anaconda.org/conda-forge/noarch/example-1.2-h0_0.tar.bz2",
        "https://conda.anaconda.org/other/noarch/example-1.2-h0_0.tar.bz2",
        "https://conda.anaconda.org/conda-forge/noarch/example-1.2-h0_0.tar.bz2?secret=1",
        "https://conda.anaconda.org/conda-forge/noarch/example-1.2-h0_0.tar.bz2#fragment",
        "https://conda.anaconda.org/conda-forge/noarch/other-1.2-h0_0.tar.bz2",
    ],
)
def test_only_exact_channel_archive_is_downloadable(tmp_path: Path, url: str) -> None:
    report = _report(_archive())
    _component(report)["externalReferences"] = [{"type": "distribution", "url": url}]
    with pytest.raises(collector.NoticeCollectionError) as error:
        collector.load_requests([_write(tmp_path / "input.json", report)])
    assert "secret" not in str(error.value)
    assert url not in str(error.value)


@pytest.mark.parametrize("field", ["hashes", "properties", "bom-ref", "purl"])
def test_malformed_selected_identity_rejected(tmp_path: Path, field: str) -> None:
    report = _report(_archive())
    del _component(report)[field]
    with pytest.raises(collector.NoticeCollectionError):
        collector.load_requests([_write(tmp_path / "input.json", report)])


def test_duplicate_inventory_and_component_rejected(tmp_path: Path) -> None:
    inventory = _write(tmp_path / "input.json", _report(_archive()))
    with pytest.raises(collector.NoticeCollectionError):
        collector.load_requests([inventory, inventory])
    report = _report(_archive())
    cast(list[object], report["components"]).append(_component(report))
    with pytest.raises(collector.NoticeCollectionError):
        collector.load_requests([_write(inventory, report)])


def test_inventory_symlinks_and_size_cap_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = _write(tmp_path / "input.json", _report(_archive()))
    linked = tmp_path / "linked.json"
    linked.symlink_to(inventory)
    with pytest.raises(collector.NoticeCollectionError):
        collector.load_requests([linked])
    monkeypatch.setattr(collector, "_INPUT_LIMIT", 8)
    with pytest.raises(collector.NoticeCollectionError):
        collector.load_requests([inventory])


def test_cached_collection_preserves_bytes_and_binds_index(tmp_path: Path) -> None:
    inventory, cache, archive = _cached(tmp_path)
    original = archive.read_bytes()
    output = tmp_path / "output"
    report = collector.collect_notices(
        [inventory], cache_dir=cache, output_dir=output, max_download_bytes=0
    )
    assert report["archive_count"] == report["conda_component_count"] == 1
    assert report["archive_collection_complete"] is True
    assert report["notice_acceptance_complete"] is False
    assert report["public_release_accepted"] is False
    assert report["downloaded_bytes"] == 0
    assert report["discovery_gaps"] == []
    package = cast(list[dict[str, object]], report["packages"])[0]
    members = cast(list[dict[str, object]], package["members"])
    notice = next(member for member in members if member["kind"] == "license")
    assert (output / str(notice["file"])).read_bytes() == b"Synthetic licence\xff\n"
    for member in members:
        content = (output / str(member["file"])).read_bytes()
        assert hashlib.sha256(content).hexdigest() == member["sha256"]
        assert len(content) == member["bytes"]
    assert json.loads((output / "index.json").read_bytes()) == report
    assert archive.read_bytes() == original
    repeated = collector.collect_notices(
        [inventory],
        cache_dir=cache,
        output_dir=tmp_path / "retry",
        max_download_bytes=0,
    )
    assert repeated == report


@pytest.mark.parametrize("kind", ["corrupt", "symlink"])
def test_invalid_cached_archive_never_publishes_index(
    tmp_path: Path, kind: str
) -> None:
    inventory, cache, archive = _cached(tmp_path)
    if kind == "corrupt":
        archive.write_bytes(b"private corrupt cache bytes")
    else:
        saved = tmp_path / "saved"
        archive.rename(saved)
        archive.symlink_to(saved)
    output = tmp_path / "output"
    with pytest.raises(ValueError):
        collector.collect_notices([inventory], cache_dir=cache, output_dir=output)
    assert not (output / "index.json").exists()


def test_existing_output_is_not_mutated(tmp_path: Path) -> None:
    inventory, cache, _ = _cached(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    witness = output / "keep"
    witness.write_bytes(b"existing output")
    with pytest.raises(collector.NoticeCollectionError):
        collector.collect_notices([inventory], cache_dir=cache, output_dir=output)
    assert witness.read_bytes() == b"existing output"
    assert list(output.iterdir()) == [witness]


@pytest.mark.parametrize("licence_path", [None, "info/recipe/LICENSE"])
def test_absent_or_recipe_only_licence_remains_a_discovery_gap(
    tmp_path: Path, licence_path: str | None
) -> None:
    payload = _archive(licence_path=licence_path)
    inventory = _write(tmp_path / "inventory.json", _report(payload))
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / f"{hashlib.sha256(payload).hexdigest()}.tar.bz2").write_bytes(payload)
    report = collector.collect_notices(
        [inventory],
        cache_dir=cache,
        output_dir=tmp_path / "output",
        max_download_bytes=0,
    )
    assert report["archive_collection_complete"] is True
    assert len(cast(list[object], report["discovery_gaps"])) == 1
    assert report["notice_acceptance_complete"] is False


def test_total_retained_bound_stops_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory, cache, _ = _cached(tmp_path)
    monkeypatch.setattr(collector, "_RETAINED_LIMIT", 1)
    output = tmp_path / "output"
    with pytest.raises(collector.NoticeCollectionError):
        collector.collect_notices([inventory], cache_dir=cache, output_dir=output)
    assert not output.exists()


def test_redirects_fail_without_disclosing_destination() -> None:
    with pytest.raises(collector.NoticeCollectionError) as error:
        collector._NoRedirect().redirect_request(
            collector.Request("https://conda.anaconda.org/"),
            None,
            302,
            "redirect",
            {},
            "http://127.0.0.1/private",
        )
    assert "private" not in str(error.value)


class _Response:
    status = 200

    def __init__(self, payload: bytes, url: str) -> None:
        self.headers = {"Content-Length": str(len(payload))}
        self._payload = io.BytesIO(payload)
        self._url = url

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        self._payload.close()

    def geturl(self) -> str:
        return self._url

    def read(self, size: int) -> bytes:
        return self._payload.read(size)


class _Opener:
    def __init__(self, response: _Response) -> None:
        self.response = response

    def open(self, request: object, *, timeout: int) -> _Response:
        assert timeout == 30
        assert isinstance(request, collector.Request)
        return self.response


@pytest.mark.parametrize("failure", [None, "digest", "budget", "encoding", "origin"])
def test_download_verifies_before_atomic_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    payload = _archive()
    inventory = _write(tmp_path / "inventory.json", _report(payload))
    _, requests = collector.load_requests([inventory])
    request = requests[0]
    response = _Response(
        b"wrong bytes" if failure == "digest" else payload, request.url
    )
    if failure == "encoding":
        response.headers["Content-Encoding"] = "gzip"
    if failure == "origin":
        response._url = "http://127.0.0.1/private"
    monkeypatch.setattr(collector, "build_opener", lambda *args: _Opener(response))
    cache = tmp_path / "cache"
    cache.mkdir()
    if failure is not None:
        with pytest.raises(collector.NoticeCollectionError):
            collector._download(
                request, cache, 1 if failure == "budget" else len(payload)
            )
        assert list(cache.iterdir()) == []
    else:
        assert collector._download(request, cache, len(payload)) == len(payload)
        assert collector._cache_path(cache, request).read_bytes() == payload
        assert collector._download(request, cache, 0) == 0


def test_independent_downloaders_do_not_overwrite_each_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _archive()
    inventory = _write(tmp_path / "inventory.json", _report(payload))
    _, requests = collector.load_requests([inventory])
    request = requests[0]
    monkeypatch.setattr(
        collector,
        "build_opener",
        lambda *args: _Opener(_Response(payload, request.url)),
    )
    barrier = threading.Barrier(2)
    original_link = collector.os.link

    def publish(source: Path, destination: Path) -> None:
        barrier.wait(timeout=10)
        original_link(source, destination)

    monkeypatch.setattr(collector.os, "link", publish)
    cache = tmp_path / "cache"
    cache.mkdir()
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [
            workers.submit(collector._download, request, cache, len(payload))
            for _ in range(2)
        ]
        assert [future.result(timeout=15) for future in futures] == [len(payload)] * 2
    target = collector._cache_path(cache, request)
    assert list(cache.iterdir()) == [target]
    assert target.read_bytes() == payload


def test_cli_failure_is_content_free(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    inventory = tmp_path / "private-name.json"
    inventory.write_text('{"secret":"private-payload"}', encoding="utf-8")
    assert (
        collector.main(
            [
                "--inventory",
                str(inventory),
                "--cache-dir",
                str(tmp_path / "cache"),
                "--output-dir",
                str(tmp_path / "output"),
            ]
        )
        == 2
    )
    message = capsys.readouterr().err
    assert "private" not in message
    assert "Traceback" not in message
