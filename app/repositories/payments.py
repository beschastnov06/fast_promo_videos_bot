from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.packages import VideoPackage
from app.models import Payment
from app.repositories.credits import add_credits, get_balance


async def create_pending_payment(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    telegram_chat_id: int,
    telegram_invoice_message_id: int | None,
    package: VideoPackage,
) -> Payment:
    payment = Payment(
        user_id=user_id,
        telegram_chat_id=telegram_chat_id,
        telegram_invoice_message_id=telegram_invoice_message_id,
        provider="robokassa",
        status="pending",
        amount_cents=package.amount_cents,
        currency="RUB",
        credits_amount=package.videos_count,
        package_code=package.code,
        receipt_status="pending",
    )
    session.add(payment)
    await session.flush()
    await session.refresh(payment)
    return payment


async def get_payment_by_invoice_id(
    session: AsyncSession,
    invoice_id: int,
    *,
    for_update: bool = False,
) -> Payment | None:
    query = select(Payment).where(Payment.invoice_id == invoice_id)
    if for_update:
        query = query.with_for_update()
    result = await session.execute(query)
    return result.scalar_one_or_none()


async def mark_payment_paid_and_credit(
    session: AsyncSession,
    payment: Payment,
    *,
    buyer_email: str | None,
    raw_provider_payload: dict[str, str],
) -> tuple[bool, int]:
    if payment.status == "paid":
        balance_value = await get_balance(session, user_id=payment.user_id)
        return False, balance_value

    payment.status = "paid"
    payment.paid_at = datetime.now(UTC)
    payment.buyer_email = buyer_email
    payment.receipt_status = "delegated_to_robokassa"
    payment.raw_provider_payload = dict(raw_provider_payload)

    await add_credits(
        session,
        user_id=payment.user_id,
        amount=payment.credits_amount,
        reason="payment_robokassa",
        source="robokassa",
    )
    balance_value = await get_balance(session, user_id=payment.user_id)
    return True, balance_value
