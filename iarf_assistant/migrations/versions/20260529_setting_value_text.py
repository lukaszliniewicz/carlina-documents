"""Widen setting values for schema-rendered addon settings.

Revision ID: 20260529_setting_value_text
Revises: 20260524_rm_custom_openai
Create Date: 2026-05-29 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '20260529_setting_value_text'
down_revision = '20260524_rm_custom_openai'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('setting') as batch_op:
        batch_op.alter_column(
            'value',
            existing_type=sa.String(length=255),
            type_=sa.Text(),
            existing_nullable=True,
        )


def downgrade():
    with op.batch_alter_table('setting') as batch_op:
        batch_op.alter_column(
            'value',
            existing_type=sa.Text(),
            type_=sa.String(length=255),
            existing_nullable=True,
        )
