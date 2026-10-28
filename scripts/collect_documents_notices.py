"""Collect Conda notice candidates from exact Documents dependency archives."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if not __package__ and os.fspath(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(PROJECT_ROOT))

if TYPE_CHECKING or not __package__:
    from scripts.documents_conda_notices import CondaNoticeBundle, read_conda_notices
else:
    from .documents_conda_notices import CondaNoticeBundle, read_conda_notices

_INPUT_LIMIT: Final = 16 * 1024 * 1024
_ARCHIVE_LIMIT: Final = 512 * 1024 * 1024
_DOWNLOAD_LIMIT: Final = 2 * 1024 * 1024 * 1024
_RETAINED_LIMIT: Final = 256 * 1024 * 1024
_MEMBER_LIMIT: Final = 100_000
_MAX_INVENTORIES: Final = 2
_MAX_ARCHIVES: Final = 4096
_SHA256: Final = re.compile(r"[0-9a-f]{64}\Z")
_REVISION: Final = re.compile(r"[0-9a-f]{40}\Z")
_TOKEN: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+\-]*\Z")
_ERROR: Final = "Documents notice collection failed; retained archives may be reused."
_INPUT_ERROR: Final = "Documents notice inventory is invalid."
_CACHE_ERROR: Final = "Documents notice cache is invalid."
_OUTPUT_ERROR: Final = "Documents notices require a new output directory."
_NETWORK_ERROR: Final = "Documents archive download failed."


class NoticeCollectionError(ValueError):
    """A collection failure that does not disclose input paths or contents."""


@dataclass(frozen=True, slots=True)
class ArchiveUse:
    inventory_sha256: str
    platform: str
    environment: str
    component_ref: str


@dataclass(frozen=True, slots=True)
class ArchiveRequest:
    name: str
    version: str
    build: str
    subdir: str
    sha256: str
    url: str
    uses: tuple[ArchiveUse, ...]


@dataclass(frozen=True, slots=True)
class InventoryInput:
    sha256: str
    platform: str
    source_revision: str
    application_version: str
    wheel_sha256: str
    component_count: int
    conda_component_count: int


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise NoticeCollectionError(_INPUT_ERROR)
    raw = cast(dict[object, object], value)
    if any(not isinstance(key, str) for key in raw):
        raise NoticeCollectionError(_INPUT_ERROR)
    return cast(dict[str, object], raw)


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise NoticeCollectionError(_INPUT_ERROR)
    return cast(list[object], value)


def _string(value: object) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise NoticeCollectionError(_INPUT_ERROR)
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise NoticeCollectionError(_INPUT_ERROR)
        result[key] = value
    return result


def _stable_identity(status: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _read_inventory(path: Path) -> tuple[str, dict[str, object]]:
    initial = path.lstat()
    if not stat.S_ISREG(initial.st_mode) or initial.st_size > _INPUT_LIMIT:
        raise NoticeCollectionError(_INPUT_ERROR)
    flags = os.O_RDONLY | cast(int, getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or _stable_identity(
            opened
        ) != _stable_identity(initial):
            raise NoticeCollectionError(_INPUT_ERROR)
        payload = handle.read(_INPUT_LIMIT + 1)
        if len(payload) > _INPUT_LIMIT or any(
            _stable_identity(status) != _stable_identity(opened)
            for status in (os.fstat(handle.fileno()), path.lstat())
        ):
            raise NoticeCollectionError(_INPUT_ERROR)
    parsed: object = json.loads(payload, object_pairs_hook=_unique_object)
    return hashlib.sha256(payload).hexdigest(), _object(parsed)


def _properties(component: dict[str, object]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in _list(component.get("properties")):
        prop = _object(raw)
        name = _string(prop.get("name"))
        if name in result:
            raise NoticeCollectionError(_INPUT_ERROR)
        result[name] = _string(prop.get("value"))
    return result


def _archive_request(
    component: dict[str, object], inventory_sha: str, platform: str
) -> ArchiveRequest:
    name = _string(component.get("name"))
    version = _string(component.get("version"))
    props = _properties(component)
    build = _string(props.get("carlina:build"))
    subdir = _string(props.get("carlina:subdir"))
    if (
        any(_TOKEN.fullmatch(value) is None for value in (name, version, build))
        or subdir not in {"noarch", platform}
        or component.get("purl")
        != f"pkg:conda/{quote(name, safe='')}@{quote(version, safe='')}"
    ):
        raise NoticeCollectionError(_INPUT_ERROR)
    hashes = [_object(raw) for raw in _list(component.get("hashes"))]
    sha256 = _string(hashes[0].get("content")) if len(hashes) == 1 else ""
    if (
        not sha256
        or hashes[0].get("alg") != "SHA-256"
        or _SHA256.fullmatch(sha256) is None
    ):
        raise NoticeCollectionError(_INPUT_ERROR)
    references = [_object(raw) for raw in _list(component.get("externalReferences"))]
    if len(references) != 1 or references[0].get("type") != "distribution":
        raise NoticeCollectionError(_INPUT_ERROR)
    url = _string(references[0].get("url"))
    parsed = urlsplit(url)
    stem = f"/conda-forge/{subdir}/{name}-{version}-{build}"
    if (
        parsed.scheme != "https"
        or parsed.netloc != "conda.anaconda.org"
        or parsed.path not in {f"{stem}.conda", f"{stem}.tar.bz2"}
        or parsed.query
        or parsed.fragment
        or url != f"https://conda.anaconda.org{parsed.path}"
    ):
        raise NoticeCollectionError(_INPUT_ERROR)
    use = ArchiveUse(
        inventory_sha,
        platform,
        _string(props.get("carlina:environment")),
        _string(component.get("bom-ref")),
    )
    return ArchiveRequest(name, version, build, subdir, sha256, url, (use,))


def load_requests(
    inventories: Sequence[Path],
) -> tuple[tuple[InventoryInput, ...], tuple[ArchiveRequest, ...]]:
    """Validate and deduplicate one artifact's Linux/Windows Conda closures."""
    if not inventories or len(inventories) > _MAX_INVENTORIES:
        raise NoticeCollectionError(_INPUT_ERROR)
    inputs: list[InventoryInput] = []
    requests: dict[str, ArchiveRequest] = {}
    for path in inventories:
        digest, report = _read_inventory(path)
        platform = _string(report.get("platform"))
        revision = _string(report.get("source_revision"))
        wheel_sha = _string(report.get("wheel_sha256"))
        if (
            report.get("format") != "carlina.documents-release-inventory"
            or type(report.get("schema_version")) is not int
            or report.get("schema_version") != 1
            or report.get("application_profile") != "documents"
            or platform not in {"linux-64", "win-64"}
            or _REVISION.fullmatch(revision) is None
            or _SHA256.fullmatch(wheel_sha) is None
        ):
            raise NoticeCollectionError(_INPUT_ERROR)
        components = [_object(raw) for raw in _list(report.get("components"))]
        refs: set[str] = set()
        conda_count = 0
        for component in components:
            ref = _string(component.get("bom-ref"))
            if ref in refs:
                raise NoticeCollectionError(_INPUT_ERROR)
            refs.add(ref)
            purl = component.get("purl")
            if purl is None:
                props = _properties(component) if "properties" in component else {}
                if {"carlina:build", "carlina:subdir"}.intersection(props):
                    raise NoticeCollectionError(_INPUT_ERROR)
                continue
            if not _string(purl).startswith("pkg:conda/"):
                continue
            request = _archive_request(component, digest, platform)
            conda_count += 1
            previous = requests.get(request.sha256)
            if previous is not None:
                if (
                    previous.name,
                    previous.version,
                    previous.build,
                    previous.subdir,
                    previous.url,
                ) != (
                    request.name,
                    request.version,
                    request.build,
                    request.subdir,
                    request.url,
                ):
                    raise NoticeCollectionError(_INPUT_ERROR)
                request = ArchiveRequest(
                    request.name,
                    request.version,
                    request.build,
                    request.subdir,
                    request.sha256,
                    request.url,
                    (*previous.uses, *request.uses),
                )
            requests[request.sha256] = request
        inputs.append(
            InventoryInput(
                digest,
                platform,
                revision,
                _string(report.get("application_version")),
                wheel_sha,
                len(components),
                conda_count,
            )
        )
    if (
        not requests
        or len(requests) > _MAX_ARCHIVES
        or len({item.platform for item in inputs}) != len(inputs)
        or len(
            {
                (item.source_revision, item.application_version, item.wheel_sha256)
                for item in inputs
            }
        )
        != 1
    ):
        raise NoticeCollectionError(_INPUT_ERROR)
    return tuple(inputs), tuple(requests[key] for key in sorted(requests))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        raise NoticeCollectionError(_NETWORK_ERROR)


