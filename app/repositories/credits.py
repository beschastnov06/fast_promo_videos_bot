from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CreditAccount, CreditTransaction


class InsufficientCreditsError(RuntimeError):
    pass


async def charge_credits(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    amount: int,
    reason: str,
    source: str,
    related_job_id: uuid.UUID | None = None,
) -> None:
    if amount <= 0:
        raise ValueError("amount must be positive")

    account = await _get_locked_account(session, user_id)
    if account.balance < amount:
        raise InsufficientCreditsError("Not enough credits")

    account.balance -= amount
    session.add(
        CreditTransaction(
            user_id=user_id,
            amount=-amount,
            reason=reason,
            source=source,
            related_job_id=related_job_id,
        )
    )


async def add_credits(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    amount: int,
    reason: str,
    source: str,
    related_job_id: uuid.UUID | None = None,
) -> None:
    if amount <= 0:
        raise ValueError("amount must be positive")

    account = await _get_locked_account(session, user_id)
    account.balance += amount
    session.add(
        CreditTransaction(
            user_id=user_id,
            amount=amount,
            reason=reason,
            source=source,
            related_job_id=related_job_id,
        )
    )


async def get_balance(session: AsyncSession, *, user_id: uuid.UUID) -> int:
    result = await session.execute(
        select(CreditAccount).where(CreditAccount.user_id == user_id)
    )
    account = result.scalar_one_or_none()
    if account is None:
        account = CreditAccount(user_id=user_id)
        session.add(account)
        await session.flush()

    return account.balance


async def _get_locked_account(session: AsyncSession, user_id: uuid.UUID) -> CreditAccount:
    result = await session.execute(
        select(CreditAccount)
        .where(CreditAccount.user_id == user_id)
        .with_for_update()
    )
    account = result.scalar_one_or_none()
    if account is None:
        account = CreditAccount(user_id=user_id)
        session.add(account)
        await session.flush()

    return account
