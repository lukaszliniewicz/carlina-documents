"""Bridge the historical and descriptive metadata migration identifiers.

Revision ID: 003_document_metadata_ownership
Revises: 003_doc_metadata_ownership
Create Date: 2026-07-31 12:00:00.000000
"""
from __future__ import annotations

revision = "003_document_metadata_ownership"
down_revision = "003_doc_metadata_ownership"
branch_labels = None
depends_on = None


def upgrade():
    """Preserve compatibility with databases stamped under either identifier."""


def downgrade():
    """The bridge carries no schema changes."""
