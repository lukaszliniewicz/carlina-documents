from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from iarf_assistant.addons.doc_processor import epub_validation


def _test_assertion(outcome: str) -> dict[str, object]:
    return {
        "@type": "earl:assertion",
        "earl:result": {"earl:outcome": outcome},
    }


def _document_assertion(
    *tests: dict[str, object], outcome: str = "pass"
) -> dict[str, object]:
    return {
        "@type": "earl:assertion",
        "assertions": list(tests),
        "earl:result": {"earl:outcome": outcome},
    }


def _report(
    *documents: dict[str, object], revision: object = "1.4.6"
) -> dict[str, object]:
    return {
        "@type": "earl:report",
        "earl:assertedBy": {"doap:release": {"doap:revision": revision}},
        "assertions": list(documents),
    }


def _install_ace(
    monkeypatch,
    report: dict[str, object] | bytes | None,
    *,
    returncode: int = 0,
    version_output: str = "1.4.6\n",
    lookup_name: str = "ace",
):
    binary = "/tools/ace"
    calls: list[tuple[list[str], dict[str, object]]] = []

    def which(name: str) -> str | None:
        return binary if name == lookup_name else None

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        if command[-1] == "--version":
            return SimpleNamespace(returncode=0, stdout=version_output)
        report_dir = Path(command[command.index("--outdir") + 1])
        if report is not None:
            report_dir.mkdir(parents=True)
            raw_report = (
                report
                if isinstance(report, bytes)
                else json.dumps(report).encode("utf-8")
            )
            (report_dir / "report.json").write_bytes(raw_report)
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(epub_validation.shutil, "which", which)
    monkeypatch.setattr(epub_validation.subprocess, "run", run)
    return binary, calls


def _allow_structure_and_epubcheck(monkeypatch) -> None:
    monkeypatch.setattr(
        epub_validation,
        "validate_epub_structure",
        lambda _path: (True, None),
    )
    monkeypatch.setattr(
        epub_validation,
        "run_official_epubcheck",
        lambda *_args, **_kwargs: epub_validation.EpubValidationResult(
            True, None, "passed", "5.3.0"
        ),
    )


def test_optional_missing_ace_is_a_successful_not_run(monkeypatch):
    monkeypatch.delenv("ACE_EXECUTABLE", raising=False)
    monkeypatch.delenv("ACE_REQUIRED", raising=False)
    monkeypatch.setattr(epub_validation.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        epub_validation.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("ran Ace")),
    )

    result = epub_validation.run_ace("book.epub", {})

    assert result.passed is True
    assert result.epubcheck == "not_run"
    assert result.ace == "not_run"
    assert result.ace_version is None
    assert result.ace_findings is False
    assert result.as_legacy_tuple() == (True, None)


def test_required_missing_ace_fails_with_environment_true_syntax(monkeypatch):
    monkeypatch.setenv("ACE_REQUIRED", "YES")
    monkeypatch.delenv("ACE_EXECUTABLE", raising=False)
    monkeypatch.setattr(epub_validation.shutil, "which", lambda _name: None)

    result = epub_validation.run_ace("private/book.epub", config={})

    assert result.passed is False
    assert result.ace == "failed"
    assert result.message == (
        "Ace accessibility validation is required, but Ace is unavailable."
    )
    assert "private/book.epub" not in str(result.message)


def test_configured_missing_ace_fails_even_when_optional(monkeypatch):
    monkeypatch.setattr(epub_validation.shutil, "which", lambda _name: None)

    result = epub_validation.run_ace(
        "book.epub", config={"ACE_EXECUTABLE": "/private/missing-ace"}
    )

    assert result.passed is False
    assert result.ace == "failed"
    assert result.message == "Configured Ace executable was not found."
    assert "/private/missing-ace" not in str(result.message)


