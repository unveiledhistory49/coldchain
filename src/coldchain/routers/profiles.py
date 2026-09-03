"""Profile routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from coldchain.deps import get_db, require_org
from coldchain.models import Organization, TempProfile
from coldchain.schemas import ProfileCreate
from coldchain.temp import TempError, parse_temp_to_mc

router = APIRouter(tags=["profiles"])


def _profile_to_dict(p: TempProfile) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "temp_min_mc": p.temp_min_mc,
        "temp_max_mc": p.temp_max_mc,
        "max_excursion_minutes": p.max_excursion_minutes,
        "mkt_limit_mc": p.mkt_limit_mc,
    }


@router.post("/v1/profiles", status_code=201)
def create_profile(
    payload: ProfileCreate,
    db: Session = Depends(get_db),
    org: Organization = Depends(require_org),
) -> dict[str, Any]:
    try:
        tmin = parse_temp_to_mc(payload.temp_min, payload.unit)
        tmax = parse_temp_to_mc(payload.temp_max, payload.unit)
    except TempError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if tmin >= tmax:
        raise HTTPException(status_code=422, detail="temp_min must be < temp_max")
    mkt_limit_mc: int | None = None
    if payload.mkt_limit is not None:
        try:
            mkt_limit_mc = parse_temp_to_mc(payload.mkt_limit, payload.mkt_unit)
        except TempError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
    prof = TempProfile(
        org_id=org.id,
        name=payload.name,
        temp_min_mc=tmin,
        temp_max_mc=tmax,
        max_excursion_minutes=payload.max_excursion_minutes,
        mkt_limit_mc=mkt_limit_mc,
    )
    db.add(prof)
    try:
        db.flush()
    except IntegrityError as e:
        raise HTTPException(status_code=409, detail="profile name conflict") from e
    return _profile_to_dict(prof)


@router.get("/v1/profiles")
def list_profiles(
    db: Session = Depends(get_db),
    org: Organization = Depends(require_org),
) -> dict[str, Any]:
    rows = db.execute(select(TempProfile).where(TempProfile.org_id == org.id)).scalars().all()
    return {"items": [_profile_to_dict(p) for p in rows]}
