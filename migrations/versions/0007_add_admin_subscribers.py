from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007_admin_subscribers"
down_revision = "0006_payment_msg_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_subscribers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_username", sa.Text(), nullable=False),
        sa.Column("first_name", sa.Text(), nullable=True),
        sa.Column("last_name", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_user_id", name="uq_admin_subscribers_telegram_user_id"),
    )
    op.create_index(
        "ix_admin_subscribers_telegram_user_id",
        "admin_subscribers",
        ["telegram_user_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_admin_subscribers_telegram_user_id", table_name="admin_subscribers")
    op.drop_table("admin_subscribers")
