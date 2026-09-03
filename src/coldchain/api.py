"""FastAPI application factory."""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from coldchain import ingest as ingest_mod
from coldchain.compliance import ComplianceError
from coldchain.excursions import ExcursionError
from coldchain.routers import (
    audit,
    devices,
    excursions,
    misc,
    profiles,
    readings,
    reports_extra,
    shipments,
)
from coldchain.temp import TempError


def _ingest_status(exc: ingest_mod.IngestError) -> int:
    if isinstance(exc, ingest_mod.UnknownDevice):
        return 404
    if isinstance(exc, ingest_mod.RetiredDevice):
        return 409
    if isinstance(exc, ingest_mod.LockedPeriod):
        return 409
    if isinstance(exc, ingest_mod.InvalidReading):
        msg = str(exc).lower()
        # Device-serial duplicates are conflicts; seq conflicts are validation.
        if "device conflict" in msg or "(org, serial)" in msg or "already exists" in msg:
            return 409
        return 422
    return 422


def _compliance_status(exc: ComplianceError) -> int:
    msg = str(exc).lower()
    if "already locked" in msg or "already" in msg:
        return 409
    if "invalid" in msg or "must be" in msg or "required" in msg or "non-empty" in msg:
        # Validation-style errors -> 422, except duplicates.
        if "already" in msg:
            return 409
        return 422
    return 409


def create_app() -> FastAPI:
    app = FastAPI(title="ColdChain")

    @app.middleware("http")
    async def _request_id(request: Request, call_next: Any) -> Any:
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    @app.exception_handler(ingest_mod.IngestError)
    async def _ingest_handler(request: Request, exc: ingest_mod.IngestError) -> JSONResponse:
        return JSONResponse(status_code=_ingest_status(exc), content={"detail": str(exc)})

    @app.exception_handler(ComplianceError)
    async def _compliance_handler(request: Request, exc: ComplianceError) -> JSONResponse:
        return JSONResponse(status_code=_compliance_status(exc), content={"detail": str(exc)})

    @app.exception_handler(TempError)
    async def _temp_handler(request: Request, exc: TempError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(ExcursionError)
    async def _excursion_handler(request: Request, exc: ExcursionError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def _value_handler(request: Request, exc: ValueError) -> JSONResponse:
        # Used for unknown shipment in reports; keep narrow to avoid masking bugs.
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(IntegrityError)
    async def _integrity_handler(request: Request, exc: IntegrityError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": "conflict"})

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "time_ms": int(time.time() * 1000)}

    app.include_router(devices.router)
    app.include_router(profiles.router)
    app.include_router(shipments.router)
    app.include_router(readings.router)
    app.include_router(excursions.router)
    app.include_router(reports_extra.router)
    app.include_router(audit.router)
    app.include_router(misc.router)
    return app
