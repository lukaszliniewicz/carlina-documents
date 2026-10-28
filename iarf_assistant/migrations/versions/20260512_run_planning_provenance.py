"""add run planning steps source and evidence records

Revision ID: 20260512_run_plan_prov
Revises: 20260512_chat_caps
Create Date: 2026-05-12 00:00:03.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '20260512_run_plan_prov'
down_revision = '20260512_chat_caps'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('workflow_run', schema=None) as batch_op:
        batch_op.add_column(sa.Column('understanding_json', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('plan_json', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('resource_reference_ids', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('step_ids', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('source_ids', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('evidence_ids', sa.JSON(), nullable=True))

    op.create_table(
        'workflow_step',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('workflow_run_id', sa.String(length=36), nullable=False),
        sa.Column('parent_step_id', sa.String(length=36), nullable=True),
        sa.Column('step_key', sa.String(length=255), nullable=True),
        sa.Column('step_type', sa.String(length=50), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=True),
        sa.Column('assigned_model_role', sa.String(length=100), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('capability_ids', sa.JSON(), nullable=True),
        sa.Column('tool_ids', sa.JSON(), nullable=True),
        sa.Column('provider_requirements', sa.JSON(), nullable=True),
        sa.Column('resource_reference_ids', sa.JSON(), nullable=True),
        sa.Column('approval_ids', sa.JSON(), nullable=True),
        sa.Column('job_ids', sa.JSON(), nullable=True),
        sa.Column('artifact_ids', sa.JSON(), nullable=True),
        sa.Column('source_ids', sa.JSON(), nullable=True),
        sa.Column('evidence_ids', sa.JSON(), nullable=True),
        sa.Column('input_payload', sa.JSON(), nullable=True),
        sa.Column('output_payload', sa.JSON(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('log_source_ids', sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['user.id'], name='fk_workflow_step_created_by_user_id_user'),
        sa.ForeignKeyConstraint(['parent_step_id'], ['workflow_step.id'], name='fk_workflow_step_parent_step_id_workflow_step'),
        sa.ForeignKeyConstraint(['workflow_run_id'], ['workflow_run.id'], name='fk_workflow_step_workflow_run_id_workflow_run'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('workflow_step', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_workflow_step_created_by_user_id'), ['created_by_user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_workflow_step_parent_step_id'), ['parent_step_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_workflow_step_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_workflow_step_step_key'), ['step_key'], unique=False)
        batch_op.create_index(batch_op.f('ix_workflow_step_step_type'), ['step_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_workflow_step_workflow_run_id'), ['workflow_run_id'], unique=False)

    op.create_table(
        'runtime_source_record',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('workflow_run_id', sa.String(length=36), nullable=False),
        sa.Column('step_id', sa.String(length=36), nullable=True),
        sa.Column('source_type', sa.String(length=50), nullable=False),
        sa.Column('uri', sa.String(length=2048), nullable=False),
        sa.Column('provider', sa.String(length=100), nullable=True),
        sa.Column('external_id', sa.String(length=512), nullable=True),
        sa.Column('title', sa.String(length=512), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('content_hash', sa.String(length=128), nullable=True),
        sa.Column('access_scope', sa.String(length=50), nullable=False),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['user.id'], name='fk_runtime_source_created_by_user_id_user'),
        sa.ForeignKeyConstraint(['step_id'], ['workflow_step.id'], name='fk_runtime_source_step_id_workflow_step'),
        sa.ForeignKeyConstraint(['workflow_run_id'], ['workflow_run.id'], name='fk_runtime_source_workflow_run_id_workflow_run'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('runtime_source_record', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_runtime_source_record_content_hash'), ['content_hash'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_source_record_created_by_user_id'), ['created_by_user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_source_record_external_id'), ['external_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_source_record_provider'), ['provider'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_source_record_source_type'), ['source_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_source_record_step_id'), ['step_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_source_record_workflow_run_id'), ['workflow_run_id'], unique=False)

    op.create_table(
        'runtime_evidence_record',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('workflow_run_id', sa.String(length=36), nullable=False),
        sa.Column('step_id', sa.String(length=36), nullable=True),
        sa.Column('source_id', sa.String(length=36), nullable=False),
        sa.Column('artifact_id', sa.String(length=36), nullable=True),
        sa.Column('claim', sa.Text(), nullable=True),
        sa.Column('quote', sa.Text(), nullable=True),
        sa.Column('citation', sa.JSON(), nullable=True),
        sa.Column('relevance_score', sa.Float(), nullable=True),
        sa.Column('novelty_score', sa.Float(), nullable=True),
        sa.Column('used_in_artifact', sa.Boolean(), nullable=False),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['artifact_id'], ['runtime_artifact.id'], name='fk_runtime_evidence_artifact_id_runtime_artifact'),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['user.id'], name='fk_runtime_evidence_created_by_user_id_user'),
        sa.ForeignKeyConstraint(['source_id'], ['runtime_source_record.id'], name='fk_runtime_evidence_source_id_runtime_source_record'),
        sa.ForeignKeyConstraint(['step_id'], ['workflow_step.id'], name='fk_runtime_evidence_step_id_workflow_step'),
        sa.ForeignKeyConstraint(['workflow_run_id'], ['workflow_run.id'], name='fk_runtime_evidence_workflow_run_id_workflow_run'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('runtime_evidence_record', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_runtime_evidence_record_artifact_id'), ['artifact_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_evidence_record_created_by_user_id'), ['created_by_user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_evidence_record_source_id'), ['source_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_evidence_record_step_id'), ['step_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_evidence_record_workflow_run_id'), ['workflow_run_id'], unique=False)


def downgrade():
    with op.batch_alter_table('runtime_evidence_record', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_runtime_evidence_record_workflow_run_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_evidence_record_step_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_evidence_record_source_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_evidence_record_created_by_user_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_evidence_record_artifact_id'))
    op.drop_table('runtime_evidence_record')

    with op.batch_alter_table('runtime_source_record', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_runtime_source_record_workflow_run_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_source_record_step_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_source_record_source_type'))
        batch_op.drop_index(batch_op.f('ix_runtime_source_record_provider'))
        batch_op.drop_index(batch_op.f('ix_runtime_source_record_external_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_source_record_created_by_user_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_source_record_content_hash'))
    op.drop_table('runtime_source_record')

    with op.batch_alter_table('workflow_step', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_workflow_step_workflow_run_id'))
        batch_op.drop_index(batch_op.f('ix_workflow_step_step_type'))
        batch_op.drop_index(batch_op.f('ix_workflow_step_step_key'))
        batch_op.drop_index(batch_op.f('ix_workflow_step_status'))
        batch_op.drop_index(batch_op.f('ix_workflow_step_parent_step_id'))
        batch_op.drop_index(batch_op.f('ix_workflow_step_created_by_user_id'))
    op.drop_table('workflow_step')

    with op.batch_alter_table('workflow_run', schema=None) as batch_op:
        batch_op.drop_column('evidence_ids')
        batch_op.drop_column('source_ids')
        batch_op.drop_column('step_ids')
        batch_op.drop_column('resource_reference_ids')
        batch_op.drop_column('plan_json')
        batch_op.drop_column('understanding_json')
