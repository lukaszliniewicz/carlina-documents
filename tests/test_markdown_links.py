"""Repository link checks preserve exact upstream documents."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import check_markdown_links as links


def test_only_the_two_verbatim_model_cards_are_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_docs = ("README.md", "third_party/models/project-notes.md")
    members = (*project_docs, *sorted(links.VERBATIM_UPSTREAM_CARDS))
    for member in members:
        path = tmp_path / member
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[upstream relative link](absent.md)\n")
    monkeypatch.setattr(links, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        links.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args, returncode=0, stdout="\n".join(members)
        ),
    )

    assert links.markdown_files() == tuple(tmp_path / name for name in project_docs)


def test_a_broken_project_link_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    document = tmp_path / "README.md"
    document.write_text("[missing project document](absent.md)\n")
    monkeypatch.setattr(links, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(links, "markdown_files", lambda: (document,))

    assert links.main() == 1
    assert "README.md:1: absent.md" in capsys.readouterr().out


def test_selected_links_reject_a_checkout_only_target(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("[plan](plan.md)\n")
    (tmp_path / "plan.md").write_text(
        "Present in Git, omitted from the source archive.\n"
    )

    assert links.selected_link_errors(tmp_path, ("README.md",)) == (
        "README.md:1: plan.md",
    )


def test_selected_links_use_the_artifact_root_and_members(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text(
        "[root](/README.md#title)\n"
        "[encoded](space%20name.md?view=source#title)\n"
        "[directory](../docs/)\n"
        "[root directory](/)\n"
        "[external](https://example.test/not-packaged.md)\n"
        "[anchor](#title)\n"
        "`[inline code](absent.md)`\n"
        "```text\n[example](absent.md)\n```\n"
    )
    (docs / "space name.md").write_text("# Included\n")
    (tmp_path / "README.md").write_text("# Included\n")

    assert not links.selected_link_errors(
        tmp_path, ("README.md", "docs/guide.md", "docs/space name.md")
    )


def test_selected_links_reject_paths_outside_the_artifact(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("[outside](../outside.md)\n")

    assert links.selected_link_errors(tmp_path, ("README.md",)) == (
        "README.md:1: ../outside.md",
    )


def test_selected_links_reject_a_missing_selected_target(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("[missing](missing.txt)\n")

    assert links.selected_link_errors(tmp_path, ("README.md", "missing.txt")) == (
        "README.md:1: missing.txt",
    )


def test_selected_links_preserve_only_the_two_verbatim_model_cards(
    tmp_path: Path,
) -> None:
    members = (*sorted(links.VERBATIM_UPSTREAM_CARDS), "third_party/models/notes.md")
    for member in members:
        path = tmp_path / member
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[upstream relative link](absent.md)\n")

    assert links.selected_link_errors(tmp_path, members) == (
        "third_party/models/notes.md:1: absent.md",
    )
