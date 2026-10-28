"""add workflow_run.chat_session_id foreign key on non-SQLite backends

Revision ID: 20260515_workflow_run_chat_session_fk
Revises: b3dd8694c5b4
Create Date: 2026-05-15 00:00:02.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "20260515_workflow_run_chat_session_fk"
down_revision = "b3dd8694c5b4"
branch_labels = None
depends_on = None

_FK_NAME = "fk_workflow_run_chat_session_id_chat_session"


def _has_foreign_key(bind, table_name: str, fk_name: str) -> bool:
    inspector = sa.inspect(bind)
    for fk in inspector.get_foreign_keys(table_name):
        if fk.get("name") == fk_name:
            return True
    return False


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # SQLite dev upgrades intentionally avoid parent table rebuilds here.
        return
    if not _has_foreign_key(bind, "workflow_run", _FK_NAME):
        op.create_foreign_key(
            _FK_NAME,
            "workflow_run",
            "chat_session",
            ["chat_session_id"],
            ["id"],
        )


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    if _has_foreign_key(bind, "workflow_run", _FK_NAME):
        op.drop_constraint(_FK_NAME, "workflow_run", type_="foreignkey")
