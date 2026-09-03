"""SQLAlchemy domain models. Readings are write-once: no code path may UPDATE
measurement columns (device/seq/ts/temp). Corrections create superseding rows;
`status` is lifecycle metadata, not measurement data (see ADR-0001)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.utcnow()


class Base(DeclarativeBase):
    pass


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    api_key_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("org_id", "serial", name="uq_devices_org_serial"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    serial: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    model: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class Calibration(Base):
    __tablename__ = "calibrations"
    __table_args__ = (
        Index("ix_cal_device_window", "device_id", "valid_from_ms", "valid_until_ms"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    valid_from_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    valid_until_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    certificate_ref: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class TempProfile(Base):
    __tablename__ = "temp_profiles"
    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_profiles_org_name"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    temp_min_mc: Mapped[int] = mapped_column(Integer, nullable=False)
    temp_max_mc: Mapped[int] = mapped_column(Integer, nullable=False)
    max_excursion_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    mkt_limit_mc: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class Shipment(Base):
    __tablename__ = "shipments"
    __table_args__ = (UniqueConstraint("org_id", "reference", name="uq_shipments_org_ref"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reference: Mapped[str] = mapped_column(String(120), nullable=False)
    profile_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("temp_profiles.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="in_transit")
    origin: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    destination: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    started_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    ended_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)

    devices: Mapped[list[ShipmentDevice]] = relationship(
        back_populates="shipment", cascade="all, delete-orphan"
    )


class ShipmentDevice(Base):
    __tablename__ = "shipment_devices"
    shipment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("shipments.id", ondelete="CASCADE"), primary_key=True
    )
    device_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("devices.id", ondelete="RESTRICT"), primary_key=True
    )

    shipment: Mapped[Shipment] = relationship(back_populates="devices")


class Reading(Base):
    __tablename__ = "readings"
    __table_args__ = (
        UniqueConstraint("device_id", "seq", name="uq_readings_device_seq"),
        Index("ix_readings_device_ts", "device_id", "ts_ms"),
        Index("ix_readings_org_ts", "org_id", "ts_ms"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("devices.id", ondelete="RESTRICT"), nullable=False
    )
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ts_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    temp_mc: Mapped[int] = mapped_column(Integer, nullable=False)
    humidity_bp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    battery_mv: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    flags: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="accepted")
    supersedes_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("readings.id"), nullable=True, default=None
    )
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class Excursion(Base):
    __tablename__ = "excursions"
    __table_args__ = (Index("ix_exc_ship_status", "shipment_id", "status"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    shipment_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("shipments.id", ondelete="CASCADE"), nullable=True, default=None
    )
    device_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("devices.id", ondelete="RESTRICT"), nullable=False
    )
    profile_id: Mapped[str] = mapped_column(String(36), nullable=False)
    started_ts_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ended_ts_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    peak_mc: Mapped[int] = mapped_column(Integer, nullable=False)
    trough_mc: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_org_seq", "org_id", "seq"),)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    target: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class Approval(Base):
    """Part-11-style electronic signature on a report hash (meaning + identity)."""

    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    report_type: Mapped[str] = mapped_column(String(60), nullable=False)
    report_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    statement_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    signer_name: Mapped[str] = mapped_column(String(200), nullable=False)
    signer_email: Mapped[str] = mapped_column(String(200), nullable=False)
    meaning: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class PeriodLock(Base):
    __tablename__ = "period_locks"
    __table_args__ = (UniqueConstraint("org_id", "period", name="uq_locks_org_period"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    readings_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    closed_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("org_id", "key", name="uq_idem_org_key"),)
    org_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    path: Mapped[str] = mapped_column(String(300), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class WebhookEndpoint(Base):
    __tablename__ = "webhook_endpoints"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    secret: Mapped[str] = mapped_column(String(128), nullable=False)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="ck_delivery_attempts"),
        Index("ix_delivery_status_retry", "status", "next_retry_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    endpoint_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("webhook_endpoints.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, nullable=False)
