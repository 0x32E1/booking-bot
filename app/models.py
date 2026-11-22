from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .timeutil import now


class Base(DeclarativeBase):
    pass


class Shop(Base):
    """One barbershop = one Telegram bot token = one paying customer."""

    __tablename__ = "shops"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    bot_token: Mapped[str] = mapped_column(String(100), unique=True)
    owner_tg_id: Mapped[int] = mapped_column(BigInteger)
    address: Mapped[str | None] = mapped_column(String(255), default=None)
    phone: Mapped[str | None] = mapped_column(String(32), default=None)
    hours_text: Mapped[str | None] = mapped_column(String(255), default=None)
    latitude: Mapped[float | None] = mapped_column(Float, default=None)
    longitude: Mapped[float | None] = mapped_column(Float, default=None)
    slot_step: Mapped[int] = mapped_column(Integer, default=30)  # minutes between start times
    rebook_days: Mapped[int] = mapped_column(Integer, default=21)  # default haircut cycle
    max_no_shows: Mapped[int] = mapped_column(Integer, default=2)  # 0 = never block
    paid_until: Mapped[date] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_broadcast: Mapped[date | None] = mapped_column(Date, default=None)
    last_expiry_notice: Mapped[date | None] = mapped_column(Date, default=None)
    last_daily_report: Mapped[date | None] = mapped_column(Date, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class User(Base):
    """Anyone who talks to a shop's bot (clients and staff), plus offline clients
    added by a barber (tg_id = NULL until they open the bot and share the same phone)."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("shop_id", "tg_id"),
        Index("ix_users_shop_phone", "shop_id", "phone"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"))
    tg_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    name: Mapped[str | None] = mapped_column(String(120), default=None)
    phone: Mapped[str | None] = mapped_column(String(32), default=None)
    lang: Mapped[str | None] = mapped_column(String(2), default=None)
    no_show_count: Mapped[int] = mapped_column(Integer, default=0)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    last_nudge_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Barber(Base):
    __tablename__ = "barbers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"))
    tg_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    name: Mapped[str] = mapped_column(String(120))
    commission_pct: Mapped[int] = mapped_column(Integer, default=50)  # barber's share of revenue
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    invite_code: Mapped[str] = mapped_column(String(32), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class WorkHours(Base):
    """One row per working weekday (0 = Monday). No row = day off."""

    __tablename__ = "work_hours"
    __table_args__ = (UniqueConstraint("barber_id", "weekday"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    barber_id: Mapped[int] = mapped_column(ForeignKey("barbers.id"))
    weekday: Mapped[int] = mapped_column(Integer)
    opens: Mapped[str] = mapped_column(String(5))  # "09:00"
    closes: Mapped[str] = mapped_column(String(5))  # "20:00"


class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"))
    name: Mapped[str] = mapped_column(String(80))
    duration: Mapped[int] = mapped_column(Integer)  # minutes
    price: Mapped[int] = mapped_column(Integer)  # so'm
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Appointment(Base):
    """status: booked -> confirmed -> done | no_show | cancelled.
    client_id is NULL for anonymous walk-ins."""

    __tablename__ = "appointments"
    __table_args__ = (
        Index("ix_appt_barber_start", "barber_id", "start_at"),
        Index("ix_appt_shop_start", "shop_id", "start_at"),
        Index("ix_appt_client", "client_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"))
    barber_id: Mapped[int] = mapped_column(ForeignKey("barbers.id"))
    client_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    start_at: Mapped[datetime] = mapped_column(DateTime)
    end_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="booked")
    price: Mapped[int | None] = mapped_column(Integer, default=None)
    tip: Mapped[int] = mapped_column(Integer, default=0)
    reminded_24: Mapped[bool] = mapped_column(Boolean, default=False)
    reminded_2: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    barber: Mapped[Barber] = relationship(lazy="joined")
    client: Mapped[User | None] = relationship(lazy="joined")
    service: Mapped[Service] = relationship(lazy="joined")


class Block(Base):
    """Barber's break / day off."""

    __tablename__ = "blocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    barber_id: Mapped[int] = mapped_column(ForeignKey("barbers.id"))
    start_at: Mapped[datetime] = mapped_column(DateTime)
    end_at: Mapped[datetime] = mapped_column(DateTime)


class ClientNote(Base):
    """Client card entries: text notes ("#2 sides, scissors top") and photos of cuts."""

    __tablename__ = "client_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    barber_id: Mapped[int | None] = mapped_column(ForeignKey("barbers.id"), default=None)
    text: Mapped[str | None] = mapped_column(Text, default=None)
    photo_file_id: Mapped[str | None] = mapped_column(String(255), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Shift(Base):
    """Barber check-in / check-out."""

    __tablename__ = "shifts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    barber_id: Mapped[int] = mapped_column(ForeignKey("barbers.id"), index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime)
    end_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
