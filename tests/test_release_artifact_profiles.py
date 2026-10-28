from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from scripts import verify_release_artifacts as verifier


def test_documents_wheel_uses_its_closure_count_and_byte_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "policy").mkdir()
    policy = tmp_path / "policy/documents-closure.json"
    policy.write_text(json.dumps({"wheel": {"file_count": 1, "file_bytes": 1}}))
    wheel = tmp_path / "test.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("carlina/a.py", "")
        archive.writestr("carlina/b.py", "")
    monkeypatch.setattr(verifier, "PROJECT_ROOT", tmp_path)
    documents = verifier.inspect_artifact(wheel, application_profile="documents")
    workbench = verifier.inspect_artifact(wheel)
    assert "contains 2 files; budget is 1" in documents.errors
    assert any(error.startswith("archive size") for error in documents.errors)
    assert not any(
        error.startswith(("contains", "archive size")) for error in workbench.errors
    )


def test_artifact_profiles_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unsupported application profile"):
        verifier.inspect_artifact(tmp_path / "absent.whl", application_profile="other")
