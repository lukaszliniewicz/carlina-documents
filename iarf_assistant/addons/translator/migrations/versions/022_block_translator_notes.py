"""Add immutable block notes and retire pre-block Translator projects.

Revision ID: 022_block_translator_notes
Revises: 021_translation_notes
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "022_block_translator_notes"
down_revision = "021_translation_notes"
branch_labels = None
depends_on = None


def _delete_translator_rows(bind: sa.Connection) -> None:
    """Delete the add-on aggregate in FK child-before-parent order.

    Several historical Translator evidence tables intentionally use RESTRICT
    references, so a single project DELETE cannot cascade through a populated
    aggregate.  The user-authorized reset removes every Translator-owned row,
    preserving all table schemas and all non-Translator add-on data.
    """

    inspector = sa.inspect(bind)
    tables = {
        table
        for table in inspector.get_table_names()
        if table.startswith("translation_") and table != "translation_project"
    }
    dependencies: dict[str, set[str]] = {table: set() for table in tables}
    for table in tables:
        for foreign_key in inspector.get_foreign_keys(table):
            referred = foreign_key.get("referred_table")
            if referred in tables and referred != table:
                dependencies[table].add(referred)

    remaining = set(tables)
    while remaining:
        # ``dependencies[child]`` contains its parent tables. Drain graph
        # leaves (tables with no remaining dependants), not roots, so RESTRICT
        # references never see their parent removed first.
        ready = sorted(
            table
            for table in remaining
            if not any(
                table in dependencies[other]
                for other in remaining
                if other != table
            )
        )
        if not ready:
            # The historical schema contains no cross-table FK cycle, but fail
            # closed if a future migration introduces one rather than guessing
            # at a destructive order.
            raise RuntimeError(
                "Translator migration 022 cannot order aggregate cleanup FKs."
            )
        for table in ready:
            quoted = '"' + table.replace('"', '""') + '"'
            # Some isolated migration harnesses intentionally omit unrelated
            # core parent tables referenced by empty Translator projection
            # tables. SQLite can reject DELETE even when such a table is empty,
            # so avoid issuing a meaningless FK check in that case.
            if bind.execute(sa.text(f"SELECT 1 FROM {quoted} LIMIT 1")).first():
                bind.execute(sa.text(f"DELETE FROM {quoted}"))
            remaining.remove(table)


def upgrade() -> None:
    op.create_table(
        "translation_target_block_note",
        # EncryptedType is physically TEXT; the ORM owns envelope handling.
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_plan_id", sa.String(length=36), nullable=False),
        sa.Column("block_id", sa.String(length=128), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("anchor_text", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "author_type", sa.String(length=20), nullable=False, server_default="model"
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("record_hash", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_target_block_note"),
        sa.UniqueConstraint(
            "revision_id",
            "order_index",
            name="uq_translation_target_block_note_revision_order",
        ),
        sa.CheckConstraint(
            "order_index >= 0", name="ck_translation_target_block_note_order"
        ),
        sa.CheckConstraint(
            "author_type = 'model'",
            name="ck_translation_target_block_note_author_type",
        ),
        sa.CheckConstraint(
            "length(record_hash) = 64",
            name="ck_translation_target_block_note_record_hash",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_target_block_note_project",
        ),
        sa.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_target_block_note_source_plan_project",
        ),
        sa.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_target_block_note_block_plan",
        ),
        sa.ForeignKeyConstraint(
            ["revision_id", "source_plan_id", "block_id"],
            [
                "translation_target_block_revision.id",
                "translation_target_block_revision.source_plan_id",
                "translation_target_block_revision.block_id",
            ],
            ondelete="CASCADE",
            name="fk_translation_target_block_note_revision",
        ),
    )
    op.create_index(
        "ix_translation_target_block_note_revision_order",
        "translation_target_block_note",
        ["source_plan_id", "block_id", "revision_id", "order_index"],
        unique=False,
    )

    # Every existing Translator project is intentionally disposable.  Remove
    # the complete add-on aggregate while leaving source documents and Subtitle
    # Studio tables outside this add-on untouched.  The final project DELETE
    # remains explicit even though child rows have already been drained: it is
    # the durable boundary and still exercises the project's FK cascade for
    # any table added between inspection and execution.
    bind = op.get_bind()
    _delete_translator_rows(bind)
    if bind.execute(sa.text('SELECT 1 FROM "translation_project" LIMIT 1')).first():
        bind.execute(sa.text('DELETE FROM "translation_project"'))


def downgrade() -> None:
    # Upgrade 022 deliberately deleted all pre-existing Translator projects;
    # no downgrade can restore that data.  Refuse every downgrade, including a
    # currently empty note table, so an operator cannot mistake schema removal
    # for a data-preserving rollback.
    raise RuntimeError(
        "Translator migration 022 is forward-only because project data was deleted."
    )