def test_config_overrides_environment_for_executable_and_required(monkeypatch):
    monkeypatch.setenv("ACE_EXECUTABLE", "environment-ace")
    monkeypatch.setenv("ACE_REQUIRED", "true")
    report = _report(
        _document_assertion(outcome="earl:pass"),
        _document_assertion(_test_assertion("pass")),
    )
    binary, calls = _install_ace(
        monkeypatch,
        report,
        lookup_name="configured-ace",
    )

    result = epub_validation.run_ace(
        "book.epub",
        config={"ACE_EXECUTABLE": "configured-ace", "ACE_REQUIRED": False},
    )

    assert result.passed is True
    assert result.ace == "passed"
    assert result.ace_version == "1.4.6"
    assert calls[0][0][0] == binary
    assert len(calls) == 1


def test_valid_empty_and_nested_pass_assertions_pass_and_cleanup_scratch(
    tmp_path, monkeypatch
):
    report = _report(
        _document_assertion(),
        _document_assertion(_test_assertion("earl:pass")),
    )
    _binary, calls = _install_ace(monkeypatch, report)

    result = epub_validation.run_ace(str(tmp_path / "book.epub"), config={})

    assert result.passed is True
    assert result.ace == "passed"
    assert result.ace_version == "1.4.6"
    command, kwargs = calls[0]
    assert command == [
        "/tools/ace",
        "--silent",
        "--doNotReportMedia",
        "--timeout",
        "30000",
        "--outdir",
        command[command.index("--outdir") + 1],
        "--tempdir",
        command[command.index("--tempdir") + 1],
        str(tmp_path / "book.epub"),
    ]
    assert kwargs == {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "check": False,
        "timeout": 180,
    }
    report_dir = Path(command[command.index("--outdir") + 1])
    work_dir = Path(command[command.index("--tempdir") + 1])
    assert not report_dir.parent.exists()
    assert not work_dir.exists()


def test_nested_failure_fails_even_when_ace_exits_zero(monkeypatch):
    _binary, _calls = _install_ace(
        monkeypatch,
        _report(
            _document_assertion(
                _test_assertion("earl:fail"),
                outcome="fail",
            )
        ),
    )

    result = epub_validation.run_ace("book.epub", config={})

    assert result.passed is False
    assert result.ace == "failed"
    assert result.message == "Ace reported accessibility findings."
    assert result.ace_findings is True


@pytest.mark.parametrize("outcome", ["cantTell", "earl:untested"])
def test_incomplete_outcomes_fail_closed(monkeypatch, outcome):
    _install_ace(
        monkeypatch,
        _report(_document_assertion(_test_assertion(outcome), outcome="pass")),
    )

    result = epub_validation.run_ace("book.epub", config={})

    assert result.passed is False
    assert result.ace == "failed"
    assert result.message == "Ace accessibility validation has incomplete results."
    assert result.ace_findings is False


def test_nonzero_ace_exit_fails_even_with_a_clean_report(monkeypatch):
    _install_ace(
        monkeypatch,
        _report(_document_assertion()),
        returncode=1,
    )

    result = epub_validation.run_ace("book.epub", config={})

    assert result.passed is False
    assert result.ace == "failed"
    assert result.message == "Ace exited with an error during accessibility validation."
    assert result.ace_findings is False


@pytest.mark.parametrize(
    "report",
    [
        None,
        b"{invalid json",
        b'{"@type":"earl:report","assertions":' + b"[" * 1100 + b"]" * 1100 + b"}",
        b" " * (8 * 1024 * 1024 + 1),
        {"@type": "earl:report", "assertions": {}},
    ],
    ids=[
        "missing",
        "invalid-json",
        "deeply-nested",
        "oversized",
        "unsupported-shape",
    ],
)
def test_missing_invalid_oversized_or_unsupported_report_fails(monkeypatch, report):
    _install_ace(monkeypatch, report)

    result = epub_validation.run_ace("private/book.epub", config={})

    assert result.passed is False
    assert result.ace == "failed"
    assert result.message is not None
    assert "private/book.epub" not in result.message
    assert "1.4.6" not in result.message
    assert result.ace_findings is False