def _cache_path(cache: Path, request: ArchiveRequest) -> Path:
    suffix = ".conda" if request.url.endswith(".conda") else ".tar.bz2"
    return cache / f"{request.sha256}{suffix}"


def _download(request: ArchiveRequest, cache: Path, remaining: int) -> int:
    target = _cache_path(cache, request)
    if target.exists() or target.is_symlink():
        return 0  # The reader verifies every cached archive before reuse.
    temporary = cache / f".download-{uuid.uuid4().hex}.partial"
    downloaded = 0
    digest = hashlib.sha256()
    opener = build_opener(_NoRedirect())
    try:
        http_request = Request(request.url, headers={"Accept-Encoding": "identity"})
        with (
            opener.open(http_request, timeout=30) as response,
            temporary.open("xb") as out,
        ):
            if (
                response.status != 200
                or response.geturl() != request.url
                or response.headers.get("Content-Encoding", "identity") != "identity"
            ):
                raise NoticeCollectionError(_NETWORK_ERROR)
            length = response.headers.get("Content-Length")
            if length is not None and (
                not length.isdecimal() or int(length) > min(_ARCHIVE_LIMIT, remaining)
            ):
                raise NoticeCollectionError(_NETWORK_ERROR)
            while chunk := response.read(64 * 1024):
                downloaded += len(chunk)
                if downloaded > min(_ARCHIVE_LIMIT, remaining):
                    raise NoticeCollectionError(_NETWORK_ERROR)
                digest.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if digest.hexdigest() != request.sha256:
            raise NoticeCollectionError(_NETWORK_ERROR)
        # A concurrent collector owns any destination created in the meantime.
        # Hard-link publication is atomic and never overwrites an existing file.
        os.link(temporary, target)
    except FileExistsError:
        if not target.exists():
            raise NoticeCollectionError(_CACHE_ERROR) from None
    finally:
        temporary.unlink(missing_ok=True)
    return downloaded


