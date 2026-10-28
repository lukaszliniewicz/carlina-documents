"""Reject broken repository-local links in tracked Markdown documents."""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import unquote, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INLINE_LINK = re.compile(r"!?\[[^\]]*\]\((?P<target><[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)")
REFERENCE_LINK = re.compile(r"^\s*\[[^\]]+\]:\s*(?P<target><[^>]+>|\S+)")
FENCE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE = re.compile(r"`[^`]*`")
EXTERNAL_SCHEMES = {
    "app",
    "data",
    "file",
    "ftp",
    "http",
    "https",
    "mailto",
    "tel",
}
# Preserve these publisher documents byte-for-byte. Their relative links refer
# to upstream sites, and their identities are checked with the pinned assets.
VERBATIM_UPSTREAM_CARDS = frozenset(
    {
        "third_party/models/segment-any-text-sat-12l-sm.md",
        "third_party/models/FacebookAI-xlm-roberta-base.md",
    }
)


def markdown_files() -> tuple[Path, ...]:
    completed = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "*.md",
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(
        path
        for raw_path in completed.stdout.splitlines()
        if raw_path not in VERBATIM_UPSTREAM_CARDS
        if (path := PROJECT_ROOT / raw_path).is_file()
    )


def targets(markdown: str) -> tuple[tuple[int, str], ...]:
    found: list[tuple[int, str]] = []
    fenced = False
    for line_number, line in enumerate(markdown.splitlines(), start=1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        searchable = INLINE_CODE.sub("", line)
        for match in INLINE_LINK.finditer(searchable):
            found.append((line_number, match.group("target")))
        reference = REFERENCE_LINK.match(searchable)
        if reference:
            found.append((line_number, reference.group("target")))
    return tuple(found)


def local_path(
    source: Path, raw_target: str, *, root: Path | None = None
) -> Path | None:
    target = raw_target.removeprefix("<").removesuffix(">").strip()
    if not target or target.startswith("#"):
        return None
    parsed = urlsplit(target)
    if parsed.scheme.lower() in EXTERNAL_SCHEMES or parsed.netloc:
        return None
    decoded = unquote(parsed.path)
    if not decoded:
        return None
    if decoded.startswith("/"):
        return (PROJECT_ROOT if root is None else root) / decoded.lstrip("/")
    return source.parent / decoded


def selected_link_errors(root: Path, members: Sequence[str]) -> tuple[str, ...]:
    """Check authored Markdown against the files selected for a source artifact."""

    selected = frozenset(members)
    source_root = root.resolve()
    broken: list[str] = []
    for member in sorted(selected):
        if not member.endswith(".md") or member in VERBATIM_UPSTREAM_CARDS:
            continue
        source = source_root / member
        for line_number, target in targets(source.read_text(encoding="utf-8")):
            destination = local_path(source, target, root=source_root)
            if destination is None:
                continue
            try:
                relative = destination.resolve().relative_to(source_root).as_posix()
            except ValueError:
                broken.append(f"{member}:{line_number}: {target}")
                continue
            included = relative in selected or (
                destination.is_dir()
                and (
                    relative == "."
                    or any(name.startswith(f"{relative}/") for name in selected)
                )
            )
            if not destination.exists() or not included:
                broken.append(f"{member}:{line_number}: {target}")
    return tuple(broken)


def main() -> int:
    broken: list[str] = []
    checked = 0
    for source in markdown_files():
        content = source.read_text(encoding="utf-8")
        for line_number, target in targets(content):
            destination = local_path(source, target)
            if destination is None:
                continue
            checked += 1
            if not destination.exists():
                relative_source = source.relative_to(PROJECT_ROOT)
                broken.append(f"{relative_source}:{line_number}: {target}")
    if broken:
        print("Broken repository-local Markdown links:")
        for item in sorted(broken):
            print(f"  {item}")
        return 1
    print(f"Markdown links: {checked} local targets valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
