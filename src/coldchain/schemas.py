"""Pydantic request schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class DeviceCreate(BaseModel):
    serial: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=200)
    model: str = Field(default="", max_length=100)


class CalibrationCreate(BaseModel):
    valid_from_ms: int = Field(ge=0)
    valid_until_ms: int = Field(ge=0)
    certificate_ref: str = Field(default="", max_length=120)


class ProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    temp_min: str = Field(min_length=1)
    temp_max: str = Field(min_length=1)
    unit: str = Field(default="C")
    max_excursion_minutes: int = Field(ge=0)
    mkt_limit: str | None = Field(default=None)
    mkt_unit: str = Field(default="C")


class ShipmentCreate(BaseModel):
    reference: str = Field(min_length=1, max_length=120)
    profile_id: str = Field(min_length=1)
    origin: str = Field(default="", max_length=200)
    destination: str = Field(default="", max_length=200)
    started_at_ms: int = Field(default=0, ge=0)
    device_ids: list[str] = Field(default_factory=list)


class ReadingCreate(BaseModel):
    device_id: str = Field(min_length=1)
    seq: int = Field(ge=0)
    ts_ms: int = Field(gt=0)
    temperature: str = Field(min_length=1)
    unit: str = Field(default="C")
    humidity_pct: float | None = Field(default=None, ge=0, le=100)
    battery_mv: int | None = Field(default=None, ge=0)


class BatchCreate(BaseModel):
    items: list[ReadingCreate] = Field(min_length=1)


class SupersedeCreate(BaseModel):
    temperature: str = Field(min_length=1)
    unit: str = Field(default="C")
    reason: str = Field(min_length=1)


class ApproveCreate(BaseModel):
    signer_name: str = Field(min_length=1, max_length=200)
    signer_email: str = Field(min_length=1, max_length=200)
    meaning: str = Field(min_length=1, max_length=200)
    statement_hash: str | None = Field(default=None)

    @field_validator("signer_email")
    @classmethod
    def _email(cls, v: str) -> str:
        if "@" not in v:
            raise ValueError("invalid email")
        return v


class LockCreate(BaseModel):
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")


class WebhookCreate(BaseModel):
    url: str = Field(min_length=1, max_length=1000)
    secret: str = Field(min_length=1, max_length=128)


class CompleteCreate(BaseModel):
    ended_at_ms: int = Field(default=0, ge=0)