def _package_record(
    request: ArchiveRequest, bundle: CondaNoticeBundle, files: Path
) -> dict[str, object]:
    if (bundle.name, bundle.version, bundle.build, bundle.archive_sha256) != (
        request.name,
        request.version,
        request.build,
        request.sha256,
    ) or (bundle.subdir is not None and bundle.subdir != request.subdir):
        raise NoticeCollectionError(_INPUT_ERROR)
    members: list[dict[str, object]] = []
    for member in bundle.members:
        digest = hashlib.sha256(member.content).hexdigest()
        if digest != member.sha256:
            raise NoticeCollectionError(_INPUT_ERROR)
        target = files / digest
        if target.exists():
            if target.read_bytes() != member.content:
                raise NoticeCollectionError(_OUTPUT_ERROR)
        else:
            with target.open("xb") as handle:
                handle.write(member.content)
        members.append(
            {
                "archive_path": member.relative_path,
                "kind": member.kind,
                "sha256": digest,
                "bytes": len(member.content),
                "file": f"files/{digest}",
            }
        )
    package_licenses = sum(
        member.kind == "license" and not member.relative_path.startswith("info/recipe/")
        for member in bundle.members
    )
    return {
        "name": request.name,
        "version": request.version,
        "build": request.build,
        "subdir": request.subdir,
        "archive_sha256": request.sha256,
        "archive_url": request.url,
        "uses": [asdict(use) for use in request.uses],
        "members": members,
        "package_license_candidate_count": package_licenses,
    }


