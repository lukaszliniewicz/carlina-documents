"""Retained-key row receipts for Translator's immutable evidence.

Receipts attest to the whole decrypted row and its owner scope.  Public SHA
hashes remain deterministic semantic identifiers; they are not integrity
authority for source-bearing evidence.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .analysis_contract import private_witness, private_witness_candidates
from .block_contract import BlockContractError


def make_receipt(*, purpose: str, values: Mapping[str, Any]) -> str:
    return private_witness(dict(values), purpose=purpose)


def row_receipt_candidates(*, purpose: str, values: Mapping[str, Any]) -> tuple[str, ...]:
    """Return every retained-key receipt accepted for immutable row material."""

    return private_witness_candidates(dict(values), purpose=purpose)


def verify_receipt(*, stored: object, purpose: str, values: Mapping[str, Any], code: str) -> str:
    if not isinstance(stored, str) or stored not in row_receipt_candidates(
        purpose=purpose, values=values
    ):
        raise BlockContractError(code)
    return stored


__all__ = ["make_receipt", "row_receipt_candidates", "verify_receipt"]
