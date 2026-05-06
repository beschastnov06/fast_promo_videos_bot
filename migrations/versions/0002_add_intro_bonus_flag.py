from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_add_intro_bonus_flag"
down_revision = "0001_initial_production_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("intro_bonus_granted", sa.Boolean(), server_default="false", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("users", "intro_bonus_granted")
