from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from scripts import check_documents_closure as checker
from scripts import measure_documents_closure as collector


def _source_summary() -> dict[str, object]:
    policy = checker._load_policy()
    classified = policy.host_retention
    grouped_members: dict[str, list[str]] = {}
    for name, limits in policy.source.items():
        if name == "carlina":
            members = sorted(
                member for member in classified if member.startswith("carlina/")
            )
        elif name == "iarf_assistant.host":
            members = sorted(
                member for member in classified if not member.startswith("carlina/")
            )
        else:
            addon = name.removeprefix("iarf_assistant.addons.")
            members = [
                f"iarf_assistant/addons/{addon}/__closure_member_{index:04}.py"
                for index in range(limits["member_count"])
            ]
        grouped_members[name] = members
    return {
        "groups": [
            {
                "name": name,
                "member_count": len(grouped_members[name]),
                "bytes": limits["bytes"],
                "modules": [],
                "members": grouped_members[name],
            }
            for name, limits in policy.source.items()
        ]
    }


def _lock_summary(platform: str) -> dict[str, object]:
    policy = checker._load_policy()
    return {
        "platform": platform,
        "contexts": [
            {
                "context": context,
                "package_count": count,
                "packages": [],
            }
            for context, count in policy.locks[platform].items()
        ],
    }


def _install_synthetic_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collector, "source_metrics", lambda _root: _source_summary())
    monkeypatch.setattr(
        collector,
        "locked_dependencies",
        lambda _root, platform: _lock_summary(platform),
    )


def test_current_policy_parses_and_exact_budget_scopes_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = checker._load_policy()
    assert policy.fresh_documents_host_module_limit == 140
    _install_synthetic_metrics(monkeypatch)
    monkeypatch.setattr(
        collector,
        "wheel_metrics",
        lambda _wheel: pytest.fail("wheel collector called without a wheel"),
    )

    assert checker.verify_closure(tmp_path, platform="linux-64") == ()
    assert checker.verify_closure(tmp_path, platform="win-64") == ()


def test_unknown_platform_is_rejected_before_collection(tmp_path: Path) -> None:
    with pytest.raises(
        ValueError,
        match=r"^Documents closure policy or measurement input is invalid\.$",
    ):
        checker.verify_closure(tmp_path, platform="darwin-64")


def test_source_growth_returns_only_owner_and_metric_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source_summary()
    groups = cast(list[dict[str, object]], source["groups"])
    carlina = next(group for group in groups if group["name"] == "carlina")
    members = cast(list[str], carlina["members"])
    count_limit = checker._load_policy().source["carlina"]["member_count"]
    for index in range(max(1, count_limit - len(members) + 1)):
        members.append(f"carlina/new_member_{index:04}.py")
    members.sort()
    carlina["member_count"] = len(members)
    carlina["bytes"] = cast(int, carlina["bytes"]) + 1
    monkeypatch.setattr(collector, "source_metrics", lambda _root: source)
    monkeypatch.setattr(
        collector,
        "locked_dependencies",
        lambda _root, platform: _lock_summary(platform),
    )

    assert checker.verify_closure(tmp_path, platform="linux-64") == (
        "source.carlina.bytes",
        "source.carlina.member_count",
        "source.host_retention.unclassified",
    )


def test_host_retention_reports_new_and_stale_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source_summary()
    groups = cast(list[dict[str, object]], source["groups"])
    carlina = next(group for group in groups if group["name"] == "carlina")
    carlina_members = cast(list[str], carlina["members"])
    carlina_members.append("carlina/new_member.py")
    carlina_members.sort()
    carlina["member_count"] = len(carlina_members)
    monkeypatch.setattr(collector, "source_metrics", lambda _root: source)
    monkeypatch.setattr(
        collector,
        "locked_dependencies",
        lambda _root, platform: _lock_summary(platform),
    )

    violations = checker.verify_closure(tmp_path, platform="linux-64")
    assert "source.host_retention.unclassified" in violations
    assert "source.host_retention.stale" not in violations

    source = _source_summary()
    groups = cast(list[dict[str, object]], source["groups"])
    carlina = next(group for group in groups if group["name"] == "carlina")
    carlina_members = cast(list[str], carlina["members"])
    removed_member = carlina_members.pop()
    carlina["member_count"] = len(carlina_members)
    monkeypatch.setattr(collector, "source_metrics", lambda _root: source)

    assert checker.verify_closure(tmp_path, platform="linux-64") == (
        "source.host_retention.stale",
    )
    assert removed_member not in carlina_members


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "not_list",
        "non_string",
        "duplicate_within_group",
        "duplicate_across_groups",
        "unsorted",
        "count_mismatch",
        "unsafe_path",
    ],
)
def test_malformed_or_duplicate_measured_members_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    source = _source_summary()
    groups = cast(list[dict[str, object]], source["groups"])
    addon = next(
        group
        for group in groups
        if group["name"] == "iarf_assistant.addons.doc_processor"
    )
    members = cast(list[str], addon["members"])
    if mutation == "missing":
        del addon["members"]
    elif mutation == "not_list":
        addon["members"] = None
    elif mutation == "non_string":
        addon["members"] = [1]
    elif mutation == "duplicate_within_group":
        members.append(members[0])
        addon["member_count"] = len(members)
    elif mutation == "duplicate_across_groups":
        host = next(group for group in groups if group["name"] == "iarf_assistant.host")
        host_members = cast(list[str], host["members"])
        host_members.append(members[0])
        host_members.sort()
        host["member_count"] = len(host_members)
    elif mutation == "unsorted":
        members.reverse()
    elif mutation == "count_mismatch":
        addon["member_count"] = cast(int, addon["member_count"]) + 1
    else:
        addon["members"] = ["../escape.py"]

    monkeypatch.setattr(collector, "source_metrics", lambda _root: source)
    monkeypatch.setattr(
        collector,
        "locked_dependencies",
        lambda _root, platform: _lock_summary(platform),
    )
    with pytest.raises(
        ValueError,
        match=r"^Documents closure policy or measurement input is invalid\.$",
    ):
        checker.verify_closure(tmp_path, platform="linux-64")


