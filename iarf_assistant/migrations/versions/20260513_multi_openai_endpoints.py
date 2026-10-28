"""add user-defined openai-compatible llm endpoints

Revision ID: 20260513_multi_openai_endpoints
Revises: 20260512_artifact_versions
Create Date: 2026-05-13 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '20260513_multi_openai_endpoints'
down_revision = '20260512_artifact_versions'
branch_labels = None
depends_on = None


def upgrade():
    # SQLite cannot rebuild this parent table while api_keys and llm_models
    # reference it, so use ADD COLUMN operations instead of batch mode.
    op.add_column('llm_providers', sa.Column('user_id', sa.Integer(), nullable=True))
    op.add_column('llm_providers', sa.Column('display_name', sa.String(length=100), nullable=True))
    op.add_column('llm_providers', sa.Column('provider_kind', sa.String(length=50), nullable=False, server_default='native'))
    op.create_index(op.f('ix_llm_providers_user_id'), 'llm_providers', ['user_id'], unique=False)

    providers = sa.table(
        'llm_providers',
        sa.column('name', sa.String(length=100)),
        sa.column('display_name', sa.String(length=100)),
        sa.column('provider_kind', sa.String(length=50)),
    )
    bind = op.get_bind()
    bind.execute(
        providers.update()
        .where(providers.c.name.in_(['lmstudio', 'vllm', 'custom_openai']))
        .values(provider_kind='openai_compatible')
    )
    bind.execute(
        providers.update()
        .where(providers.c.name == 'custom_openai')
        .values(display_name='Custom OpenAI (Legacy)')
    )


def downgrade():
    op.drop_index(op.f('ix_llm_providers_user_id'), table_name='llm_providers')
    op.drop_column('llm_providers', 'provider_kind')
    op.drop_column('llm_providers', 'display_name')
    op.drop_column('llm_providers', 'user_id')
