"""Explicit, owner-scoped publication and adoption of advisory terminology."""

from __future__ import annotations

import copy
import unicodedata
import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import update

from ...database_utils import run_transaction_with_retry
from ...extensions import db
from .analysis_contract import private_witness, private_witness_candidates
from .block_contract import BlockContractError, canonical_json
from .block_persistence import require_active_source_plan
from .block_workspace_api import _load_glossary_source_plan
from .glossary_contract import GlossaryTermInput
from .models import (
    TranslationProject,
    TranslationProjectGlossaryBinding,
    TranslationSharedGlossary,
    TranslationSharedGlossaryProposal,
    TranslationSharedGlossaryReceipt,
    TranslationSharedGlossaryRevision,
)
from .operations import find_active_operation
from .working_glossary import propose_glossary, snapshot_current_glossary_entries

CONTRACT = "translator-shared-glossary-v1"
MAX_TERMS = 2000
MAX_BYTES = 2 * 1024 * 1024
LEXICAL = ("source_term", "target_term", "sense", "notes", "match_policy", "source_forms", "accepted_target_forms")


class SharedGlossaryError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise SharedGlossaryError(code)


def _integer(value: Any, code: str, *, low: int = 0, high: int = 2_147_483_647) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        _fail(code)
    return value


def _text(value: Any, code: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value.encode("utf-8")) > maximum:
        _fail(code)
    return value