def test_lock_growth_uses_platform_specific_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_synthetic_metrics(monkeypatch)

    def measured_locks(_root: Path, platform: str) -> dict[str, object]:
        result = _lock_summary(platform)
        contexts = cast(list[dict[str, object]], result["contexts"])
        documents = next(item for item in contexts if item["context"] == "documents")
        documents["package_count"] = 180
        return result

    monkeypatch.setattr(collector, "locked_dependencies", measured_locks)

    assert checker.verify_closure(tmp_path, platform="linux-64") == ()
    assert checker.verify_closure(tmp_path, platform="win-64") == (
        "locks.documents.package_count",
    )


def test_wheel_growth_rejects_size_and_entry_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_synthetic_metrics(monkeypatch)
    policy = checker._load_policy()
    monkeypatch.setattr(
        collector,
        "wheel_metrics",
        lambda _wheel: {
            "file_bytes": policy.wheel_file_bytes + 1,
            "file_count": policy.wheel_file_count + 1,
        },
    )

    assert checker.verify_closure(
        tmp_path, platform="linux-64", wheel=tmp_path / "candidate.whl"
    ) == ("wheel.file_bytes", "wheel.file_count")


@pytest.mark.parametrize("mutation", ["missing", "unexpected", "duplicate"])
def test_source_group_set_must_match_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    source = _source_summary()
    groups = cast(list[dict[str, object]], source["groups"])
    if mutation == "missing":
        groups.pop()
    elif mutation == "unexpected":
        groups[0] = {**groups[0], "name": "private.extra"}
    else:
        groups.append(copy.deepcopy(groups[0]))
    monkeypatch.setattr(collector, "source_metrics", lambda _root: source)
    monkeypatch.setattr(
        collector,
        "locked_dependencies",
        lambda _root, platform: _lock_summary(platform),
    )

    with pytest.raises(
        ValueError,
        match=r"^Documents closure policy or measurement input is invalid\.$",
    ):
        checker.verify_closure(tmp_path, platform="linux-64")