def test_timed_out_ace_fails_and_cleans_scratch(monkeypatch, tmp_path):
    monkeypatch.setattr(epub_validation.shutil, "which", lambda _name: "/tools/ace")
    captured: dict[str, Path] = {}

    def timeout(command: list[str], **_kwargs: object) -> None:
        captured["report"] = Path(command[command.index("--outdir") + 1])
        captured["work"] = Path(command[command.index("--tempdir") + 1])
        raise subprocess.TimeoutExpired(command, timeout=180)

    monkeypatch.setattr(epub_validation.subprocess, "run", timeout)

    result = epub_validation.run_ace(str(tmp_path / "book.epub"), config={})

    assert result.passed is False
    assert result.ace == "failed"
    assert result.message == "Ace accessibility validation timed out."
    assert not captured["report"].parent.exists()
    assert not captured["work"].exists()


def test_unavailable_scratch_fails_without_running_ace(monkeypatch):
    monkeypatch.setattr(epub_validation.shutil, "which", lambda _name: "/tools/ace")
    monkeypatch.setattr(
        epub_validation.tempfile,
        "TemporaryDirectory",
        lambda **_kwargs: (_ for _ in ()).throw(PermissionError("private path")),
    )
    monkeypatch.setattr(
        epub_validation.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("ran Ace")),
    )

    result = epub_validation.run_ace("book.epub", config={})

    assert result.passed is False
    assert result.ace == "failed"
    assert result.message == (
        "Unable to prepare temporary files for Ace accessibility validation."
    )


def test_missing_report_version_uses_bounded_version_probe(monkeypatch):
    _binary, calls = _install_ace(
        monkeypatch,
        _report(_document_assertion(), revision="invalid revision"),
        version_output="1.4.6\n",
    )

    result = epub_validation.run_ace("book.epub", config={})

    assert result.passed is True
    assert result.ace_version == "1.4.6"
    assert calls[1][0] == ["/tools/ace", "--version"]
    assert calls[1][1] == {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "check": False,
        "text": True,
        "timeout": 10,
    }


def test_unvalidated_version_probe_does_not_fail_success(monkeypatch):
    _install_ace(
        monkeypatch,
        _report(_document_assertion(), revision=None),
        version_output="Ace version unavailable",
    )

    result = epub_validation.run_ace("book.epub", config={})

    assert result.passed is True
    assert result.ace == "passed"
    assert result.ace_version is None
    assert result.ace_findings is False


def test_validate_epub_file_skips_ace_after_epubcheck_failure(monkeypatch):
    monkeypatch.setattr(
        epub_validation,
        "validate_epub_structure",
        lambda _path: (True, None),
    )
    monkeypatch.setattr(
        epub_validation,
        "run_official_epubcheck",
        lambda *_args, **_kwargs: epub_validation.EpubValidationResult(
            False, "EPUBCheck failed.", "failed", "5.3.0"
        ),
    )
    monkeypatch.setattr(
        epub_validation,
        "run_ace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("ran Ace")),
    )

    result = epub_validation.validate_epub_file("book.epub", config={})

    assert result.passed is False
    assert result.message == "EPUBCheck failed."
    assert result.epubcheck == "failed"
    assert result.version == "5.3.0"
    assert result.ace == "not_run"
    assert result.ace_version is None
    assert result.ace_findings is False


