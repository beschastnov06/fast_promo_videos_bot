from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005_robokassa_payments"
down_revision = "0004_add_render_stage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS payments_invoice_id_seq START WITH 1000")
    op.add_column(
        "payments",
        sa.Column(
            "invoice_id",
            sa.BigInteger(),
            server_default=sa.text("nextval('payments_invoice_id_seq')"),
            nullable=False,
        ),
    )
    op.add_column("payments", sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True))
    op.add_column("payments", sa.Column("package_code", sa.Text(), nullable=True))
    op.add_column("payments", sa.Column("buyer_email", sa.Text(), nullable=True))
    op.add_column(
        "payments",
        sa.Column("receipt_status", sa.Text(), server_default="unknown", nullable=False),
    )
    op.add_column("payments", sa.Column("receipt_url", sa.Text(), nullable=True))
    op.add_column("payments", sa.Column("raw_provider_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column(
        "payments",
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_payments_invoice_id", "payments", ["invoice_id"], unique=True)

    # Existing rows are not expected in production yet. Keep a defensive fallback for local dev DBs.
    op.execute("UPDATE payments SET telegram_chat_id = 0 WHERE telegram_chat_id IS NULL")
    op.execute("UPDATE payments SET package_code = 'legacy' WHERE package_code IS NULL")
    op.alter_column("payments", "telegram_chat_id", nullable=False)
    op.alter_column("payments", "package_code", nullable=False)


def downgrade() -> None:
    op.drop_index("ix_payments_invoice_id", table_name="payments")
    op.drop_column("payments", "updated_at")
    op.drop_column("payments", "raw_provider_payload")
    op.drop_column("payments", "receipt_url")
    op.drop_column("payments", "receipt_status")
    op.drop_column("payments", "buyer_email")
    op.drop_column("payments", "package_code")
    op.drop_column("payments", "telegram_chat_id")
    op.drop_column("payments", "invoice_id")
    op.execute("DROP SEQUENCE IF EXISTS payments_invoice_id_seq")
