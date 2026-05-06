from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003_add_job_status_message_id"
down_revision = "0002_add_intro_bonus_flag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("video_jobs", sa.Column("telegram_status_message_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("video_jobs", "telegram_status_message_id")