def test_validate_epub_file_merges_ace_after_epubcheck_not_run(monkeypatch):
    monkeypatch.setattr(
        epub_validation,
        "validate_epub_structure",
        lambda _path: (True, None),
    )
    monkeypatch.setattr(
        epub_validation,
        "run_official_epubcheck",
        lambda *_args, **_kwargs: epub_validation.EpubValidationResult(
            True, None, "not_run"
        ),
    )
    monkeypatch.setattr(
        epub_validation,
        "run_ace",
        lambda *_args, **_kwargs: epub_validation.EpubValidationResult(
            True, None, "not_run", ace="passed", ace_version="1.4.6"
        ),
    )

    result = epub_validation.validate_epub_file("book.epub", config={})

    assert result.passed is True
    assert result.epubcheck == "not_run"
    assert result.ace == "passed"
    assert result.ace_version == "1.4.6"
    assert result.ace_findings is False


@pytest.mark.parametrize(
    ("environment_required", "configured_required", "expected_passed"),
    [("true", False, True), ("false", True, False)],
    ids=["optional-review-draft", "required-publishing-lane"],
)
def test_validate_epub_file_only_allows_completed_ace_findings_when_optional(
    monkeypatch, environment_required, configured_required, expected_passed
):
    monkeypatch.setenv("ACE_REQUIRED", environment_required)
    _allow_structure_and_epubcheck(monkeypatch)
    _install_ace(
        monkeypatch,
        _report(_document_assertion(_test_assertion("fail"), outcome="fail")),
    )

    result = epub_validation.validate_epub_file(
        "book.epub", config={"ACE_REQUIRED": configured_required}
    )

    assert result.passed is expected_passed
    assert result.message == "Ace reported accessibility findings."
    assert result.epubcheck == "passed"
    assert result.ace == "failed"
    assert result.ace_version == "1.4.6"
    assert result.ace_findings is True


@pytest.mark.parametrize(
    ("report", "returncode"),
    [
        (None, 0),
        (b"{invalid json", 0),
        (_report(_document_assertion(_test_assertion("cantTell"))), 0),
        (_report(_document_assertion()), 1),
    ],
    ids=["missing-report", "malformed-report", "incomplete-report", "nonzero-exit"],
)
def test_validate_epub_file_does_not_bypass_report_or_exit_failures(
    monkeypatch, report, returncode
):
    monkeypatch.setenv("ACE_REQUIRED", "false")
    _allow_structure_and_epubcheck(monkeypatch)
    _install_ace(monkeypatch, report, returncode=returncode)

    result = epub_validation.validate_epub_file(
        "book.epub", config={"ACE_REQUIRED": False}
    )

    assert result.passed is False
    assert result.ace == "failed"
    assert result.ace_findings is False


def test_validate_epub_file_does_not_bypass_timeout(monkeypatch):
    monkeypatch.setenv("ACE_REQUIRED", "false")
    _allow_structure_and_epubcheck(monkeypatch)
    monkeypatch.setattr(epub_validation.shutil, "which", lambda _name: "/tools/ace")
    monkeypatch.setattr(
        epub_validation.subprocess,
        "run",
        lambda command, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(command, timeout=180)
        ),
    )

    result = epub_validation.validate_epub_file(
        "book.epub", config={"ACE_REQUIRED": False}
    )

    assert result.passed is False
    assert result.ace == "failed"
    assert result.ace_findings is False
    assert result.message == "Ace accessibility validation timed out."


def test_incomplete_report_with_failure_is_not_bypassable_findings(monkeypatch):
    _install_ace(
        monkeypatch,
        _report(
            _document_assertion(
                _test_assertion("fail"),
                _test_assertion("cantTell"),
                outcome="fail",
            )
        ),
    )

    result = epub_validation.run_ace("book.epub", config={})

    assert result.passed is False
    assert result.ace == "failed"
    assert result.ace_findings is False
    assert result.message == "Ace accessibility validation has incomplete results."


def test_old_result_constructor_and_legacy_tuple_remain_compatible():
    result = epub_validation.EpubValidationResult(True, None, "passed", "5.3.0")

    assert result.ace == "not_run"
    assert result.ace_version is None
    assert result.ace_findings is False
    assert result.as_legacy_tuple() == (True, None)
