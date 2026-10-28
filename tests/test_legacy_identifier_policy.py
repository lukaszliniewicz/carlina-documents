from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_legacy_identifiers import audit_repository


def _policy(*, expected: int = 1) -> dict[str, object]:
    return {
        "schema_version": 1,
        "scopes": [
            {
                "id": "implementation",
                "paths": ["src/**"],
                "owner": "test",
                "classification": "temporary",
                "removal": "test",
            }
        ],
        "rules": [
            {
                "id": "old_namespace",
                "pattern": r"\bold_name\b",
                "expected_by_scope": {"implementation": expected},
            }
        ],
    }


def test_inventory_accepts_the_exact_reviewed_scope_count(tmp_path: Path):
    source = tmp_path / "src" / "module.py"
    source.parent.mkdir()
    source.write_text("import old_name\n", encoding="utf-8")

    results = audit_repository(
        tmp_path,
        _policy(),
        tracked_paths=["src/module.py"],
    )

    assert len(results) == 1
    assert results[0].valid is True
    assert results[0].scopes[0].actual == 1


@pytest.mark.parametrize("contents", ["", "old_name\nold_name\n"])
def test_inventory_rejects_both_unrecorded_growth_and_unratcheted_reduction(
    tmp_path: Path,
    contents: str,
):
    source = tmp_path / "src" / "module.py"
    source.parent.mkdir()
    source.write_text(contents, encoding="utf-8")

    result = audit_repository(
        tmp_path,
        _policy(),
        tracked_paths=["src/module.py"],
    )[0]

    assert result.valid is False


def test_inventory_rejects_matches_outside_an_owned_scope(tmp_path: Path):
    source = tmp_path / "unexpected.py"
    source.write_text("old_name\n", encoding="utf-8")

    result = audit_repository(
        tmp_path,
        _policy(expected=0),
        tracked_paths=["unexpected.py"],
    )[0]

    assert result.valid is False
    assert result.unclassified == (("unexpected.py", 1),)


def test_inventory_rejects_malformed_policy(tmp_path: Path):
    with pytest.raises(ValueError, match="one expectation for every scope"):
        audit_repository(
            tmp_path,
            {
                "schema_version": 1,
                "scopes": [{"id": "implementation", "paths": ["src/**"]}],
                "rules": [
                    {
                        "id": "old_namespace",
                        "pattern": "old_name",
                        "expected_by_scope": {},
                    }
                ],
            },
            tracked_paths=[],
        )
