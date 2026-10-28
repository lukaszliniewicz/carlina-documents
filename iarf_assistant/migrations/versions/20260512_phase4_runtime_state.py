"""phase 4 runtime state foundation

Revision ID: 20260512_phase4
Revises: 497401de4bc9
Create Date: 2026-05-12 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '20260512_phase4'
down_revision = '497401de4bc9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'workflow_run',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('workflow_id', sa.String(length=255), nullable=False),
        sa.Column('source_addon_id', sa.String(length=100), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('input_payload', sa.JSON(), nullable=True),
        sa.Column('state_json', sa.JSON(), nullable=True),
        sa.Column('current_step', sa.String(length=255), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('approval_ids', sa.JSON(), nullable=True),
        sa.Column('job_ids', sa.JSON(), nullable=True),
        sa.Column('artifact_ids', sa.JSON(), nullable=True),
        sa.Column('log_source_ids', sa.JSON(), nullable=True),
        sa.Column('resume_url', sa.String(length=512), nullable=True),
        sa.Column('cancel_url', sa.String(length=512), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['user.id'], name='fk_workflow_run_created_by_user_id_user'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('workflow_run', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_workflow_run_created_by_user_id'), ['created_by_user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_workflow_run_source_addon_id'), ['source_addon_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_workflow_run_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_workflow_run_workflow_id'), ['workflow_id'], unique=False)

    op.create_table(
        'runtime_approval_request',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('requester_user_id', sa.Integer(), nullable=True),
        sa.Column('approver_user_id', sa.Integer(), nullable=True),
        sa.Column('subject_type', sa.String(length=50), nullable=False),
        sa.Column('subject_id', sa.String(length=255), nullable=False),
        sa.Column('tool_id', sa.String(length=255), nullable=True),
        sa.Column('workflow_run_id', sa.String(length=36), nullable=True),
        sa.Column('effects', sa.JSON(), nullable=True),
        sa.Column('preview_payload', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.Column('audit_log_source_id', sa.String(length=255), nullable=True),
        sa.Column('log_source_ids', sa.JSON(), nullable=True),
        sa.Column('resolution_note', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['approver_user_id'], ['user.id'], name='fk_runtime_approval_approver_user_id_user'),
        sa.ForeignKeyConstraint(['requester_user_id'], ['user.id'], name='fk_runtime_approval_requester_user_id_user'),
        sa.ForeignKeyConstraint(['workflow_run_id'], ['workflow_run.id'], name='fk_runtime_approval_workflow_run_id_workflow_run'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('runtime_approval_request', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_runtime_approval_request_approver_user_id'), ['approver_user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_approval_request_requester_user_id'), ['requester_user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_approval_request_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_approval_request_subject_id'), ['subject_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_approval_request_subject_type'), ['subject_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_approval_request_tool_id'), ['tool_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_approval_request_workflow_run_id'), ['workflow_run_id'], unique=False)

    op.create_table(
        'runtime_resource_reference',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('owner_user_id', sa.Integer(), nullable=True),
        sa.Column('resource_type', sa.String(length=50), nullable=False),
        sa.Column('resource_uri', sa.String(length=1024), nullable=False),
        sa.Column('provider', sa.String(length=100), nullable=True),
        sa.Column('label', sa.String(length=255), nullable=True),
        sa.Column('permissions', sa.JSON(), nullable=True),
        sa.Column('scopes', sa.JSON(), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('source_addon_id', sa.String(length=100), nullable=True),
        sa.Column('workflow_run_id', sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(['owner_user_id'], ['user.id'], name='fk_runtime_resource_owner_user_id_user'),
        sa.ForeignKeyConstraint(['workflow_run_id'], ['workflow_run.id'], name='fk_runtime_resource_workflow_run_id_workflow_run'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('runtime_resource_reference', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_runtime_resource_reference_owner_user_id'), ['owner_user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_resource_reference_resource_type'), ['resource_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_resource_reference_source_addon_id'), ['source_addon_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_resource_reference_workflow_run_id'), ['workflow_run_id'], unique=False)

    op.create_table(
        'runtime_job',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('source_type', sa.String(length=50), nullable=False),
        sa.Column('source_id', sa.String(length=255), nullable=True),
        sa.Column('source_addon_id', sa.String(length=100), nullable=True),
        sa.Column('owner_user_id', sa.Integer(), nullable=True),
        sa.Column('task_name', sa.String(length=255), nullable=True),
        sa.Column('workflow_run_id', sa.String(length=36), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('progress', sa.Float(), nullable=True),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('input_payload', sa.JSON(), nullable=True),
        sa.Column('result_payload', sa.JSON(), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('queued_at', sa.DateTime(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('log_source_ids', sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(['owner_user_id'], ['user.id'], name='fk_runtime_job_owner_user_id_user'),
        sa.ForeignKeyConstraint(['workflow_run_id'], ['workflow_run.id'], name='fk_runtime_job_workflow_run_id_workflow_run'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('runtime_job', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_runtime_job_owner_user_id'), ['owner_user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_job_source_addon_id'), ['source_addon_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_job_source_id'), ['source_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_job_source_type'), ['source_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_job_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_job_workflow_run_id'), ['workflow_run_id'], unique=False)

    op.create_table(
        'runtime_artifact',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('job_id', sa.String(length=64), nullable=True),
        sa.Column('workflow_run_id', sa.String(length=36), nullable=True),
        sa.Column('source_addon_id', sa.String(length=100), nullable=True),
        sa.Column('created_by_user_id', sa.Integer(), nullable=True),
        sa.Column('artifact_type', sa.String(length=50), nullable=False),
        sa.Column('uri', sa.String(length=1024), nullable=True),
        sa.Column('path', sa.String(length=1024), nullable=True),
        sa.Column('remote_reference', sa.String(length=1024), nullable=True),
        sa.Column('mime_type', sa.String(length=255), nullable=True),
        sa.Column('label', sa.String(length=255), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('retention_policy', sa.String(length=100), nullable=False),
        sa.Column('download_permission', sa.String(length=50), nullable=False),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['user.id'], name='fk_runtime_artifact_created_by_user_id_user'),
        sa.ForeignKeyConstraint(['job_id'], ['runtime_job.id'], name='fk_runtime_artifact_job_id_runtime_job'),
        sa.ForeignKeyConstraint(['workflow_run_id'], ['workflow_run.id'], name='fk_runtime_artifact_workflow_run_id_workflow_run'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('runtime_artifact', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_runtime_artifact_artifact_type'), ['artifact_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_artifact_created_by_user_id'), ['created_by_user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_artifact_job_id'), ['job_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_artifact_source_addon_id'), ['source_addon_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_artifact_workflow_run_id'), ['workflow_run_id'], unique=False)


def downgrade():
    with op.batch_alter_table('runtime_artifact', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_runtime_artifact_workflow_run_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_artifact_source_addon_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_artifact_job_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_artifact_created_by_user_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_artifact_artifact_type'))
    op.drop_table('runtime_artifact')

    with op.batch_alter_table('runtime_job', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_runtime_job_workflow_run_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_job_status'))
        batch_op.drop_index(batch_op.f('ix_runtime_job_source_type'))
        batch_op.drop_index(batch_op.f('ix_runtime_job_source_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_job_source_addon_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_job_owner_user_id'))
    op.drop_table('runtime_job')

    with op.batch_alter_table('runtime_resource_reference', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_runtime_resource_reference_workflow_run_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_resource_reference_source_addon_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_resource_reference_resource_type'))
        batch_op.drop_index(batch_op.f('ix_runtime_resource_reference_owner_user_id'))
    op.drop_table('runtime_resource_reference')

    with op.batch_alter_table('runtime_approval_request', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_runtime_approval_request_workflow_run_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_approval_request_tool_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_approval_request_subject_type'))
        batch_op.drop_index(batch_op.f('ix_runtime_approval_request_subject_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_approval_request_status'))
        batch_op.drop_index(batch_op.f('ix_runtime_approval_request_requester_user_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_approval_request_approver_user_id'))
    op.drop_table('runtime_approval_request')

    with op.batch_alter_table('workflow_run', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_workflow_run_workflow_id'))
        batch_op.drop_index(batch_op.f('ix_workflow_run_status'))
        batch_op.drop_index(batch_op.f('ix_workflow_run_source_addon_id'))
        batch_op.drop_index(batch_op.f('ix_workflow_run_created_by_user_id'))
    op.drop_table('workflow_run')