def _ids(values: Any, code: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(values, list) or len(values) > MAX_TERMS or (not allow_empty and not values):
        _fail(code)
    result = [_text(value, code, 128) for value in values]
    if len(set(result)) != len(result):
        _fail(code)
    return result


def _page(offset: int, limit: int) -> tuple[int, int]:
    return _integer(offset, "shared-glossary-offset"), _integer(limit, "shared-glossary-limit", low=1, high=100)


def _bounded(value: Any) -> Any:
    if len(canonical_json(value).encode("utf-8")) > MAX_BYTES:
        _fail("shared-glossary-payload-too-large")
    return value


def _normal(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _key(term: dict[str, Any]) -> tuple[str, str]:
    return _normal(term["source_term"]), _normal(term.get("sense", ""))


def _origin_local(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("concept_id"), str):
        return value["concept_id"]
    return None


def _origin_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {"concept_id": entry["concept_id"], "revision_id": entry["revision_id"],
            "revision": entry["revision"]}


def _lexical(raw: dict[str, Any]) -> dict[str, Any]:
    value = {field: copy.deepcopy(raw.get(field)) for field in LEXICAL}
    value["sense"] = value["sense"] or ""
    value["match_policy"] = value["match_policy"] or "preferred"
    value["source_forms"] = value["source_forms"] or [value["source_term"]]
    value["accepted_target_forms"] = value["accepted_target_forms"] or []
    try:
        parsed = GlossaryTermInput.model_validate({**value, "occurrences": []})
    except (ValidationError, TypeError, ValueError) as exc:
        raise SharedGlossaryError("shared-glossary-term-invalid") from exc
    return {field: copy.deepcopy(getattr(parsed, field)) for field in LEXICAL}


def _project(owner: int, project_id: str) -> tuple[TranslationProject, Any]:
    _integer(owner, "shared-glossary-owner", low=1)
    _text(project_id, "shared-glossary-project-id", 36)
    project = db.session.get(TranslationProject, project_id)
    if project is None or project.user_id != owner:
        _fail("shared-glossary-project-not-found")
    try:
        record, _ = _load_glossary_source_plan(project)
    except BlockContractError as exc:
        raise SharedGlossaryError(str(exc)) from exc
    return project, record


def _asset(owner: int, asset_id: str) -> TranslationSharedGlossary:
    _integer(owner, "shared-glossary-owner", low=1)
    _text(asset_id, "shared-glossary-asset-id", 36)
    asset = db.session.get(TranslationSharedGlossary, asset_id)
    if asset is None or asset.owner_user_id != owner:
        _fail("shared-glossary-asset-not-found")
    _verify_asset(asset)
    return asset


def _verify_asset(asset: TranslationSharedGlossary) -> None:
    witness = {"owner": asset.owner_user_id, "asset": asset.id, "metadata": asset.metadata_json}
    if asset.metadata_receipt not in private_witness_candidates(
        witness, purpose="translator-shared-glossary-metadata-v1"
    ):
        _fail("shared-glossary-metadata-corrupt")


def _binding_witness(binding: TranslationProjectGlossaryBinding) -> dict[str, Any]:
    return {"owner": binding.owner_user_id, "project": binding.project_id,
            "version": binding.version, "source_plan": binding.source_plan_id,
            "asset": binding.asset_id, "adopted_revision": binding.adopted_revision,
            "payload": binding.payload_json}


def _verify_binding(binding: TranslationProjectGlossaryBinding) -> None:
    if binding.binding_receipt not in private_witness_candidates(
        _binding_witness(binding), purpose="translator-shared-glossary-binding-v1"
    ):
        _fail("shared-glossary-binding-corrupt")


def _revision(asset: TranslationSharedGlossary, revision: int) -> tuple[TranslationSharedGlossaryRevision, list[dict[str, Any]]]:
    _integer(revision, "shared-glossary-revision", low=1)
    row = TranslationSharedGlossaryRevision.query.filter_by(asset_id=asset.id, revision=revision).one_or_none()
    if row is None or row.owner_user_id != asset.owner_user_id:
        _fail("shared-glossary-revision-not-found")
    payload = row.payload_json
    witness = {"owner": asset.owner_user_id, "asset": asset.id, "revision": revision, "payload": payload}
    if row.receipt not in private_witness_candidates(witness, purpose="translator-shared-glossary-revision-v1"):
        _fail("shared-glossary-revision-corrupt")
    terms = payload.get("terms") if isinstance(payload, dict) else None
    if not isinstance(terms, list) or len(terms) > MAX_TERMS:
        _fail("shared-glossary-revision-corrupt")
    _bounded(payload)
    ids = set()
    keys = set()
    for term in terms:
        if not isinstance(term, dict) or not isinstance(term.get("id"), str):
            _fail("shared-glossary-revision-corrupt")
        lexical = _lexical(term)
        if term["id"] in ids or _key(lexical) in keys:
            _fail("shared-glossary-revision-corrupt")
        ids.add(term["id"])
        keys.add(_key(lexical))
    return row, terms


def _write_revision(asset: TranslationSharedGlossary, revision: int, terms: list[dict[str, Any]]) -> None:
    _bounded({"terms": terms})
    if len(terms) > MAX_TERMS or len({_key(term) for term in terms}) != len(terms):
        _fail("shared-glossary-duplicate-or-limit")
    payload = {"terms": terms}
    db.session.add(TranslationSharedGlossaryRevision(
        id=str(uuid.uuid4()), asset_id=asset.id, owner_user_id=asset.owner_user_id,
        revision=revision, payload_json=payload,
        receipt=private_witness({"owner": asset.owner_user_id, "asset": asset.id,
                                 "revision": revision, "payload": payload},
                                purpose="translator-shared-glossary-revision-v1"),
    ))


def _entries(record: Any) -> list[dict[str, Any]]:
    entries = snapshot_current_glossary_entries(record)
    if len(entries) > MAX_TERMS:
        _fail("shared-glossary-local-term-limit")
    return entries


def _binding(project: TranslationProject, record: Any, *, allow_stale: bool = False) -> TranslationProjectGlossaryBinding | None:
    binding = db.session.get(TranslationProjectGlossaryBinding, project.id)
    if binding is not None:
        if binding.owner_user_id != project.user_id:
            _fail("shared-glossary-binding-corrupt")
        _verify_binding(binding)
        if binding.source_plan_id != record.id and not allow_stale:
            _fail("shared-glossary-source-plan-conflict")
    return binding


def _idle(project: TranslationProject, record: Any) -> None:
    _plan_fence(project, record)
    if find_active_operation(project.id) is not None:
        _fail("shared-glossary-project-busy")


def _plan_fence(project: TranslationProject, record: Any) -> None:
    try:
        require_active_source_plan(project.id, record.id, lock=True)
    except BlockContractError as exc:
        raise SharedGlossaryError("shared-glossary-source-plan-conflict") from exc


def _lang(project: TranslationProject, asset: TranslationSharedGlossary) -> None:
    meta = asset.metadata_json
    if (project.source_lang.strip().casefold(), project.target_lang.strip().casefold()) != (
        meta["source_lang"].strip().casefold(), meta["target_lang"].strip().casefold()
    ):
        _fail("shared-glossary-language-mismatch")


def _pagination(total: int, offset: int, limit: int) -> dict[str, int | bool | None]:
    more = offset + limit < total
    return {"offset": offset, "limit": limit, "total": total, "has_more": more,
            "next_offset": offset + limit if more else None}


def _summary(asset: TranslationSharedGlossary, term_count: int) -> dict[str, Any]:
    return {"id": asset.id, "name": asset.metadata_json["name"],
            "source_lang": asset.metadata_json["source_lang"],
            "target_lang": asset.metadata_json["target_lang"],
            "revision": asset.current_revision, "term_count": term_count}


def _binding_view(binding: TranslationProjectGlossaryBinding | None,
                  owner: int, current_plan_id: str) -> dict[str, Any] | None:
    if binding is None or binding.asset_id is None:
        return None
    asset = _asset(owner, binding.asset_id)
    return {"asset_id": asset.id, "asset_name": asset.metadata_json["name"],
            "adopted_revision": binding.adopted_revision,
            "latest_revision": asset.current_revision, "version": binding.version,
            "source_plan_id": binding.source_plan_id,
            "stale_source_plan": binding.source_plan_id != current_plan_id}


def _receipt(owner: int, action: str, key: str, request: dict[str, Any]) -> dict[str, Any] | None:
    _text(key, "shared-glossary-idempotency-key", 160)
    old = TranslationSharedGlossaryReceipt.query.filter_by(owner_user_id=owner, action=action, retry_key=key).one_or_none()
    if old is None:
        return None
    payload = old.payload_json
    identity = {"owner": old.owner_user_id, "action": old.action, "key": old.retry_key, "payload": payload}
    if old.integrity_receipt not in private_witness_candidates(
        identity, purpose="translator-shared-glossary-idempotency-v1"
    ):
        _fail("shared-glossary-receipt-corrupt")
    if payload.get("request_witness") not in private_witness_candidates(request, purpose="translator-shared-glossary-request-v1"):
        _fail("shared-glossary-idempotency-conflict")
    return copy.deepcopy(payload["result"])


def _save_receipt(owner: int, action: str, key: str, request: dict[str, Any], result: dict[str, Any]) -> None:
    payload = {"request_witness": private_witness(request, purpose="translator-shared-glossary-request-v1"),
               "result": _bounded(result)}
    db.session.add(TranslationSharedGlossaryReceipt(
        id=str(uuid.uuid4()), owner_user_id=owner, action=action, retry_key=key,
        payload_json=payload,
        integrity_receipt=private_witness({"owner": owner, "action": action, "key": key,
            "payload": payload}, purpose="translator-shared-glossary-idempotency-v1"),
    ))


def _transaction(name: str, fn: Any) -> Any:
    return run_transaction_with_retry(db.session, lambda _session: fn(), operation_name=f"shared-glossary-{name}")


def list_assets(owner_user_id: int, *, offset: int = 0, limit: int = 50) -> dict[str, Any]:
    _integer(owner_user_id, "shared-glossary-owner", low=1)
    offset, limit = _page(offset, limit)
    assets = TranslationSharedGlossary.query.filter_by(owner_user_id=owner_user_id).order_by(
        TranslationSharedGlossary.created_at.desc(), TranslationSharedGlossary.id
    ).all()
    items = []
    for asset in assets[offset:offset + limit]:
        _verify_asset(asset)
        items.append(_summary(asset, len(_revision(asset, asset.current_revision)[1])))
    return {"contract": CONTRACT, "items": items, "pagination": _pagination(len(assets), offset, limit)}


def get_asset(owner_user_id: int, *, asset_id: str, revision: int | None = None,
              offset: int = 0, limit: int = 50, proposal_offset: int = 0,
              proposal_limit: int = 10) -> dict[str, Any]:
    offset, limit = _page(offset, limit)
    proposal_offset, proposal_limit = _page(proposal_offset, proposal_limit)
    asset = _asset(owner_user_id, asset_id)
    number = asset.current_revision if revision is None else _integer(revision, "shared-glossary-revision", low=1)
    _, terms = _revision(asset, number)
    pending_query = TranslationSharedGlossaryProposal.query.filter_by(
        asset_id=asset.id, owner_user_id=owner_user_id, state="pending")
    proposal_total = pending_query.count()
    pending = pending_query.order_by(
        TranslationSharedGlossaryProposal.created_at, TranslationSharedGlossaryProposal.id
    ).offset(proposal_offset).limit(proposal_limit).all()
    proposals = [_proposal_view(row) for row in pending]
    return _bounded({"contract": CONTRACT, **_summary(asset, len(terms)), "revision": number,
            "items": copy.deepcopy(terms[offset:offset + limit]),
            "pagination": _pagination(len(terms), offset, limit), "proposals": proposals,
            "proposal_pagination": _pagination(proposal_total, proposal_offset, proposal_limit)})


def _proposal_view(row: TranslationSharedGlossaryProposal) -> dict[str, Any]:
    payload = row.payload_json
    witness = {"owner": row.owner_user_id, "asset": row.asset_id, "proposal": row.id,
               "base": row.base_revision, "payload": payload}
    if row.receipt not in private_witness_candidates(witness, purpose="translator-shared-glossary-proposal-v1"):
        _fail("shared-glossary-proposal-corrupt")
    return {"id": row.id, "base_revision": row.base_revision, "state": row.state,
            "reason": payload["reason"], "changes": copy.deepcopy(payload["changes"])}


def create_asset(owner_user_id: int, *, project_id: str, name: str,
                 concept_ids: list[str], idempotency_key: str) -> dict[str, Any]:
    name = _text(name, "shared-glossary-name", 255)
    selected = _ids(concept_ids, "shared-glossary-concept-ids")
    request = {"project_id": project_id, "name": name, "concept_ids": selected}
    _bounded(request)

    def write() -> dict[str, Any]:
        old = _receipt(owner_user_id, "create", idempotency_key, request)
        if old is not None:
            return old
        project, record = _project(owner_user_id, project_id)
        _plan_fence(project, record)
        old = _receipt(owner_user_id, "create", idempotency_key, request)
        if old is not None:
            return old
        binding = _binding(project, record)
        current = {item["concept_id"]: item for item in _entries(record)}
        if any(cid not in current for cid in selected):
            _fail("shared-glossary-concept-not-current")
        metadata = {"name": name, "source_lang": project.source_lang,
                    "target_lang": project.target_lang}
        asset_id = str(uuid.uuid4())
        asset = TranslationSharedGlossary(id=asset_id, owner_user_id=owner_user_id,
            current_revision=1, metadata_json=metadata,
            metadata_receipt=private_witness({"owner": owner_user_id, "asset": asset_id,
                "metadata": metadata}, purpose="translator-shared-glossary-metadata-v1"))
        db.session.add(asset)
        db.session.flush()
        terms = []
        origin = {}
        for cid in selected:
            term_id = str(uuid.uuid4())
            terms.append({"id": term_id, **_lexical(current[cid]),
                          "origin": {"project_id": project.id, **_origin_entry(current[cid])}})
            origin[term_id] = _origin_entry(current[cid])
        _write_revision(asset, 1, terms)
        payload = copy.deepcopy(binding.payload_json) if binding is not None else {"origins": {}, "links": {}, "base": {}}
        payload.setdefault("origins", {})[asset.id] = origin
        _bounded(payload)
        _binding_write(project, record, binding,
            asset_id=binding.asset_id if binding else None,
            adopted_revision=binding.adopted_revision if binding else None, payload=payload)
        result = {"contract": CONTRACT, **_summary(asset, len(terms)), "authority": "advisory"}
        _save_receipt(owner_user_id, "create", idempotency_key, request, result)
        return result

    return _transaction("create", write)


def project_state(owner_user_id: int, *, project_id: str, offset: int = 0,
                  limit: int = 100) -> dict[str, Any]:
    offset, limit = _page(offset, limit)
    project, record = _project(owner_user_id, project_id)
    binding = _binding(project, record, allow_stale=True)
    payload = binding.payload_json if binding is not None else {}
    origins = payload.get("origins", {})
    reverse = {_origin_local(local): shared for mapping in origins.values()
               for shared, local in mapping.items()}
    reverse.update({local: shared for shared, local in payload.get("links", {}).items()})
    local = [{"concept_id": entry["concept_id"], "revision_id": entry["revision_id"],
              "revision": entry["revision"],
              "authority": entry["authority"], "asset_concept_id": reverse.get(entry["concept_id"]),
              **_lexical(entry)} for entry in _entries(record)]
    linked = _binding_view(binding, owner_user_id, record.id)
    return {"contract": CONTRACT, "project_id": project.id, "source_plan_id": record.id,
            "binding": linked, "binding_version": binding.version if binding else 0,
            "stale_source_plan": binding is not None and binding.source_plan_id != record.id,
            "local_terms": local[offset:offset + limit], "pagination": _pagination(len(local), offset, limit),
            "authority": "model_advisory", "applies_to": "next_operation"}


def _preview(owner: int, project_id: str, asset_id: str, revision: int,
             mode: str) -> dict[str, Any]:
    if mode not in ("link", "copy", "update"):
        _fail("shared-glossary-mode")
    project, record = _project(owner, project_id)
    asset = _asset(owner, asset_id)
    _lang(project, asset)
    binding = _binding(project, record)
    if mode == "update" and (binding is None or binding.asset_id != asset.id):
        _fail("shared-glossary-link-required")
    if mode == "link" and binding is not None and binding.asset_id not in (None, asset.id):
        _fail("shared-glossary-detach-required")
    _, incoming = _revision(asset, revision)
    local_entries = _entries(record)
    by_id = {item["concept_id"]: item for item in local_entries}
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in local_entries:
        by_key.setdefault(_key(item), []).append(item)
    payload = binding.payload_json if binding is not None else {}
    mapped = {key: _origin_local(value) for key, value in
              payload.get("origins", {}).get(asset.id, {}).items()}
    if binding is not None and binding.asset_id == asset.id:
        mapped.update(payload.get("links", {}))
    base = payload.get("base", {}) if binding is not None and binding.asset_id == asset.id else {}
    rows = []
    for term in incoming:
        term_id = term["id"]
        lexical = _lexical(term)
        local = by_id.get(mapped.get(term_id, ""))
        match = "mapped" if local is not None else None
        candidates = by_key.get(_key(lexical), []) if local is None else []
        if len(candidates) == 1:
            local, match = candidates[0], "unique_lemma_sense_candidate"
        elif len(candidates) > 1:
            match = "ambiguous_lemma_sense"
        local_lexical = _lexical(local) if local is not None else None
        old_base = base.get(term_id)
        locked_change = local is not None and local["authority"] == "human_locked" and local_lexical != lexical
        if match == "ambiguous_lemma_sense":
            status = "conflict"
        elif local is None:
            status = "local" if old_base is not None and lexical == old_base else "add"
        elif local_lexical == lexical:
            status = "unchanged"
        elif old_base is not None and local_lexical != old_base and lexical != old_base:
            status = "conflict"
        elif old_base is not None and lexical == old_base:
            status = "local"
        else:
            status = "update"
        rows.append({"id": term_id, "status": status, "match": match,
                     "suppressed": local is None and old_base is not None and lexical == old_base,
                     "base": copy.deepcopy(old_base),
                     "local": None if local is None else {"concept_id": local["concept_id"],
                             "revision_id": local["revision_id"], "revision": local["revision"],
                             "authority": local["authority"], **local_lexical},
                     "incoming": lexical, "can_take_incoming": not locked_change and match != "ambiguous_lemma_sense"})
    for term_id, old_base in base.items():
        if term_id not in {term["id"] for term in incoming}:
            local = by_id.get(mapped.get(term_id, ""))
            rows.append({"id": term_id, "status": "retired", "match": "mapped" if local else None,
                         "suppressed": False,
                         "base": copy.deepcopy(old_base),
                         "local": None if local is None else {"concept_id": local["concept_id"],
                                 "revision_id": local["revision_id"], "revision": local["revision"],
                                 "authority": local["authority"], **_lexical(local)},
                         "incoming": None, "can_take_incoming": False})
    if len(rows) > MAX_TERMS:
        _fail("shared-glossary-term-limit")
    evidence = {"owner": owner, "project": project.id, "source_plan": record.id,
                "asset": asset.id, "revision": revision, "head": asset.current_revision,
                "mode": mode, "binding_version": binding.version if binding else 0,
                "local": [{"id": item["concept_id"], "revision": item["revision"],
                           "authority": item["authority"], "lexical": _lexical(item)} for item in local_entries],
                "rows": rows}
    _bounded(evidence)
    return {"contract": CONTRACT, "project_id": project.id, "source_plan_id": record.id,
            "asset_id": asset.id, "revision": revision, "latest_revision": asset.current_revision,
            "mode": mode, "preview_token": private_witness(evidence, purpose="translator-shared-glossary-preview-v1"),
            "binding_version": binding.version if binding else 0, "rows": rows,
            "authority": "model_advisory", "applies_to": "next_operation"}


def preview_import(owner_user_id: int, *, project_id: str, asset_id: str,
                   revision: int, mode: str) -> dict[str, Any]:
    return _preview(owner_user_id, project_id, asset_id, revision, mode)


def _binding_write(project: TranslationProject, record: Any,
                   binding: TranslationProjectGlossaryBinding | None,
                   *, asset_id: str | None, adopted_revision: int | None,
                   payload: dict[str, Any]) -> TranslationProjectGlossaryBinding:
    _bounded(payload)
    if binding is None:
        binding = TranslationProjectGlossaryBinding(
            project_id=project.id, owner_user_id=project.user_id, source_plan_id=record.id,
            asset_id=asset_id, adopted_revision=adopted_revision, version=1, payload_json=payload)
        binding.binding_receipt = private_witness(_binding_witness(binding),
            purpose="translator-shared-glossary-binding-v1")
        db.session.add(binding)
    else:
        expected = binding.version
        next_witness = {"owner": binding.owner_user_id, "project": project.id,
            "version": expected + 1, "source_plan": record.id,
            "asset": asset_id, "adopted_revision": adopted_revision, "payload": payload}
        next_receipt = private_witness(next_witness, purpose="translator-shared-glossary-binding-v1")
        changed = db.session.execute(update(TranslationProjectGlossaryBinding).where(
            TranslationProjectGlossaryBinding.project_id == project.id,
            TranslationProjectGlossaryBinding.version == expected,
            TranslationProjectGlossaryBinding.source_plan_id == binding.source_plan_id,
        ).values(version=expected + 1, asset_id=asset_id,
                 source_plan_id=record.id, adopted_revision=adopted_revision,
                 payload_json=payload, binding_receipt=next_receipt)).rowcount
        if changed != 1:
            _fail("shared-glossary-binding-conflict")
        binding.version = expected + 1
        binding.asset_id = asset_id
        binding.adopted_revision = adopted_revision
        binding.source_plan_id = record.id
        binding.payload_json = payload
        binding.binding_receipt = next_receipt
    return binding


def apply_import(owner_user_id: int, *, project_id: str, asset_id: str,
                 revision: int, mode: str, preview_token: str,
                 decisions: list[dict[str, str]], idempotency_key: str) -> dict[str, Any]:
    _text(preview_token, "shared-glossary-preview-token", 64)
    if not isinstance(decisions, list) or len(decisions) > MAX_TERMS:
        _fail("shared-glossary-decisions")
    selected: dict[str, str] = {}
    for decision in decisions:
        if not isinstance(decision, dict) or set(decision) != {"id", "choice"}:
            _fail("shared-glossary-decisions")
        concept = _text(decision["id"], "shared-glossary-decisions", 128)
        if concept in selected or decision["choice"] not in ("incoming", "local"):
            _fail("shared-glossary-decisions")
        selected[concept] = decision["choice"]
    request = {"project_id": project_id, "asset_id": asset_id, "revision": revision,
               "mode": mode, "preview_token": preview_token, "decisions": decisions}
    _bounded(request)

    def write() -> dict[str, Any]:
        old = _receipt(owner_user_id, "apply", idempotency_key, request)
        if old is not None:
            return old
        project, record = _project(owner_user_id, project_id)
        _idle(project, record)
        old = _receipt(owner_user_id, "apply", idempotency_key, request)
        if old is not None:
            return old
        preview = _preview(owner_user_id, project_id, asset_id, revision, mode)
        if preview["preview_token"] != preview_token:
            _fail("shared-glossary-preview-stale")
        asset = _asset(owner_user_id, asset_id)
        binding = _binding(project, record)
        rows = preview["rows"]
        row_by_id = {row["id"]: row for row in rows}
        if set(selected) - set(row_by_id):
            _fail("shared-glossary-decisions")
        if mode in ("link", "update") and any(
            row["status"] != "unchanged" and row["id"] not in selected for row in rows
        ):
            _fail("shared-glossary-decisions-required")
        if mode == "copy" and not any(choice == "incoming" for choice in selected.values()):
            _fail("shared-glossary-copy-empty")
        if any(selected.get(row["id"]) == "incoming" and not row["can_take_incoming"] for row in rows):
            _fail("shared-glossary-incoming-forbidden")
        payload = copy.deepcopy(binding.payload_json) if binding is not None else {"origins": {}, "links": {}, "base": {}}
        origins = payload.setdefault("origins", {}).setdefault(asset.id, {})
        links = dict(payload.get("links", {})) if mode != "copy" else {}
        base = {} if mode != "copy" else payload.get("base", {})
        to_write = []
        queued = []
        kept = 0
        imported = 0
        for row in rows:
            choice = selected.get(row["id"], "local")
            local = row["local"]
            incoming = row["incoming"]
            if mode != "copy" and incoming is not None:
                base[row["id"]] = copy.deepcopy(incoming)
            if incoming is None or choice == "local":
                kept += 1
                if local is not None and mode != "copy":
                    origins[row["id"]] = _origin_entry(local)
                    links[row["id"]] = local["concept_id"]
                continue
            if local is not None and _lexical(local) == incoming:
                origins[row["id"]] = _origin_entry(local)
                if mode != "copy":
                    links[row["id"]] = local["concept_id"]
                kept += 1
                continue
            item = {**incoming, "occurrences": []}
            if local is not None:
                item.update(concept_id=local["concept_id"], expected_revision=local["revision"])
            to_write.append(item)
            queued.append(row["id"])
        for index in range(0, len(to_write), 32):
            chunk = to_write[index:index + 32]
            inner_key = "shared:" + str(uuid.uuid5(uuid.NAMESPACE_URL,
                f"{project.id}:{asset.id}:{idempotency_key}:{index // 32}"))
            try:
                result = propose_glossary(project, source_plan_id=record.id, entries=chunk,
                    idempotency_key=inner_key, provenance="shared-glossary-import")
            except (BlockContractError, ValidationError) as exc:
                raise SharedGlossaryError(str(exc)) from exc
            for term_id, item in zip(queued[index:index + 32], result["items"], strict=True):
                origins[term_id] = _origin_entry(item)
                if mode != "copy":
                    links[term_id] = item["concept_id"]
                imported += 1
        if mode != "copy":
            payload["links"] = links
            payload["base"] = base
        binding = _binding_write(project, record, binding,
            asset_id=(binding.asset_id if binding else None) if mode == "copy" else asset.id,
            adopted_revision=(binding.adopted_revision if binding else None) if mode == "copy" else revision,
            payload=payload)
        result = {"contract": CONTRACT, "project_id": project.id,
                  "binding": _binding_view(binding, owner_user_id, record.id),
                  "binding_version": binding.version, "imported_count": imported,
                  "kept_count": kept, "authority": "model_advisory",
                  "applies_to": "next_operation", "target_text_changed": False}
        _save_receipt(owner_user_id, "apply", idempotency_key, request, result)
        return result

    return _transaction("apply", write)


def detach_asset(owner_user_id: int, *, project_id: str, expected_version: int,
                 idempotency_key: str) -> dict[str, Any]:
    _integer(expected_version, "shared-glossary-binding-version", low=1)
    request = {"project_id": project_id, "expected_version": expected_version}

    def write() -> dict[str, Any]:
        old = _receipt(owner_user_id, "detach", idempotency_key, request)
        if old is not None:
            return old
        project, record = _project(owner_user_id, project_id)
        _idle(project, record)
        old = _receipt(owner_user_id, "detach", idempotency_key, request)
        if old is not None:
            return old
        binding = _binding(project, record, allow_stale=True)
        if binding is None or binding.version != expected_version or (
            binding.asset_id is None and binding.source_plan_id == record.id
        ):
            _fail("shared-glossary-binding-conflict")
        payload = copy.deepcopy(binding.payload_json)
        payload["links"] = {}
        payload["base"] = {}
        binding = _binding_write(project, record, binding, asset_id=None, adopted_revision=None, payload=payload)
        result = {"contract": CONTRACT, "project_id": project.id, "source_plan_id": record.id,
                  "binding": None, "binding_version": binding.version,
                  "authority": "model_advisory", "applies_to": "next_operation"}
        _save_receipt(owner_user_id, "detach", idempotency_key, request, result)
        return result

    return _transaction("detach", write)


def propose_contribution(owner_user_id: int, *, project_id: str, asset_id: str,
                         expected_revision: int, concept_ids: list[str],
                         retire_ids: list[str] | None = None, reason: str,
                         idempotency_key: str) -> dict[str, Any]:
    selected = _ids(concept_ids, "shared-glossary-concept-ids")
    retired = _ids(retire_ids or [], "shared-glossary-retire-ids")
    if len(selected) + len(retired) > MAX_TERMS or not selected and not retired:
        _fail("shared-glossary-selection-limit")
    reason = _text(reason, "shared-glossary-reason", 8192)
    _integer(expected_revision, "shared-glossary-revision", low=1)
    request = {"project_id": project_id, "asset_id": asset_id, "expected_revision": expected_revision,
               "concept_ids": selected, "retire_ids": retired, "reason": reason}
    _bounded(request)

    def write() -> dict[str, Any]:
        old = _receipt(owner_user_id, "propose", idempotency_key, request)
        if old is not None:
            return old
        project, record = _project(owner_user_id, project_id)
        _plan_fence(project, record)
        old = _receipt(owner_user_id, "propose", idempotency_key, request)
        if old is not None:
            return old
        asset = _asset(owner_user_id, asset_id)
        _lang(project, asset)
        if asset.current_revision != expected_revision:
            _fail("shared-glossary-revision-stale")
        _, shared = _revision(asset, expected_revision)
        shared_by_id = {item["id"]: item for item in shared}
        shared_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in shared:
            shared_by_key.setdefault(_key(item), []).append(item)
        binding = _binding(project, record)
        payload = binding.payload_json if binding is not None else {}
        mapping = {key: _origin_local(value) for key, value in
                   payload.get("origins", {}).get(asset.id, {}).items()}
        if binding is not None and binding.asset_id == asset.id:
            mapping.update(payload.get("links", {}))
        reverse = {local: asset_concept for asset_concept, local in mapping.items()}
        local_by_id = {item["concept_id"]: item for item in _entries(record)}
        if any(cid not in local_by_id for cid in selected):
            _fail("shared-glossary-concept-not-current")
        changes = []
        used = set()
        for cid in selected:
            local = local_by_id[cid]
            after = _lexical(local)
            term_id = reverse.get(cid)
            matched_by = "origin" if term_id is not None else "new"
            if term_id is None:
                candidates = shared_by_key.get(_key(after), [])
                if len(candidates) > 1:
                    _fail("shared-glossary-ambiguous-match")
                if candidates:
                    term_id = candidates[0]["id"]
                    matched_by = "unique_lemma_sense_candidate"
                else:
                    term_id = str(uuid.uuid4())
            if term_id in used:
                _fail("shared-glossary-duplicate-selection")
            used.add(term_id)
            upstream = shared_by_id.get(term_id)
            before = _lexical(upstream) if upstream is not None else None
            adopted = payload.get("base", {}).get(term_id) if binding is not None and binding.asset_id == asset.id else None
            if adopted is not None and before is not None and before != adopted and after != before:
                _fail("shared-glossary-upstream-conflict")
            if before is None and any(_key(item) == _key(after) for item in shared):
                _fail("shared-glossary-duplicate-lemma-sense")
            if before != after:
                changes.append({"id": term_id, "before": before, "after": after,
                                "origin": {"project_id": project.id, **_origin_entry(local),
                                           "matched_by": matched_by}})
        for term_id in retired:
            if term_id in used or term_id not in shared_by_id:
                _fail("shared-glossary-retire-id")
            changes.append({"id": term_id, "before": _lexical(shared_by_id[term_id]),
                            "after": None, "origin": {"project_id": project.id, "retirement": True}})
        if not changes:
            _fail("shared-glossary-no-changes")
        future = {term["id"]: _lexical(term) for term in shared}
        for change in changes:
            if change["after"] is None:
                future.pop(change["id"], None)
            else:
                future[change["id"]] = change["after"]
        if len(future) > MAX_TERMS or len({_key(term) for term in future.values()}) != len(future):
            _fail("shared-glossary-duplicate-lemma-sense")
        _bounded({"reason": reason, "changes": changes})
        proposal_id = str(uuid.uuid4())
        proposal_payload = {"reason": reason, "changes": changes}
        db.session.add(TranslationSharedGlossaryProposal(
            id=proposal_id, asset_id=asset.id, owner_user_id=owner_user_id,
            base_revision=expected_revision, state="pending", payload_json=proposal_payload,
            receipt=private_witness({"owner": owner_user_id, "asset": asset.id,
                "proposal": proposal_id, "base": expected_revision, "payload": proposal_payload},
                purpose="translator-shared-glossary-proposal-v1"),
        ))
        result = {"contract": CONTRACT, "proposal_id": proposal_id, "asset_id": asset.id,
                  "base_revision": expected_revision, "state": "pending",
                  "changes": changes, "authority": "advisory"}
        _save_receipt(owner_user_id, "propose", idempotency_key, request, result)
        return result

    return _transaction("propose", write)


def review_contribution(owner_user_id: int, *, asset_id: str, proposal_id: str,
                        expected_revision: int, decision: str,
                        idempotency_key: str) -> dict[str, Any]:
    if decision not in ("accept", "reject"):
        _fail("shared-glossary-review-decision")
    _integer(expected_revision, "shared-glossary-revision", low=1)
    request = {"asset_id": asset_id, "proposal_id": proposal_id,
               "expected_revision": expected_revision, "decision": decision}

    def write() -> dict[str, Any]:
        old = _receipt(owner_user_id, "review", idempotency_key, request)
        if old is not None:
            return old
        asset = _asset(owner_user_id, asset_id)
        # Serialize review attempts before the second receipt lookup. A peer
        # may have committed the same retry key after our first read.
        db.session.execute(update(TranslationSharedGlossary).where(
            TranslationSharedGlossary.id == asset.id,
            TranslationSharedGlossary.owner_user_id == owner_user_id,
        ).values(current_revision=TranslationSharedGlossary.current_revision))
        old = _receipt(owner_user_id, "review", idempotency_key, request)
        if old is not None:
            return old
        db.session.refresh(asset)
        _text(proposal_id, "shared-glossary-proposal-id", 36)
        proposal = db.session.get(TranslationSharedGlossaryProposal, proposal_id)
        if proposal is None or proposal.asset_id != asset.id or proposal.owner_user_id != owner_user_id:
            _fail("shared-glossary-proposal-not-found")
        view = _proposal_view(proposal)
        if proposal.state != "pending":
            _fail("shared-glossary-proposal-reviewed")
        if asset.current_revision != expected_revision or (decision == "accept" and proposal.base_revision != expected_revision):
            _fail("shared-glossary-revision-stale")
        next_revision = expected_revision
        if decision == "accept":
            _, terms = _revision(asset, expected_revision)
            term_map = {item["id"]: copy.deepcopy(item) for item in terms}
            for change in view["changes"]:
                term_id = change["id"]
                old_term = term_map.get(term_id)
                before = _lexical(old_term) if old_term is not None else None
                if before != change["before"]:
                    _fail("shared-glossary-proposal-conflict")
                if change["after"] is None:
                    term_map.pop(term_id)
                else:
                    term_map[term_id] = {"id": term_id, **_lexical(change["after"]),
                                         "origin": change["origin"]}
            if len(term_map) > MAX_TERMS or len({_key(item) for item in term_map.values()}) != len(term_map):
                _fail("shared-glossary-duplicate-or-limit")
            next_revision = expected_revision + 1
            changed = db.session.execute(update(TranslationSharedGlossary).where(
                TranslationSharedGlossary.id == asset.id,
                TranslationSharedGlossary.owner_user_id == owner_user_id,
                TranslationSharedGlossary.current_revision == expected_revision,
            ).values(current_revision=next_revision)).rowcount
            if changed != 1:
                _fail("shared-glossary-revision-stale")
            _write_revision(asset, next_revision, list(term_map.values()))
        changed = db.session.execute(update(TranslationSharedGlossaryProposal).where(
            TranslationSharedGlossaryProposal.id == proposal.id,
            TranslationSharedGlossaryProposal.state == "pending",
        ).values(state="accepted" if decision == "accept" else "rejected")).rowcount
        if changed != 1:
            _fail("shared-glossary-proposal-reviewed")
        result = {"contract": CONTRACT, "asset_id": asset.id, "proposal_id": proposal.id,
                  "revision": next_revision, "state": "accepted" if decision == "accept" else "rejected",
                  "authority": "advisory"}
        _save_receipt(owner_user_id, "review", idempotency_key, request, result)
        return result

    return _transaction("review", write)