def collect_notices(
    inventories: Sequence[Path],
    *,
    cache_dir: Path,
    output_dir: Path,
    max_download_bytes: int = _DOWNLOAD_LIMIT,
) -> dict[str, object]:
    """Retain verified raw candidates; discovery never asserts licence acceptance."""
    if not 0 <= max_download_bytes <= _DOWNLOAD_LIMIT:
        raise NoticeCollectionError(_INPUT_ERROR)
    inputs, requests = load_requests(inventories)
    if output_dir.exists() or output_dir.is_symlink():
        raise NoticeCollectionError(_OUTPUT_ERROR)
    cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not stat.S_ISDIR(cache_dir.lstat().st_mode):
        raise NoticeCollectionError(_CACHE_ERROR)
    bundles: list[tuple[ArchiveRequest, CondaNoticeBundle]] = []
    downloaded = 0
    retained = 0
    member_count = 0
    for index, request in enumerate(requests, start=1):
        downloaded += _download(request, cache_dir, max_download_bytes - downloaded)
        bundle = read_conda_notices(
            _cache_path(cache_dir, request),
            expected_sha256=request.sha256,
            name=request.name,
            version=request.version,
            build=request.build,
        )
        retained += sum(len(member.content) for member in bundle.members)
        member_count += len(bundle.members)
        if retained > _RETAINED_LIMIT or member_count > _MEMBER_LIMIT:
            raise NoticeCollectionError(_INPUT_ERROR)
        bundles.append((request, bundle))
        if index % 20 == 0 or index == len(requests):
            print(f"Verified Conda archives: {index}/{len(requests)}", flush=True)
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    files = output_dir / "files"
    files.mkdir(mode=0o700)
    packages = [_package_record(request, bundle, files) for request, bundle in bundles]
    gaps = [
        {
            "archive_sha256": package["archive_sha256"],
            "reason": "No package licence candidate was found in the locked archive.",
        }
        for package in packages
        if package["package_license_candidate_count"] == 0
    ]
    report: dict[str, object] = {
        "format": "carlina.documents-conda-notices",
        "schema_version": 1,
        "application_profile": "documents",
        "application_version": inputs[0].application_version,
        "source_revision": inputs[0].source_revision,
        "wheel_sha256": inputs[0].wheel_sha256,
        "basis": "SHA-256 verified locked Conda archives; retained bytes are unmodified",
        "inventory_inputs": [asdict(item) for item in inputs],
        "archive_collection_complete": True,
        "notice_acceptance_complete": False,
        "public_release_accepted": False,
        "downloaded_bytes": downloaded,
        "archive_count": len(packages),
        "conda_component_count": sum(item.conda_component_count for item in inputs),
        "packages": packages,
        "discovery_gaps": gaps,
        "required_follow_up": [
            "Review package notices, recipe-only notices and candidate gaps.",
            "Collect installed PyPI, frontend and pinned model notices separately.",
            "Accept distribution licences and any corresponding-source obligations.",
        ],
    }
    with (output_dir / "index.json").open("x", encoding="utf-8") as handle:
        handle.write(
            json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
        )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, action="append", required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-download-bytes", type=int, default=_DOWNLOAD_LIMIT)
    args = parser.parse_args(argv)
    try:
        report = collect_notices(
            cast(list[Path], args.inventory),
            cache_dir=cast(Path, args.cache_dir),
            output_dir=cast(Path, args.output_dir),
            max_download_bytes=cast(int, args.max_download_bytes),
        )
    except OSError, ValueError, KeyError:
        print(_ERROR, file=sys.stderr)
        return 2
    print(
        f"Collected {report['archive_count']} archives; notice acceptance remains open."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
