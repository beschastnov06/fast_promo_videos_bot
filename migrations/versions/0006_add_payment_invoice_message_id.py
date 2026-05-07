from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0006_payment_msg_id"
down_revision = "0005_robokassa_payments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "payments",
        sa.Column("telegram_invoice_message_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("payments", "telegram_invoice_message_id")
