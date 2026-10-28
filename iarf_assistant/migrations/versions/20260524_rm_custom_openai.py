"""Remove legacy custom_openai provider and dependent records.

Revision ID: 20260524_rm_custom_openai
Revises: 70bd868428a2
Create Date: 2026-05-24 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20260524_rm_custom_openai'
down_revision = '70bd868428a2'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()

    providers = sa.table(
        'llm_providers',
        sa.column('id', sa.Integer()),
        sa.column('name', sa.String(length=100)),
    )
    api_keys = sa.table(
        'api_key',
        sa.column('provider_id', sa.Integer()),
    )
    llm_models = sa.table(
        'llm_models',
        sa.column('provider_id', sa.Integer()),
    )
    settings = sa.table(
        'setting',
        sa.column('key', sa.String(length=50)),
        sa.column('value', sa.String(length=255)),
    )

    legacy_provider_ids = [
        row[0]
        for row in bind.execute(
            sa.select(providers.c.id).where(providers.c.name == 'custom_openai')
        ).fetchall()
    ]
    if legacy_provider_ids:
        bind.execute(api_keys.delete().where(api_keys.c.provider_id.in_(legacy_provider_ids)))
        bind.execute(llm_models.delete().where(llm_models.c.provider_id.in_(legacy_provider_ids)))
        bind.execute(providers.delete().where(providers.c.id.in_(legacy_provider_ids)))

    bind.execute(
        settings.update()
        .where(
            settings.c.key.in_(
                [
                    'default_fast_model',
                    'default_medium_model',
                    'default_powerful_model',
                ]
            )
        )
        .where(settings.c.value.like('custom_openai/%'))
        .values(value=None)
    )


def downgrade():
    # This data cleanup intentionally has no automatic restore path.
    op.execute("SELECT 1")
