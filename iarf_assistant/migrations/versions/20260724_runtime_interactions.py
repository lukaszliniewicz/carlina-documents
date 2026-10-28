"""add durable conversational runtime interactions

Revision ID: 20260724_runtime_interactions
Revises: 60e8bba8d4f3
Create Date: 2026-07-24 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '20260724_runtime_interactions'
down_revision = '60e8bba8d4f3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'runtime_interaction',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('requester_user_id', sa.Integer(), nullable=False),
        sa.Column('resolver_user_id', sa.Integer(), nullable=True),
        sa.Column('chat_session_id', sa.String(length=36), nullable=False),
        sa.Column('workflow_run_id', sa.String(length=36), nullable=True),
        sa.Column('approval_id', sa.String(length=36), nullable=True),
        sa.Column('tool_call_id', sa.String(length=512), nullable=True),
        sa.Column('tool_name', sa.String(length=255), nullable=True),
        sa.Column('interaction_type', sa.String(length=50), nullable=False),
        # EncryptedType is backed by TEXT; encryption remains an application concern.
        sa.Column('request_payload', sa.Text(), nullable=False),
        sa.Column('response_payload', sa.Text(), nullable=True),
        sa.Column('resume_payload', sa.Text(), nullable=True),
        sa.Column('result_payload', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.Column('resume_started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ['approval_id'],
            ['runtime_approval_request.id'],
            name='fk_runtime_interaction_approval_id_runtime_approval_request',
        ),
        sa.ForeignKeyConstraint(
            ['chat_session_id'],
            ['chat_session.id'],
            name='fk_runtime_interaction_chat_session_id_chat_session',
        ),
        sa.ForeignKeyConstraint(
            ['requester_user_id'],
            ['user.id'],
            name='fk_runtime_interaction_requester_user_id_user',
        ),
        sa.ForeignKeyConstraint(
            ['resolver_user_id'],
            ['user.id'],
            name='fk_runtime_interaction_resolver_user_id_user',
        ),
        sa.ForeignKeyConstraint(
            ['workflow_run_id'],
            ['workflow_run.id'],
            name='fk_runtime_interaction_workflow_run_id_workflow_run',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'chat_session_id',
            'tool_call_id',
            name='uq_runtime_interaction_chat_tool_call',
        ),
    )
    with op.batch_alter_table('runtime_interaction', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_runtime_interaction_approval_id'), ['approval_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_interaction_chat_session_id'), ['chat_session_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_interaction_interaction_type'), ['interaction_type'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_interaction_requester_user_id'), ['requester_user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_interaction_resolver_user_id'), ['resolver_user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_interaction_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_interaction_tool_name'), ['tool_name'], unique=False)
        batch_op.create_index(batch_op.f('ix_runtime_interaction_workflow_run_id'), ['workflow_run_id'], unique=False)


def downgrade():
    with op.batch_alter_table('runtime_interaction', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_runtime_interaction_workflow_run_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_interaction_tool_name'))
        batch_op.drop_index(batch_op.f('ix_runtime_interaction_status'))
        batch_op.drop_index(batch_op.f('ix_runtime_interaction_resolver_user_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_interaction_requester_user_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_interaction_interaction_type'))
        batch_op.drop_index(batch_op.f('ix_runtime_interaction_chat_session_id'))
        batch_op.drop_index(batch_op.f('ix_runtime_interaction_approval_id'))
    op.drop_table('runtime_interaction')
