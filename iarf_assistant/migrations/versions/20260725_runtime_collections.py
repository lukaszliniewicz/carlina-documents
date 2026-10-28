"""add durable runtime collection and item ledgers

Revision ID: 20260725_runtime_collections
Revises: 20260724_execution_kernel
Create Date: 2026-07-25 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "20260725_runtime_collections"
down_revision = "20260724_execution_kernel"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "runtime_collection",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workflow_run_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
        sa.Column("collection_key", sa.String(length=96), nullable=False),
        # EncryptedType columns are backed by TEXT; encryption is applied by the app.
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("objective", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(length=100), nullable=False),
        sa.Column("adapter_id", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("identity_keys", sa.JSON(), nullable=True),
        sa.Column("schema_json", sa.Text(), nullable=True),
        sa.Column("partitions_json", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("storage_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            name="fk_runtime_collection_owner_user_id_user",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_run_id"],
            ["workflow_run.id"],
            name="fk_runtime_collection_workflow_run_id_workflow_run",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workflow_run_id",
            "collection_key",
            name="uq_runtime_collection_run_key",
        ),
    )
    with op.batch_alter_table("runtime_collection", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_runtime_collection_adapter_id"),
            ["adapter_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_runtime_collection_kind"),
            ["kind"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_runtime_collection_owner_user_id"),
            ["owner_user_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_runtime_collection_status"),
            ["status"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_runtime_collection_workflow_run_id"),
            ["workflow_run_id"],
            unique=False,
        )

    op.create_table(
        "runtime_collection_item",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("collection_id", sa.String(length=36), nullable=False),
        sa.Column("item_key", sa.String(length=128), nullable=False),
        sa.Column("identity_hash", sa.String(length=64), nullable=False),
        sa.Column("identity_key", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("parent_item_key", sa.String(length=128), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=True),
        sa.Column("source_type", sa.String(length=100), nullable=True),
        sa.Column("source_id", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(length=100), nullable=True),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("uri", sa.Text(), nullable=True),
        sa.Column("canonical_uri", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=128), nullable=True),
        sa.Column("content_ref_json", sa.Text(), nullable=True),
        sa.Column("source_ref_json", sa.Text(), nullable=True),
        sa.Column("resource_reference_ids", sa.JSON(), nullable=True),
        sa.Column("estimated_tokens", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column(
            "annotation_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("lease_token", sa.String(length=36), nullable=True),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("workflow_step_id", sa.String(length=36), nullable=True),
        sa.Column("annotations_json", sa.Text(), nullable=True),
        sa.Column("result_ref_json", sa.Text(), nullable=True),
        sa.Column("latest_error", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("leased_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["collection_id"],
            ["runtime_collection.id"],
            name="fk_runtime_collection_item_collection_id_runtime_collection",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_step_id"],
            ["workflow_step.id"],
            name="fk_runtime_collection_item_workflow_step_id_workflow_step",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "collection_id",
            "identity_hash",
            name="uq_runtime_collection_item_identity",
        ),
        sa.UniqueConstraint(
            "collection_id",
            "item_key",
            name="uq_runtime_collection_item_key",
        ),
    )
    with op.batch_alter_table("runtime_collection_item", schema=None) as batch_op:
        for column in (
            "collection_id",
            "content_hash",
            "fingerprint",
            "lease_expires_at",
            "lease_owner",
            "lease_token",
            "parent_item_key",
            "provider",
            "source_type",
            "status",
            "workflow_step_id",
        ):
            batch_op.create_index(
                batch_op.f(f"ix_runtime_collection_item_{column}"),
                [column],
                unique=False,
            )


def downgrade():
    op.drop_table("runtime_collection_item")
    op.drop_table("runtime_collection")