@pytest.mark.parametrize("mutation", ["missing", "unexpected", "duplicate"])
def test_lock_context_set_must_match_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    _install_synthetic_metrics(monkeypatch)
    locks = _lock_summary("linux-64")
    contexts = cast(list[dict[str, object]], locks["contexts"])
    if mutation == "missing":
        contexts.pop()
    elif mutation == "unexpected":
        contexts[0] = {**contexts[0], "context": "private:context"}
    else:
        contexts.append(copy.deepcopy(contexts[0]))
    monkeypatch.setattr(
        collector, "locked_dependencies", lambda _root, _platform: locks
    )

    with pytest.raises(
        ValueError,
        match=r"^Documents closure policy or measurement input is invalid\.$",
    ):
        checker.verify_closure(tmp_path, platform="linux-64")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda policy: policy.update(extra="rejected"),
        lambda policy: policy.update(schema_version=True),
        lambda policy: policy.update(schema_version=3),
        lambda policy: policy["source"]["groups"]["carlina"].update(member_count=True),
        lambda policy: policy["source"]["groups"]["carlina"].update(member_count="52"),
        lambda policy: policy["source"]["groups"]["carlina"].update(bytes=-1),
        lambda policy: policy["locks"]["platforms"].update(macos={}),
        lambda policy: policy["locks"]["platforms"]["win-64"].clear(),
        lambda policy: policy.update(host_retention={}),
        lambda policy: policy["host_retention"].update(
            {"Bad-name": {"reason": "valid", "members": ["carlina/a.py"]}}
        ),
        lambda policy: next(iter(policy["host_retention"].values())).update(
            reason="  "
        ),
        lambda policy: next(iter(policy["host_retention"].values())).update(
            reason="x" * 801
        ),
        lambda policy: next(iter(policy["host_retention"].values())).update(reason=17),
        lambda policy: next(iter(policy["host_retention"].values())).update(
            extra="rejected"
        ),
        lambda policy: next(iter(policy["host_retention"].values())).update(members=[]),
        lambda policy: next(iter(policy["host_retention"].values()))["members"].append(
            next(iter(policy["host_retention"].values()))["members"][0]
        ),
        lambda policy: policy["host_retention"].update(
            retention_duplicate={
                "reason": "duplicate classification",
                "members": [
                    next(iter(policy["host_retention"].values()))["members"][0]
                ],
            }
        ),
        lambda policy: next(iter(policy["host_retention"].values())).update(
            members=["iarf_assistant/addons/doc_processor/worker.py"]
        ),
        lambda policy: next(iter(policy["host_retention"].values())).update(
            members=["carlina/*.py"]
        ),
        lambda policy: next(iter(policy["host_retention"].values())).update(
            members=["carlina/../escape.py"]
        ),
        lambda policy: next(iter(policy["host_retention"].values())).update(
            members=["/carlina/absolute.py"]
        ),
        lambda policy: next(iter(policy["host_retention"].values())).update(
            members=["carlina\\windows.py"]
        ),
        lambda policy: next(iter(policy["host_retention"].values())).update(
            members=["carlina/control\n.py"]
        ),
        lambda policy: next(iter(policy["host_retention"].values())).update(
            members=["carlina//double-slash.py"]
        ),
    ],
    ids=[
        "unknown-field",
        "boolean-version",
        "unsupported-version",
        "boolean-cap",
        "string-cap",
        "negative-cap",
        "unknown-platform",
        "empty-contexts",
        "empty-host-retention",
        "invalid-retention-name",
        "empty-retention-reason",
        "long-retention-reason",
        "non-string-retention-reason",
        "unknown-retention-field",
        "empty-retention-members",
        "duplicate-retention-member",
        "cross-group-duplicate-retention-member",
        "addon-retention-member",
        "glob-retention-member",
        "traversal-retention-member",
        "absolute-retention-member",
        "backslash-retention-member",
        "control-retention-member",
        "unnormalized-retention-member",
    ],
)
def test_malformed_policy_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: Callable[[dict[str, Any]], object],
) -> None:
    policy: dict[str, Any] = json.loads(checker.POLICY_PATH.read_text(encoding="utf-8"))
    mutate(policy)
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setattr(checker, "POLICY_PATH", policy_path)

    with pytest.raises(
        ValueError,
        match=r"^Documents closure policy or measurement input is invalid\.$",
    ):
        checker._load_policy()


def test_duplicate_policy_keys_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_path = tmp_path / "policy.json"
    policy_path.write_text('{"schema_version":2,"schema_version":2}', encoding="utf-8")
    monkeypatch.setattr(checker, "POLICY_PATH", policy_path)

    with pytest.raises(
        ValueError,
        match=r"^Documents closure policy or measurement input is invalid\.$",
    ):
        checker._load_policy()


def test_cli_budget_failure_and_invalid_input_are_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_synthetic_metrics(monkeypatch)
    source = _source_summary()
    groups = cast(list[dict[str, object]], source["groups"])
    group_name = cast(str, groups[0]["name"])
    groups[0]["bytes"] = checker._load_policy().source[group_name]["bytes"] + 1
    monkeypatch.setattr(collector, "source_metrics", lambda _root: source)

    assert checker.main(["--root", str(tmp_path)]) == 1
    failure = capsys.readouterr().err
    assert failure == (
        "Documents closure budget check failed: 1 violation(s).\nsource.carlina.bytes\n"
    )
    assert str(tmp_path) not in failure

    policy_path = tmp_path / "bad-policy.json"
    secret = "untrusted-policy-content"
    policy_path.write_text(
        json.dumps({"schema_version": 1, "secret": secret}), encoding="utf-8"
    )
    monkeypatch.setattr(checker, "POLICY_PATH", policy_path)
    assert checker.main(["--root", str(tmp_path)]) == 2
    error = capsys.readouterr().err
    assert error == "Documents closure policy or measurement input is invalid.\n"
    assert str(tmp_path) not in error
    assert secret not in error
