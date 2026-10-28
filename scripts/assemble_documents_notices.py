"""Assemble verified Conda notice candidates into a portable bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import unicodedata
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn, cast
from urllib.parse import quote, urlsplit

_COLLECTION_FORMAT: Final = "carlina.documents-conda-notices"
_COLLECTION_SCHEMA: Final = 1
_SUPPLEMENT_FORMAT: Final = "carlina.documents-conda-notice-supplement"
_SUPPLEMENT_SCHEMA: Final = 1
_BUNDLE_FORMAT: Final = "carlina.documents-conda-notice-bundle"
_BUNDLE_SCHEMA: Final = 1
_INDEX_LIMIT: Final = 16 * 1024 * 1024
_SUPPLEMENT_LIMIT: Final = 1024 * 1024
_NOTICE_LIMIT: Final = 16 * 1024 * 1024
_METADATA_LIMIT: Final = 2 * 1024 * 1024
_RETAINED_LIMIT: Final = 256 * 1024 * 1024
_PACKAGE_LIMIT: Final = 4096
_MEMBER_LIMIT: Final = 100_000
_DOWNLOAD_LIMIT: Final = 2 * 1024 * 1024 * 1024
_SHA256: Final = re.compile(r"[0-9a-f]{64}\Z")
_REVISION: Final = re.compile(r"[0-9a-f]{40}\Z")
_TOKEN: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+\-]*\Z")
_REFERENCE: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:+@\-]*\Z")
_DRIVE_PREFIX: Final = re.compile(r"[A-Za-z]:")
_WINDOWS_ABSOLUTE: Final = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]")
_UNC_ABSOLUTE: Final = re.compile(r"(?<![\w:])\\\\[^\\/\s]+[\\/]")
_POSIX_ABSOLUTE: Final = re.compile(r"(?<![\w:/])/(?![/\\])\S+")
_ERROR: Final = "Documents notice assembly failed; no acceptance is implied."
_OUTPUT_ERROR: Final = "Documents notices require a new output directory."
_COLLECTED_BASIS: Final = "collected_license"
_GAP_BASES: Final = frozenset(
    {
        "metadata_only_wrapper",
        "embedded_header_notice",
        "same_source_sibling_notice",
    }
)
_MEMBER_KEYS: Final = frozenset({"archive_path", "kind", "sha256", "bytes", "file"})
_PACKAGE_KEYS: Final = frozenset(
    {
        "name",
        "version",
        "build",
        "subdir",
        "archive_sha256",
        "archive_url",
        "uses",
        "members",
        "package_license_candidate_count",
    }
)
_INVENTORY_KEYS: Final = frozenset(
    {
        "sha256",
        "platform",
        "source_revision",
        "application_version",
        "wheel_sha256",
        "component_count",
        "conda_component_count",
    }
)
_USE_KEYS: Final = frozenset(
    {"inventory_sha256", "platform", "environment", "component_ref"}
)
_SUPPLEMENT_KEYS: Final = frozenset(
    {"format", "schema_version", "collection_index_sha256", "decisions"}
)
_DECISION_KEYS: Final = frozenset({"archive_sha256", "basis", "explanation", "notices"})
_SUPPLEMENT_NOTICE_KEYS: Final = frozenset(
    {"file", "sha256", "bytes", "archive_path", "origin_archive_sha256"}
)


class NoticeAssemblyError(ValueError):
    """An assembly failure that does not disclose input paths or contents."""


@dataclass(frozen=True, slots=True)
class _InputTree:
    path: Path
    root_fd: int
    files_fd: int
    root_identity: tuple[int, int, int, int, int]
    files_identity: tuple[int, int, int, int, int]

    def verify(self) -> None:
        try:
            root_path_status = self.path.lstat()
            root_fd_status = os.fstat(self.root_fd)
            files_path_status = self.path / "files"
            files_leaf_status = os.stat(
                "files", dir_fd=self.root_fd, follow_symlinks=False
            )
            files_fd_status = os.fstat(self.files_fd)
            files_resolved_status = files_path_status.lstat()
        except OSError:
            raise NoticeAssemblyError(_ERROR) from None
        if (
            not stat.S_ISDIR(root_path_status.st_mode)
            or _identity(root_path_status) != self.root_identity
            or _identity(root_fd_status) != self.root_identity
            or not stat.S_ISDIR(files_leaf_status.st_mode)
            or _identity(files_leaf_status) != self.files_identity
            or _identity(files_fd_status) != self.files_identity
            or _identity(files_resolved_status) != self.files_identity
        ):
            raise NoticeAssemblyError(_ERROR)

    def read_root_file(self, name: str, limit: int) -> bytes:
        return self._read_file(self.root_fd, self.path / name, name, limit)

    def read_blob(self, digest: str, size: int, limit: int) -> bytes:
        leaf = digest
        return self._read_file(
            self.files_fd, self.path / "files" / leaf, leaf, limit, size
        )

    def _read_file(
        self,
        parent_fd: int,
        path: Path,
        leaf: str,
        limit: int,
        expected_size: int | None = None,
    ) -> bytes:
        self.verify()
        try:
            before = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            path_before = path.lstat()
        except OSError:
            raise NoticeAssemblyError(_ERROR) from None
        if (
            not stat.S_ISREG(before.st_mode)
            or _identity(before) != _identity(path_before)
            or before.st_size > limit
            or (expected_size is not None and before.st_size != expected_size)
        ):
            raise NoticeAssemblyError(_ERROR)

        flags = os.O_RDONLY | cast(int, getattr(os, "O_NOFOLLOW", 0))
        flags |= cast(int, getattr(os, "O_CLOEXEC", 0))
        try:
            descriptor = os.open(leaf, flags, dir_fd=parent_fd)
        except OSError:
            raise NoticeAssemblyError(_ERROR) from None
        with os.fdopen(descriptor, "rb") as handle:
            try:
                opened = os.fstat(handle.fileno())
                if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(
                    before
                ):
                    raise NoticeAssemblyError(_ERROR)
                content = handle.read(limit + 1)
                after_fd = os.fstat(handle.fileno())
                after_leaf = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
                after_path = path.lstat()
            except OSError:
                raise NoticeAssemblyError(_ERROR) from None
        if (
            len(content) > limit
            or (expected_size is not None and len(content) != expected_size)
            or _identity(after_fd) != _identity(opened)
            or _identity(after_leaf) != _identity(opened)
            or _identity(after_path) != _identity(opened)
        ):
            raise NoticeAssemblyError(_ERROR)
        self.verify()
        return content


@dataclass(frozen=True, slots=True)
class _Member:
    archive_path: str
    kind: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class _Inventory:
    sha256: str
    platform: str
    source_revision: str
    application_version: str
    wheel_sha256: str
    component_count: int
    conda_component_count: int

    def as_json(self) -> dict[str, object]:
        return {
            "sha256": self.sha256,
            "platform": self.platform,
            "source_revision": self.source_revision,
            "application_version": self.application_version,
            "wheel_sha256": self.wheel_sha256,
            "component_count": self.component_count,
            "conda_component_count": self.conda_component_count,
        }


@dataclass(frozen=True, slots=True)
class _Package:
    name: str
    version: str
    build: str
    subdir: str
    archive_sha256: str
    archive_url: str
    uses: tuple[dict[str, str], ...]
    members: tuple[_Member, ...]
    license_candidate_count: int

    def identity(self) -> tuple[str, str, str, str]:
        return self.name, self.version, self.build, self.subdir


@dataclass(frozen=True, slots=True)
class _SupplementNotice:
    file: str
    sha256: str
    size: int
    archive_path: str
    origin_archive_sha256: str


@dataclass(frozen=True, slots=True)
class _Decision:
    archive_sha256: str
    basis: str
    explanation: str
    notices: tuple[_SupplementNotice, ...]


@contextmanager
def _open_tree(root: Path) -> Generator[_InputTree]:
    root_fd = -1
    files_fd = -1
    try:
        root_status = root.lstat()
        if not stat.S_ISDIR(root_status.st_mode):
            raise NoticeAssemblyError(_ERROR)
        flags = os.O_RDONLY | cast(int, getattr(os, "O_DIRECTORY", 0))
        flags |= cast(int, getattr(os, "O_NOFOLLOW", 0))
        flags |= cast(int, getattr(os, "O_CLOEXEC", 0))
        root_fd = os.open(root, flags)
        opened_root = os.fstat(root_fd)
        if not stat.S_ISDIR(opened_root.st_mode) or _identity(opened_root) != _identity(
            root_status
        ):
            raise NoticeAssemblyError(_ERROR)

        files_status = os.stat("files", dir_fd=root_fd, follow_symlinks=False)
        if not stat.S_ISDIR(files_status.st_mode):
            raise NoticeAssemblyError(_ERROR)
        files_fd = os.open("files", flags, dir_fd=root_fd)
        opened_files = os.fstat(files_fd)
        if not stat.S_ISDIR(opened_files.st_mode) or _identity(
            opened_files
        ) != _identity(files_status):
            raise NoticeAssemblyError(_ERROR)

        tree = _InputTree(
            root,
            root_fd,
            files_fd,
            _identity(opened_root),
            _identity(opened_files),
        )
        tree.verify()
        yield tree
        tree.verify()
    except NoticeAssemblyError:
        raise
    except OSError:
        raise NoticeAssemblyError(_ERROR) from None
    finally:
        if files_fd >= 0:
            os.close(files_fd)
        if root_fd >= 0:
            os.close(root_fd)


def _identity(status: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise NoticeAssemblyError(_ERROR)
        result[key] = value
    return result


def _json_object(content: bytes) -> dict[str, object]:
    parsed: object = json.loads(
        content.decode("utf-8"), object_pairs_hook=_unique_object
    )
    if not isinstance(parsed, dict):
        raise NoticeAssemblyError(_ERROR)
    raw = cast(dict[object, object], parsed)
    if any(not isinstance(key, str) for key in raw):
        raise NoticeAssemblyError(_ERROR)
    return cast(dict[str, object], raw)


def _exact_keys(value: dict[str, object], expected: frozenset[str]) -> None:
    if frozenset(value) != expected:
        raise NoticeAssemblyError(_ERROR)


def _object_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise NoticeAssemblyError(_ERROR)
    return cast(list[object], value)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or _has_control(value):
        raise NoticeAssemblyError(_ERROR)
    return value


def _has_control(value: str) -> bool:
    return any(unicodedata.category(character) in {"Cc", "Cs"} for character in value)


def _sha256(value: object) -> str:
    result = _text(value)
    if _SHA256.fullmatch(result) is None:
        raise NoticeAssemblyError(_ERROR)
    return result


def _positive_int(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise NoticeAssemblyError(_ERROR)
    return value


def _nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise NoticeAssemblyError(_ERROR)
    return value


def _member_size(value: object, kind: str) -> int:
    if kind == "license":
        return _positive_int(value)
    return _nonnegative_int(value)


def _safe_archive_path(value: object) -> str:
    result = _text(value)
    parts = result.split("/")
    if (
        result.startswith("/")
        or "\\" in result
        or _DRIVE_PREFIX.match(result) is not None
        or any(_DRIVE_PREFIX.match(part) is not None for part in parts)
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise NoticeAssemblyError(_ERROR)
    return result


def _safe_reference(value: object) -> str:
    result = _text(value)
    if _REFERENCE.fullmatch(result) is None:
        raise NoticeAssemblyError(_ERROR)
    return result


def _no_private_path(value: str) -> None:
    if (
        _WINDOWS_ABSOLUTE.search(value) is not None
        or _UNC_ABSOLUTE.search(value) is not None
        or _POSIX_ABSOLUTE.search(value) is not None
    ):
        raise NoticeAssemblyError(_ERROR)


def _read_json(
    tree: _InputTree, filename: str, limit: int
) -> tuple[bytes, dict[str, object]]:
    payload = tree.read_root_file(filename, limit)
    return payload, _json_object(payload)


def _package_url(
    name: str, version: str, build: str, subdir: str, value: object
) -> str:
    url = _text(value)
    parsed = urlsplit(url)
    prefix = (
        f"/conda-forge/{subdir}/"
        f"{quote(name, safe='')}-{quote(version, safe='')}-"
        f"{quote(build, safe='')}"
    )
    if (
        parsed.scheme != "https"
        or parsed.netloc != "conda.anaconda.org"
        or parsed.path not in {f"{prefix}.conda", f"{prefix}.tar.bz2"}
        or parsed.query
        or parsed.fragment
        or url != f"https://conda.anaconda.org{parsed.path}"
    ):
        raise NoticeAssemblyError(_ERROR)
    return url


def _load_inventories(report: dict[str, object]) -> tuple[_Inventory, ...]:
    raw_inventories = _object_list(report.get("inventory_inputs"))
    if not 1 <= len(raw_inventories) <= 2:
        raise NoticeAssemblyError(_ERROR)
    inventories: list[_Inventory] = []
    seen_sha: set[str] = set()
    seen_platform: set[str] = set()
    for raw in raw_inventories:
        if not isinstance(raw, dict):
            raise NoticeAssemblyError(_ERROR)
        item = cast(dict[str, object], raw)
        _exact_keys(item, _INVENTORY_KEYS)
        digest = _sha256(item.get("sha256"))
        revision = _text(item.get("source_revision"))
        wheel_sha = _sha256(item.get("wheel_sha256"))
        version = _text(item.get("application_version"))
        platform = _text(item.get("platform"))
        component_count = _nonnegative_int(item.get("component_count"))
        conda_count = _nonnegative_int(item.get("conda_component_count"))
        if (
            _REVISION.fullmatch(revision) is None
            or _TOKEN.fullmatch(version) is None
            or platform not in {"linux-64", "win-64"}
            or conda_count > component_count
            or digest in seen_sha
            or platform in seen_platform
        ):
            raise NoticeAssemblyError(_ERROR)
        seen_sha.add(digest)
        seen_platform.add(platform)
        inventories.append(
            _Inventory(
                digest,
                platform,
                revision,
                version,
                wheel_sha,
                component_count,
                conda_count,
            )
        )
    return tuple(inventories)


def _load_collection(
    report: dict[str, object],
) -> tuple[tuple[_Inventory, ...], tuple[_Package, ...], frozenset[str]]:
    if (
        report.get("format") != _COLLECTION_FORMAT
        or type(report.get("schema_version")) is not int
        or report.get("schema_version") != _COLLECTION_SCHEMA
        or report.get("application_profile") != "documents"
        or report.get("archive_collection_complete") is not True
        or report.get("notice_acceptance_complete") is not False
        or report.get("public_release_accepted") is not False
    ):
        raise NoticeAssemblyError(_ERROR)
    version = _text(report.get("application_version"))
    revision = _text(report.get("source_revision"))
    wheel_sha = _sha256(report.get("wheel_sha256"))
    if _TOKEN.fullmatch(version) is None or _REVISION.fullmatch(revision) is None:
        raise NoticeAssemblyError(_ERROR)
    inventories = _load_inventories(report)
    if any(
        (item.application_version, item.source_revision, item.wheel_sha256)
        != (version, revision, wheel_sha)
        for item in inventories
    ):
        raise NoticeAssemblyError(_ERROR)
    if _nonnegative_int(report.get("downloaded_bytes")) > _DOWNLOAD_LIMIT:
        raise NoticeAssemblyError(_ERROR)
    collection_conda_count = _positive_int(report.get("conda_component_count"))
    inventory_conda_count = sum(item.conda_component_count for item in inventories)
    if collection_conda_count != inventory_conda_count:
        raise NoticeAssemblyError(_ERROR)

    raw_packages = _object_list(report.get("packages"))
    if (
        not raw_packages
        or len(raw_packages) > _PACKAGE_LIMIT
        or _positive_int(report.get("archive_count")) != len(raw_packages)
    ):
        raise NoticeAssemblyError(_ERROR)
    inventory_by_sha = {item.sha256: item for item in inventories}
    packages: list[_Package] = []
    seen_archive: set[str] = set()
    seen_identity: set[tuple[str, str, str, str]] = set()
    member_count = 0
    use_count_by_inventory = {item.sha256: 0 for item in inventories}
    seen_component_refs: set[tuple[str, str]] = set()
    for raw_package in raw_packages:
        if not isinstance(raw_package, dict):
            raise NoticeAssemblyError(_ERROR)
        package = cast(dict[str, object], raw_package)
        _exact_keys(package, _PACKAGE_KEYS)
        name = _text(package.get("name"))
        package_version = _text(package.get("version"))
        build = _text(package.get("build"))
        subdir = _text(package.get("subdir"))
        archive_sha = _sha256(package.get("archive_sha256"))
        if (
            any(
                _TOKEN.fullmatch(value) is None
                for value in (name, package_version, build)
            )
            or subdir not in {"noarch", "linux-64", "win-64"}
            or archive_sha in seen_archive
        ):
            raise NoticeAssemblyError(_ERROR)
        identity = (name, package_version, build, subdir)
        if identity in seen_identity:
            raise NoticeAssemblyError(_ERROR)
        seen_archive.add(archive_sha)
        seen_identity.add(identity)
        archive_url = _package_url(
            name, package_version, build, subdir, package.get("archive_url")
        )

        raw_uses = _object_list(package.get("uses"))
        if not raw_uses:
            raise NoticeAssemblyError(_ERROR)
        uses: list[dict[str, str]] = []
        seen_uses: set[tuple[str, str, str, str]] = set()
        for raw_use in raw_uses:
            if not isinstance(raw_use, dict):
                raise NoticeAssemblyError(_ERROR)
            use = cast(dict[str, object], raw_use)
            _exact_keys(use, _USE_KEYS)
            inventory_sha = _sha256(use.get("inventory_sha256"))
            inventory = inventory_by_sha.get(inventory_sha)
            platform = _text(use.get("platform"))
            environment = _safe_reference(use.get("environment"))
            component_ref = _safe_reference(use.get("component_ref"))
            key = (inventory_sha, platform, environment, component_ref)
            if inventory is None or platform != inventory.platform or key in seen_uses:
                raise NoticeAssemblyError(_ERROR)
            component_key = (inventory_sha, component_ref)
            if component_key in seen_component_refs:
                raise NoticeAssemblyError(_ERROR)
            seen_component_refs.add(component_key)
            use_count_by_inventory[inventory_sha] += 1
            if use_count_by_inventory[inventory_sha] > inventory.conda_component_count:
                raise NoticeAssemblyError(_ERROR)
            seen_uses.add(key)
            uses.append(
                {
                    "inventory_sha256": inventory_sha,
                    "platform": platform,
                    "environment": environment,
                    "component_ref": component_ref,
                }
            )

        raw_members = _object_list(package.get("members"))
        member_count += len(raw_members)
        if member_count > _MEMBER_LIMIT:
            raise NoticeAssemblyError(_ERROR)
        members: list[_Member] = []
        candidate_count = 0
        for raw_member in raw_members:
            if not isinstance(raw_member, dict):
                raise NoticeAssemblyError(_ERROR)
            member = cast(dict[str, object], raw_member)
            _exact_keys(member, _MEMBER_KEYS)
            archive_path = _safe_archive_path(member.get("archive_path"))
            kind = _text(member.get("kind"))
            if kind not in {"license", "metadata", "recipe"}:
                raise NoticeAssemblyError(_ERROR)
            digest = _sha256(member.get("sha256"))
            size = _member_size(member.get("bytes"), kind)
            if member.get("file") != f"files/{digest}":
                raise NoticeAssemblyError(_ERROR)
            limit = _NOTICE_LIMIT if kind == "license" else _METADATA_LIMIT
            if size > limit:
                raise NoticeAssemblyError(_ERROR)
            members.append(_Member(archive_path, kind, digest, size))
            if kind == "license" and not archive_path.startswith("info/recipe/"):
                candidate_count += 1
        if (
            _nonnegative_int(package.get("package_license_candidate_count"))
            != candidate_count
        ):
            raise NoticeAssemblyError(_ERROR)
        packages.append(
            _Package(
                name,
                package_version,
                build,
                subdir,
                archive_sha,
                archive_url,
                tuple(uses),
                tuple(members),
                candidate_count,
            )
        )

    gaps = frozenset(
        package.archive_sha256
        for package in packages
        if package.license_candidate_count == 0
    )
    raw_gaps = _object_list(report.get("discovery_gaps"))
    if len(raw_gaps) > _PACKAGE_LIMIT:
        raise NoticeAssemblyError(_ERROR)
    listed_gaps: set[str] = set()
    for raw_gap in raw_gaps:
        if not isinstance(raw_gap, dict):
            raise NoticeAssemblyError(_ERROR)
        gap = cast(dict[str, object], raw_gap)
        if frozenset(gap) != frozenset({"archive_sha256", "reason"}):
            raise NoticeAssemblyError(_ERROR)
        digest = _sha256(gap.get("archive_sha256"))
        _text(gap.get("reason"))
        if digest in listed_gaps:
            raise NoticeAssemblyError(_ERROR)
        listed_gaps.add(digest)
    if listed_gaps != set(gaps):
        raise NoticeAssemblyError(_ERROR)
    if sum(use_count_by_inventory.values()) != collection_conda_count or any(
        use_count_by_inventory[item.sha256] != item.conda_component_count
        for item in inventories
    ):
        raise NoticeAssemblyError(_ERROR)
    return (
        inventories,
        tuple(sorted(packages, key=lambda item: item.archive_sha256)),
        gaps,
    )


def _load_supplement(
    manifest: dict[str, object],
    collection_sha256: str,
    gap_hashes: frozenset[str],
    packages_by_sha: dict[str, _Package],
) -> tuple[_Decision, ...]:
    _exact_keys(manifest, _SUPPLEMENT_KEYS)
    if (
        manifest.get("format") != _SUPPLEMENT_FORMAT
        or type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != _SUPPLEMENT_SCHEMA
        or manifest.get("collection_index_sha256") != collection_sha256
    ):
        raise NoticeAssemblyError(_ERROR)
    raw_decisions = _object_list(manifest.get("decisions"))
    if len(raw_decisions) != len(gap_hashes):
        raise NoticeAssemblyError(_ERROR)
    decisions: list[_Decision] = []
    seen_decisions: set[str] = set()
    for raw_decision in raw_decisions:
        if not isinstance(raw_decision, dict):
            raise NoticeAssemblyError(_ERROR)
        decision = cast(dict[str, object], raw_decision)
        _exact_keys(decision, _DECISION_KEYS)
        digest = _sha256(decision.get("archive_sha256"))
        basis = _text(decision.get("basis"))
        explanation = _text(decision.get("explanation"))
        raw_notices = _object_list(decision.get("notices"))
        if (
            digest not in gap_hashes
            or digest in seen_decisions
            or basis not in _GAP_BASES
            or len(explanation) > 4096
            or not explanation.strip()
        ):
            raise NoticeAssemblyError(_ERROR)
        _no_private_path(explanation)
        seen_decisions.add(digest)
        notices: list[_SupplementNotice] = []
        for raw_notice in raw_notices:
            if not isinstance(raw_notice, dict):
                raise NoticeAssemblyError(_ERROR)
            notice = cast(dict[str, object], raw_notice)
            _exact_keys(notice, _SUPPLEMENT_NOTICE_KEYS)
            sha256 = _sha256(notice.get("sha256"))
            size = _positive_int(notice.get("bytes"))
            file_ref = _text(notice.get("file"))
            archive_path = _safe_archive_path(notice.get("archive_path"))
            origin_sha = _sha256(notice.get("origin_archive_sha256"))
            if file_ref != f"files/{sha256}" or size > _NOTICE_LIMIT:
                raise NoticeAssemblyError(_ERROR)
            if basis == "embedded_header_notice" and origin_sha != digest:
                raise NoticeAssemblyError(_ERROR)
            if basis == "same_source_sibling_notice":
                sibling = packages_by_sha.get(origin_sha)
                if sibling is None or origin_sha == digest:
                    raise NoticeAssemblyError(_ERROR)
                if not any(
                    member.kind == "license"
                    and member.archive_path == archive_path
                    and member.sha256 == sha256
                    and member.size == size
                    for member in sibling.members
                ):
                    raise NoticeAssemblyError(_ERROR)
            notices.append(
                _SupplementNotice(file_ref, sha256, size, archive_path, origin_sha)
            )
        if basis == "metadata_only_wrapper" and notices:
            raise NoticeAssemblyError(_ERROR)
        if (
            basis in {"embedded_header_notice", "same_source_sibling_notice"}
            and not notices
        ):
            raise NoticeAssemblyError(_ERROR)
        decisions.append(_Decision(digest, basis, explanation, tuple(notices)))
    if seen_decisions != set(gap_hashes):
        raise NoticeAssemblyError(_ERROR)
    return tuple(sorted(decisions, key=lambda item: item.archive_sha256))


def _collect_expected_sizes(
    packages: tuple[_Package, ...], decisions: tuple[_Decision, ...]
) -> tuple[dict[str, tuple[int, int]], dict[str, tuple[int, int]]]:
    collection_sizes: dict[str, tuple[int, int]] = {}
    supplement_sizes: dict[str, tuple[int, int]] = {}
    reference_count = sum(len(package.members) for package in packages) + sum(
        len(decision.notices) for decision in decisions
    )
    if reference_count > _MEMBER_LIMIT:
        raise NoticeAssemblyError(_ERROR)
    for package in packages:
        for member in package.members:
            limit = _NOTICE_LIMIT if member.kind == "license" else _METADATA_LIMIT
            _record_expected_size(collection_sizes, member.sha256, member.size, limit)
    for decision in decisions:
        for notice in decision.notices:
            _record_expected_size(
                supplement_sizes, notice.sha256, notice.size, _NOTICE_LIMIT
            )
            previous = collection_sizes.get(notice.sha256)
            if previous is not None and previous[0] != notice.size:
                raise NoticeAssemblyError(_ERROR)
    return collection_sizes, supplement_sizes


def _record_expected_size(
    sizes: dict[str, tuple[int, int]], digest: str, size: int, limit: int
) -> None:
    previous = sizes.get(digest)
    if previous is not None and previous[0] != size:
        raise NoticeAssemblyError(_ERROR)
    sizes[digest] = (size, min(limit, previous[1]) if previous else limit)


def _read_blobs(
    tree: _InputTree, sizes: dict[str, tuple[int, int]]
) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for digest, (size, limit) in sorted(sizes.items()):
        content = tree.read_blob(digest, size, limit)
        if hashlib.sha256(content).hexdigest() != digest:
            raise NoticeAssemblyError(_ERROR)
        result[digest] = content
    return result


def _validate_total_bytes(
    collection_payload: bytes,
    supplement_payload: bytes,
    collection_sizes: dict[str, tuple[int, int]],
    supplement_sizes: dict[str, tuple[int, int]],
) -> None:
    all_sizes: dict[str, int] = {}
    for sizes in (collection_sizes, supplement_sizes):
        for digest, (size, _limit) in sizes.items():
            previous = all_sizes.get(digest)
            if previous is not None and previous != size:
                raise NoticeAssemblyError(_ERROR)
            all_sizes[digest] = size
    if (
        len(collection_payload) + len(supplement_payload) + sum(all_sizes.values())
        > _RETAINED_LIMIT
    ):
        raise NoticeAssemblyError(_ERROR)


def _output_notice(
    archive_path: str,
    digest: str,
    size: int,
    origin_archive_sha256: str,
    basis: str,
) -> dict[str, object]:
    return {
        "archive_path": archive_path,
        "sha256": digest,
        "bytes": size,
        "file": f"files/{digest}",
        "origin_archive_sha256": origin_archive_sha256,
        "basis": basis,
    }


def _write_output(
    output_dir: Path,
    report: dict[str, object],
    notice_files: dict[str, bytes],
) -> None:
    try:
        output_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
        files_dir = output_dir / "files"
        files_dir.mkdir(mode=0o700, exist_ok=False)
        flags = os.O_RDONLY | cast(int, getattr(os, "O_DIRECTORY", 0))
        flags |= cast(int, getattr(os, "O_NOFOLLOW", 0))
        flags |= cast(int, getattr(os, "O_CLOEXEC", 0))
        output_fd = os.open(output_dir, flags)
        try:
            files_fd = os.open("files", flags, dir_fd=output_fd)
            try:
                for digest, content in sorted(notice_files.items()):
                    write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    write_flags |= cast(int, getattr(os, "O_NOFOLLOW", 0))
                    write_flags |= cast(int, getattr(os, "O_CLOEXEC", 0))
                    descriptor = os.open(digest, write_flags, 0o600, dir_fd=files_fd)
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(content)
                payload = (
                    json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2)
                    + "\n"
                )
                index_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                index_flags |= cast(int, getattr(os, "O_NOFOLLOW", 0))
                index_flags |= cast(int, getattr(os, "O_CLOEXEC", 0))
                index_fd = os.open("index.json", index_flags, 0o600, dir_fd=output_fd)
                with os.fdopen(index_fd, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(payload)
            finally:
                os.close(files_fd)
        finally:
            os.close(output_fd)
    except NoticeAssemblyError:
        raise
    except OSError:
        raise NoticeAssemblyError(_ERROR) from None


def _assemble(
    collection_dir: Path, supplement_dir: Path, output_dir: Path
) -> dict[str, object]:
    if (
        os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
    ):
        raise NoticeAssemblyError(_ERROR)
    try:
        output_dir.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        raise NoticeAssemblyError(_OUTPUT_ERROR) from None
    else:
        raise NoticeAssemblyError(_OUTPUT_ERROR)

    with _open_tree(collection_dir) as collection:
        collection_payload, collection_report = _read_json(
            collection, "index.json", _INDEX_LIMIT
        )
        collection_sha = hashlib.sha256(collection_payload).hexdigest()
        inventories, packages, gap_hashes = _load_collection(collection_report)
        packages_by_sha = {item.archive_sha256: item for item in packages}

        with _open_tree(supplement_dir) as supplement:
            supplement_payload, supplement_report = _read_json(
                supplement, "manifest.json", _SUPPLEMENT_LIMIT
            )
            supplement_sha = hashlib.sha256(supplement_payload).hexdigest()
            decisions = _load_supplement(
                supplement_report, collection_sha, gap_hashes, packages_by_sha
            )
            collection_sizes, supplement_sizes = _collect_expected_sizes(
                packages, decisions
            )
            _validate_total_bytes(
                collection_payload,
                supplement_payload,
                collection_sizes,
                supplement_sizes,
            )
            collection_content = _read_blobs(collection, collection_sizes)
            supplement_content = _read_blobs(supplement, supplement_sizes)
            if any(
                collection_content[digest] != supplement_content[digest]
                for digest in collection_content.keys() & supplement_content.keys()
            ):
                raise NoticeAssemblyError(_ERROR)

    decisions_by_sha = {item.archive_sha256: item for item in decisions}
    notice_files: dict[str, bytes] = {}
    output_packages: list[dict[str, object]] = []
    for package in packages:
        notices = [
            _output_notice(
                member.archive_path,
                member.sha256,
                member.size,
                package.archive_sha256,
                _COLLECTED_BASIS,
            )
            for member in package.members
            if member.kind == "license"
        ]
        decision = decisions_by_sha.get(package.archive_sha256)
        if decision is not None:
            notices.extend(
                _output_notice(
                    notice.archive_path,
                    notice.sha256,
                    notice.size,
                    notice.origin_archive_sha256,
                    decision.basis,
                )
                for notice in decision.notices
            )
        notices.sort(
            key=lambda item: (
                cast(str, item["archive_path"]),
                cast(str, item["sha256"]),
                cast(str, item["origin_archive_sha256"]),
                cast(str, item["basis"]),
            )
        )
        for item in notices:
            digest = cast(str, item["sha256"])
            content = collection_content.get(digest, supplement_content.get(digest))
            if content is None:
                raise NoticeAssemblyError(_ERROR)
            previous = notice_files.get(digest)
            if previous is not None and previous != content:
                raise NoticeAssemblyError(_ERROR)
            notice_files[digest] = content
        gap_decision: dict[str, str] | None = None
        if decision is not None:
            gap_decision = {
                "basis": decision.basis,
                "explanation": decision.explanation,
            }
        output_packages.append(
            {
                "name": package.name,
                "version": package.version,
                "build": package.build,
                "subdir": package.subdir,
                "archive_sha256": package.archive_sha256,
                "archive_url": package.archive_url,
                "uses": list(package.uses),
                "notices": notices,
                "gap_decision": gap_decision,
            }
        )

    bundle: dict[str, object] = {
        "format": _BUNDLE_FORMAT,
        "schema_version": _BUNDLE_SCHEMA,
        "application_profile": "documents",
        "application_version": _text(collection_report.get("application_version")),
        "source_revision": _text(collection_report.get("source_revision")),
        "wheel_sha256": _sha256(collection_report.get("wheel_sha256")),
        "inventory_inputs": [item.as_json() for item in inventories],
        "collection_index_sha256": collection_sha,
        "supplement_manifest_sha256": supplement_sha,
        "archive_count": len(output_packages),
        "notice_file_count": len(notice_files),
        "total_notice_bytes": sum(len(content) for content in notice_files.values()),
        "discovery_gaps_accounted_for": True,
        "notice_acceptance_complete": False,
        "public_release_accepted": False,
        "packages": output_packages,
    }
    _write_output(output_dir, bundle, notice_files)
    return bundle


def assemble_notices(
    collection_dir: Path, supplement_dir: Path, output_dir: Path
) -> dict[str, object]:
    """Write an offline, hash-bound notice bundle from validated local inputs."""
    try:
        return _assemble(collection_dir, supplement_dir, output_dir)
    except NoticeAssemblyError:
        raise
    except OSError, ValueError, TypeError, KeyError, RecursionError:
        raise NoticeAssemblyError(_ERROR) from None


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        print(_ERROR, file=sys.stderr)
        raise SystemExit(2)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--collection-dir", type=Path, required=True)
    parser.add_argument("--supplement-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = assemble_notices(
            cast(Path, args.collection_dir),
            cast(Path, args.supplement_dir),
            cast(Path, args.output_dir),
        )
    except NoticeAssemblyError:
        print(_ERROR, file=sys.stderr)
        return 2
    print(
        "Assembled "
        f"{report['archive_count']} archives, "
        f"{report['notice_file_count']} notice files, "
        f"{report['total_notice_bytes']} notice bytes."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
