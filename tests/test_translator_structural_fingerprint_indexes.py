"""Indexed structural-fingerprint validation and lazy scoped memo.

Synthetic source only. Proves the optimisation retains exact digest and
``target-fingerprint-block`` rejection semantics while avoiding the linear
``block in plan.blocks`` scan and eager full-plan fingerprint computation.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from importlib import import_module

import pytest

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION

_ROOT = "iarf" + "_assistant"
_translator = f"{_ROOT}.addons.translator"
_block_contract = import_module(f"{_translator}.block_contract")
_block_planner = import_module(f"{_translator}.block_planner")
_source_indexes = import_module(f"{_translator}.source_indexes")
_target_contract = import_module(f"{_translator}.target_contract")

source_indexes = _source_indexes
target_contract = _target_contract
AttestedDocumentSnapshot = _block_contract.AttestedDocumentSnapshot
BlockContractError = _block_contract.BlockContractError
SourceProviderAttestation = _block_contract.SourceProviderAttestation
TranslationBlock = _block_contract.TranslationBlock
canonical_json = _block_contract.canonical_json
sha256_text = _block_contract.sha256_text
plan_document_blocks = _block_planner.plan_document_blocks
structural_fingerprint = _target_contract.structural_fingerprint


def _synthetic_plan(html: str, *, project_id: str = "synthetic-fingerprint"):
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
    snapshot = AttestedDocumentSnapshot(
        id="doc-1",
        document_id="doc-1",
        output_html=html,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
        captured_at="2026-09-01T00:00:00+00:00",
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )
    attestation = SourceProviderAttestation(
        provider_addon_id="doc-provider",
        provider_capability_id="document-processing",
        owner_id="owner-1",
        resource_id="doc-1",
    )
    return plan_document_blocks(
        snapshot,
        project_id=project_id,
        source_attestation=attestation,
        source_language="en",
    )


def _independent_digest(plan, block) -> str:
    """Recompute the documented payload without calling structural_fingerprint."""
    return sha256_text(
        canonical_json(
            {
                "plan_id": plan.plan_hash,
                "plan_contract": plan.contract_version,
                "snapshot_contract": plan.snapshot.semantic_contract_version,
                "snapshot_hash": plan.snapshot.content_hash,
                "planner_version": plan.planner_version,
                "planner_settings": plan.settings_fingerprint,
                "block_id": block.id,
                "role": block.role,
                "locator": block.locator,
                "slots": [
                    {
                        "id": slot.id,
                        "kind": slot.kind,
                        "disposition": slot.disposition,
                        "policy_code": slot.policy_code,
                        "source_hash": slot.source_hash,
                    }
                    for slot in block.slots
                ],
                "tokens": [
                    {
                        "id": token.id,
                        "kind": token.kind,
                        "tag": token.tag,
                        "locator": token.locator,
                        "movement_policy": token.movement_policy,
                        "element_id": token.element_id,
                        "source_hash": token.source_hash,
                    }
                    for token in block.protected_inline
                ],
                "wire": [part.to_dict() for part in block.wire],
            }
        )
    )


def test_parity_matches_independent_payload_and_equal_clone_accepts():
    plan = _synthetic_plan(
        "<html><body><p>Alpha synthetic.</p><p>Beta synthetic.</p></body></html>"
    )
    assert len(plan.blocks) >= 2
    for block in plan.blocks:
        assert structural_fingerprint(plan, block) == _independent_digest(plan, block)

    canonical = plan.blocks[0]
    assert structural_fingerprint(plan, replace(canonical)) == structural_fingerprint(
        plan, canonical
    )
    clone = type(canonical).from_dict(canonical.to_dict())
    assert clone == canonical
    assert clone is not canonical
    assert structural_fingerprint(plan, clone) == structural_fingerprint(
        plan, canonical
    )
    assert source_indexes._scope.get() is None


def test_same_id_altered_content_rejects_even_when_warm():
    plan = _synthetic_plan(
        "<html><body><p>Alpha synthetic.</p><p>Beta synthetic.</p></body></html>"
    )
    canonical = plan.blocks[0]

    @source_indexes.reuse_source_indexes
    def _warm(*, plan):
        return structural_fingerprint(plan, canonical)

    expected = _warm(plan=plan)
    assert expected == _independent_digest(plan, canonical)

    # Same ID, one altered structural field. Still a valid DTO, but not owned.
    altered = replace(canonical, locator=f"{canonical.locator}-mutated")

    @source_indexes.reuse_source_indexes
    def _warm_then_altered(*, plan):
        assert structural_fingerprint(plan, canonical) == expected
        with pytest.raises(BlockContractError) as excinfo:
            structural_fingerprint(plan, altered)
        assert excinfo.value.code == "target-fingerprint-block"

    _warm_then_altered(plan=plan)

    with pytest.raises(BlockContractError) as excinfo:
        structural_fingerprint(plan, altered)
    assert excinfo.value.code == "target-fingerprint-block"
    # The warm canonical entry still validates afterwards.
    assert structural_fingerprint(plan, canonical) == expected
    assert source_indexes._scope.get() is None


def test_unknown_and_cross_plan_rejected():
    plan = _synthetic_plan(
        "<html><body><p>Alpha synthetic.</p><p>Beta synthetic.</p></body></html>"
    )
    other = _synthetic_plan(
        "<html><body><p>Entirely different synthetic prose.</p></body></html>"
    )
    with pytest.raises(BlockContractError) as excinfo:
        structural_fingerprint(plan, other.blocks[0])
    assert excinfo.value.code == "target-fingerprint-block"

    unknown = replace(plan.blocks[0], id="no-such-synthetic-block")
    with pytest.raises(BlockContractError) as excinfo:
        structural_fingerprint(plan, unknown)
    assert excinfo.value.code == "target-fingerprint-block"

    for invalid in (None, object(), "not-a-block"):
        with pytest.raises(BlockContractError) as excinfo:
            structural_fingerprint(plan, invalid)  # type: ignore[arg-type]
        assert excinfo.value.code == "target-fingerprint-block"
    assert source_indexes._scope.get() is None


def test_lazy_memo_computes_single_block_only_and_reuses(monkeypatch):
    plan = _synthetic_plan(
        "<html><body><p>Alpha synthetic.</p><p>Beta synthetic.</p>"
        "<p>Gamma synthetic.</p></body></html>"
    )
    assert len(plan.blocks) >= 3
    calls = {"canonical_json": 0}
    original = target_contract.canonical_json

    def _counting(value):
        calls["canonical_json"] += 1
        return original(value)

    monkeypatch.setattr(target_contract, "canonical_json", _counting)

    @source_indexes.reuse_source_indexes
    def _scoped(*, plan):
        calls["canonical_json"] = 0
        first = structural_fingerprint(plan, plan.blocks[0])
        after_first = calls["canonical_json"]
        second = structural_fingerprint(plan, plan.blocks[0])
        after_second = calls["canonical_json"]
        third = structural_fingerprint(plan, plan.blocks[1])
        after_third = calls["canonical_json"]
        return first, second, third, after_first, after_second, after_third

    first, second, third, after_first, after_second, after_third = _scoped(plan=plan)
    assert first == second
    assert first == _independent_digest(plan, plan.blocks[0])
    assert third == _independent_digest(plan, plan.blocks[1])
    # One digest build for one requested block (no eager full-plan fan-out),
    # zero rebuilds for the immediate repeat, one more for the next block.
    assert after_first == 1
    assert after_second == 1
    assert after_third == 2
    assert source_indexes._scope.get() is None


def test_nested_scopes_isolate_and_release(monkeypatch):
    plan = _synthetic_plan(
        "<html><body><p>Alpha synthetic.</p><p>Beta synthetic.</p></body></html>"
    )
    twin = replace(plan)
    assert twin is not plan
    assert twin.blocks == plan.blocks

    builds = {"canonical_json": 0}
    original = target_contract.canonical_json

    def _counting(value):
        builds["canonical_json"] += 1
        return original(value)

    monkeypatch.setattr(target_contract, "canonical_json", _counting)

    @source_indexes.reuse_source_indexes
    def _inner_same(*, plan):
        return structural_fingerprint(plan, plan.blocks[0])

    @source_indexes.reuse_source_indexes
    def _inner_twin(*, plan):
        return structural_fingerprint(plan, plan.blocks[0])

    @source_indexes.reuse_source_indexes
    def _outer(*, plan):
        builds["canonical_json"] = 0
        first = structural_fingerprint(plan, plan.blocks[0])
        assert builds["canonical_json"] == 1
        # Same plan object nested: scope is shared, memo hits, no rebuild.
        assert _inner_same(plan=plan) == first
        assert builds["canonical_json"] == 1
        # Distinct plan object nested: separate scope, rebuilds once, same digest.
        assert _inner_twin(plan=twin) == first
        assert builds["canonical_json"] == 2
        # Outer scope still intact after the nested twin scope exits.
        assert structural_fingerprint(plan, plan.blocks[0]) == first
        assert builds["canonical_json"] == 2
        return first

    assert _outer(plan=plan) == _independent_digest(plan, plan.blocks[0])
    assert source_indexes._scope.get() is None

    @source_indexes.reuse_source_indexes
    def _failing(*, plan):
        structural_fingerprint(plan, plan.blocks[0])
        raise RuntimeError("stop-synthetic-assembly")

    with pytest.raises(RuntimeError, match="stop-synthetic-assembly"):
        _failing(plan=plan)
    assert source_indexes._scope.get() is None


def test_unscoped_calls_do_not_retain_global_cache(monkeypatch):
    plan = _synthetic_plan(
        "<html><body><p>Alpha synthetic.</p><p>Beta synthetic.</p></body></html>"
    )
    assert source_indexes._scope.get() is None
    calls = {"canonical_json": 0}
    original = target_contract.canonical_json

    def _counting(value):
        calls["canonical_json"] += 1
        return original(value)

    monkeypatch.setattr(target_contract, "canonical_json", _counting)
    first = structural_fingerprint(plan, plan.blocks[0])
    assert calls["canonical_json"] == 1
    assert source_indexes._scope.get() is None
    second = structural_fingerprint(plan, plan.blocks[0])
    assert first == second
    # Without an outer invocation-local scope there is intentionally no
    # cross-call global memo: the second call rebuilds once.
    assert calls["canonical_json"] == 2
    assert source_indexes._scope.get() is None


def test_membership_scales_without_tuple_eq_scans(monkeypatch):
    count = 70
    html = "<html><body>" + "".join(
        f"<p>Synthetic block {index:03d}.</p>" for index in range(count)
    )
    html += "</body></html>"
    plan = _synthetic_plan(html)
    assert len(plan.blocks) >= count

    original_eq = TranslationBlock.__eq__
    calls = {"eq": 0}

    def _counting_eq(self, other):
        calls["eq"] += 1
        return original_eq(self, other)

    monkeypatch.setattr(TranslationBlock, "__eq__", _counting_eq)

    @source_indexes.reuse_source_indexes
    def _passes(*, plan):
        for _ in range(3):
            for block in plan.blocks:
                structural_fingerprint(plan, block)

    calls["eq"] = 0
    _passes(plan=plan)
    # Canonical plan objects hit the `is` fast path: zero dataclass scans.
    # The original `block in plan.blocks` tuple scan would need O(passes *
    # blocks^2 / 2) comparisons here (thousands for 70 blocks x 3 passes).
    assert calls["eq"] == 0
    assert source_indexes._scope.get() is None

    clone = replace(plan.blocks[0])
    assert clone is not plan.blocks[0]
    assert clone == plan.blocks[0]
    # One indexed guard comparison for one equal clone, not a tuple scan.
    assert calls["eq"] == 1
    assert structural_fingerprint(plan, clone) == structural_fingerprint(
        plan, plan.blocks[0]
    )
    assert calls["eq"] == 2
    assert source_indexes._scope.get() is None
