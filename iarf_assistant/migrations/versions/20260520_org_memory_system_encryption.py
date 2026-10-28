"""Switch OrgMemory encryption strategy to system and prepare lazy migration.

Revision ID: 20260520_org_memory_system_encryption
Revises: 20260519_runtime_source_record_columns
Create Date: 2026-05-20 00:00:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = '20260520_org_memory_system_encryption'
down_revision = '20260519_runtime_source_record_columns'
branch_labels = None
depends_on = None


def upgrade():
    # EncryptedType(strategy=...) is an application-layer concern backed by TEXT columns.
    # No schema mutation is required. Existing legacy org-memory rows are migrated lazily
    # at runtime when decryptable with an active admin session key.
    op.execute("SELECT 1")


def downgrade():
    op.execute("SELECT 1")
