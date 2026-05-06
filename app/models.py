from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Numeric, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    telegram_username: Mapped[str | None] = mapped_column(Text)
    first_name: Mapped[str | None] = mapped_column(Text)
    last_name: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active", server_default="active")
    intro_bonus_granted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    credit_account: Mapped[CreditAccount | None] = relationship(back_populates="user")
    jobs: Mapped[list[VideoJob]] = relationship(back_populates="user")


class CreditAccount(Base, TimestampMixin):
    __tablename__ = "credit_accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    balance: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    user: Mapped[User] = relationship(back_populates="credit_account")

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_credit_accounts_user_id"),
    )


class CreditTransaction(Base):
    __tablename__ = "credit_transactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    related_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class VideoJob(Base, TimestampMixin):
    __tablename__ = "video_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft", server_default="draft")
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_status_message_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_video_file_id: Mapped[str | None] = mapped_column(Text)
    telegram_video_file_unique_id: Mapped[str | None] = mapped_column(Text)
    ad_content_type: Mapped[str] = mapped_column(Text, nullable=False, default="none", server_default="none")
    ad_text: Mapped[str | None] = mapped_column(Text)
    ad_banner_file_id: Mapped[str | None] = mapped_column(Text)
    ad_banner_file_unique_id: Mapped[str | None] = mapped_column(Text)
    ad_banner_name: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    credits_charged: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="jobs")
    settings: Mapped[VideoJobSettings | None] = relationship(back_populates="job")


class VideoJobSettings(Base, TimestampMixin):
    __tablename__ = "video_job_settings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("video_jobs.id", ondelete="CASCADE"), nullable=False)
    video_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    video_format: Mapped[str] = mapped_column(Text, nullable=False, default="9:16", server_default="9:16")
    fill_color: Mapped[str] = mapped_column(Text, nullable=False, default="black", server_default="black")
    subtitle_font: Mapped[str] = mapped_column(Text, nullable=False, default="DejaVu Sans", server_default="DejaVu Sans")
    subtitle_color: Mapped[str] = mapped_column(Text, nullable=False, default="white", server_default="white")
    video_speed: Mapped[float] = mapped_column(Numeric(3, 2), nullable=False, default=1.00, server_default="1.00")
    mirror: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    strip_metadata: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    job: Mapped[VideoJob] = relationship(back_populates="settings")

    __table_args__ = (
        UniqueConstraint("job_id", name="uq_video_job_settings_job_id"),
    )


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    provider_payment_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    credits_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
