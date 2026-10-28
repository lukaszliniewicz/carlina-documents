"""add artifact versions and review state

Revision ID: 20260512_artifact_versions
Revises: 20260512_run_plan_prov
Create Date: 2026-05-12 00:00:04.000000

"""
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = '20260512_artifact_versions'
down_revision = '20260512_run_plan_prov'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('runtime_artifact', schema=None) as batch_op:
        batch_op.add_column(sa.Column('content_hash', sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column('size_bytes', sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')))
        batch_op.add_column(sa.Column('current_version_id', sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column('review_status', sa.String(length=32), nullable=False, server_default='draft'))
        batch_op.add_column(sa.Column('reviewed_by_user_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('reviewed_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('review_note', sa.Text(), nullable=True))
        batch_op.create_foreign_key(
            'fk_runtime_artifact_reviewed_by_user_id_user',
            'user',
            ['reviewed_by_user_id'],
            ['id'],
        )
        batch_op.create_index(batch_op.f('ix_runtime_artifact_content_hash'), ['content_hash'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_artifact_current_version_id'), ['current_version_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_artifact_review_status'), ['review_status'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_artifact_reviewed_by_user_id'), ['reviewed_by_user_id'], unique=False)

    op.create_table(
        'runtime_artifact_version',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('artifact_id', sa.String(length=36), nullable=False),
        sa.Column('version_number', sa.Integer(), nullable=False),
        sa.Column('job_id', sa.String(length=64), nullable=True),
        sa.Column('workflow_run_id', sa.String(length=36), nullable=True),
        sa.Column('step_id', sa.String(length=36), nullable=True),
        sa.Column('source_addon_id', sa.String(length=100), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('artifact_type', sa.String(length=50), nullable=False),
        sa.Column('uri', sa.String(length=1024), nullable=True),
        sa.Column('path', sa.String(length=1024), nullable=True),
        sa.Column('remote_reference', sa.String(length=1024), nullable=True),
        sa.Column('mime_type', sa.String(length=255), nullable=True),
        sa.Column('content_hash', sa.String(length=128), nullable=True),
        sa.Column('size_bytes', sa.BigInteger(), nullable=True),
        sa.Column('label', sa.String(length=255), nullable=True),
        sa.Column('source_ids', sa.JSON(), nullable=True),
        sa.Column('evidence_ids', sa.JSON(), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('change_summary', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['artifact_id'], ['runtime_artifact.id'], name='fk_runtime_artifact_version_artifact_id_runtime_artifact'),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['user.id'], name='fk_runtime_artifact_version_created_by_user_id_user'),
        sa.ForeignKeyConstraint(['job_id'], ['runtime_job.id'], name='fk_runtime_artifact_version_job_id_runtime_job'),
        sa.ForeignKeyConstraint(['step_id'], ['workflow_step.id'], name='fk_runtime_artifact_version_step_id_workflow_step'),
        sa.ForeignKeyConstraint(['workflow_run_id'], ['workflow_run.id'], name='fk_runtime_artifact_version_workflow_run_id_workflow_run'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('artifact_id', 'version_number', name='uq_runtime_artifact_version_artifact_version_number'),
    )
    with op.batch_alter_table('runtime_artifact_version', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_runtime_artifact_version_artifact_id'), ['artifact_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_artifact_version_artifact_type'), ['artifact_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_artifact_version_content_hash'), ['content_hash'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_artifact_version_created_by_user_id'), ['created_by_user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_artifact_version_job_id'), ['job_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_artifact_version_source_addon_id'), ['source_addon_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_artifact_version_step_id'), ['step_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_artifact_version_workflow_run_id'), ['workflow_run_id'], unique=False)

    _backfill_existing_artifact_versions()


def _backfill_existing_artifact_versions():
    bind = op.get_bind()
    artifact_table = sa.table(
        'runtime_artifact',
        sa.column('id', sa.String(length=36)),
        sa.column('job_id', sa.String(length=64)),
        sa.column('workflow_run_id', sa.String(length=36)),
        sa.column('source_addon_id', sa.String(length=100)),
        sa.column('created_by_user_id', sa.Integer()),
        sa.column('artifact_type', sa.String(length=50)),
        sa.column('uri', sa.String(length=1024)),
        sa.column('path', sa.String(length=1024)),
        sa.column('remote_reference', sa.String(length=1024)),
        sa.column('mime_type', sa.String(length=255)),
        sa.column('content_hash', sa.String(length=128)),
        sa.column('size_bytes', sa.BigInteger()),
        sa.column('label', sa.String(length=255)),
        sa.column('metadata_json', sa.JSON()),
        sa.column('created_at', sa.DateTime()),
        sa.column('updated_at', sa.DateTime()),
        sa.column('current_version_id', sa.String(length=36)),
    )
    version_table = sa.table(
        'runtime_artifact_version',
        sa.column('id', sa.String(length=36)),
        sa.column('artifact_id', sa.String(length=36)),
        sa.column('version_number', sa.Integer()),
        sa.column('job_id', sa.String(length=64)),
        sa.column('workflow_run_id', sa.String(length=36)),
        sa.column('step_id', sa.String(length=36)),
        sa.column('source_addon_id', sa.String(length=100)),
        sa.column('created_by_user_id', sa.Integer()),
        sa.column('artifact_type', sa.String(length=50)),
        sa.column('uri', sa.String(length=1024)),
        sa.column('path', sa.String(length=1024)),
        sa.column('remote_reference', sa.String(length=1024)),
        sa.column('mime_type', sa.String(length=255)),
        sa.column('content_hash', sa.String(length=128)),
        sa.column('size_bytes', sa.BigInteger()),
        sa.column('label', sa.String(length=255)),
        sa.column('source_ids', sa.JSON()),
        sa.column('evidence_ids', sa.JSON()),
        sa.column('metadata_json', sa.JSON()),
        sa.column('change_summary', sa.Text()),
        sa.column('created_at', sa.DateTime()),
    )

    now = datetime.now(UTC).replace(tzinfo=None)
    rows = bind.execute(sa.select(artifact_table)).mappings().all()
    for row in rows:
        version_id = str(uuid.uuid4())
        created_at = row.get('created_at') or now
        bind.execute(
            version_table.insert().values(
                id=version_id,
                artifact_id=row['id'],
                version_number=1,
                job_id=row.get('job_id'),
                workflow_run_id=row.get('workflow_run_id'),
                step_id=None,
                source_addon_id=row.get('source_addon_id'),
                created_by_user_id=row.get('created_by_user_id'),
                artifact_type=row.get('artifact_type') or 'file',
                uri=row.get('uri'),
                path=row.get('path'),
                remote_reference=row.get('remote_reference'),
                mime_type=row.get('mime_type'),
                content_hash=row.get('content_hash'),
                size_bytes=row.get('size_bytes'),
                label=row.get('label'),
                source_ids=[],
                evidence_ids=[],
                metadata_json=row.get('metadata_json'),
                change_summary='Initial artifact version backfilled from pre-version artifact record.',
                created_at=created_at,
            )
        )
        bind.execute(
            artifact_table.update()
            .where(artifact_table.c.id == row['id'])
            .values(current_version_id=version_id, updated_at=row.get('updated_at') or created_at)
        )


def downgrade():
    with op.batch_alter_table('runtime_artifact_version', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_runtime_artifact_version_workflow_run_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_artifact_version_step_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_artifact_version_source_addon_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_artifact_version_job_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_artifact_version_created_by_user_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_artifact_version_content_hash'))
        batch_op.drop_index(batch_op.f('ix_runtime_artifact_version_artifact_type'))
        batch_op.drop_index(batch_op.f('ix_runtime_artifact_version_artifact_id'))
    op.drop_table('runtime_artifact_version')

    with op.batch_alter_table('runtime_artifact', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_runtime_artifact_reviewed_by_user_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_artifact_review_status'))
        batch_op.drop_index(batch_op.f('ix_runtime_artifact_current_version_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_artifact_content_hash'))
        batch_op.drop_constraint('fk_runtime_artifact_reviewed_by_user_id_user', type_='foreignkey')
        batch_op.drop_column('review_note')
        batch_op.drop_column('reviewed_at')
        batch_op.drop_column('reviewed_by_user_id')
        batch_op.drop_column('review_status')
        batch_op.drop_column('current_version_id')
        batch_op.drop_column('updated_at')
        batch_op.drop_column('size_bytes')
        batch_op.drop_column('content_hash')
