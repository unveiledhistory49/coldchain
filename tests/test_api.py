"""API tests: TestClient with :memory: override."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from coldchain.api import create_app
from coldchain.auth import new_api_key
from coldchain.db import create_app_engine, create_session_factory, init_db
from coldchain.deps import get_db
from coldchain.models import Organization

BASE = 1_712_000_000_000
MIN = 60_000


def make_client() -> tuple[TestClient, str, dict[str, str]]:
    engine = create_app_engine("sqlite:///:memory:")
    init_db(engine)
    factory = create_session_factory(engine)

    app = create_app()

    def _override() -> Any:
        db = factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    client = TestClient(app)
    db2: Session = factory()
    raw, digest, prefix = new_api_key()
    org = Organization(name="t", api_key_hash=digest, key_prefix=prefix)
    db2.add(org)
    db2.commit()
    org_id = str(org.id)
    db2.close()
    return (client, org_id, {"X-API-Key": raw})


def _mk_device(client: TestClient, h: dict[str, str], serial: str = "SN-1") -> dict[str, Any]:
    r = client.post("/v1/devices", json={"serial": serial}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


def _mk_cal(
    client: TestClient, h: dict[str, str], dev_id: str, frm: int = 0, to: int = 9_000_000_000_000
) -> None:
    r = client.post(
        f"/v1/devices/{dev_id}/calibrations",
        json={"valid_from_ms": frm, "valid_until_ms": to},
        headers=h,
    )
    assert r.status_code == 201, r.text


def _mk_profile(
    client: TestClient,
    h: dict[str, str],
    name: str = "P",
    tmin: str = "2",
    tmax: str = "8",
    budget: int = 30,
) -> dict[str, Any]:
    r = client.post(
        "/v1/profiles",
        json={
            "name": name,
            "temp_min": tmin,
            "temp_max": tmax,
            "unit": "C",
            "max_excursion_minutes": budget,
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


def _mk_shipment(
    client: TestClient,
    h: dict[str, str],
    ref: str,
    profile_id: str,
    device_ids: list[str],
    started: int = 0,
) -> dict[str, Any]:
    r = client.post(
        "/v1/shipments",
        json={
            "reference": ref,
            "profile_id": profile_id,
            "started_at_ms": started,
            "device_ids": device_ids,
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


def test_health_no_auth() -> None:
    client, _, _ = make_client()
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert "X-Request-ID" in r.headers


def test_401() -> None:
    client, _, _ = make_client()
    assert client.get("/v1/devices").status_code == 401
    assert client.get("/v1/devices", headers={"X-API-Key": "bad"}).status_code == 401


def test_device_profile_shipment_flow() -> None:
    client, _, h = make_client()
    dev = _mk_device(client, h, "SN-1")
    _mk_cal(client, h, dev["id"])
    prof = _mk_profile(client, h, "P1")
    ship = _mk_shipment(client, h, "S-1", prof["id"], [dev["id"]])
    assert ship["reference"] == "S-1"
    r = client.get(f"/v1/shipments/{ship['id']}", headers=h)
    assert r.status_code == 200
    assert "stats" in r.json()
    r2 = client.get(f"/v1/devices/{dev['id']}/readings", headers=h)
    assert r2.status_code == 200


def test_reading_lifecycle() -> None:
    client, _, h = make_client()
    dev = _mk_device(client, h, "SN-A")
    _mk_cal(client, h, dev["id"])
    body = {
        "device_id": dev["id"],
        "seq": 0,
        "ts_ms": BASE,
        "temperature": "5.0",
        "unit": "C",
    }
    r = client.post("/v1/readings", json=body, headers=h)
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    # duplicate replay
    r2 = client.post("/v1/readings", json=body, headers=h)
    assert r2.status_code == 200
    assert r2.json()["duplicate"] is True
    assert r2.json()["id"] == rid
    # seq conflict
    bad = dict(body)
    bad["temperature"] = "6.0"
    r3 = client.post("/v1/readings", json=bad, headers=h)
    assert r3.status_code == 422
    # retired
    assert client.post(f"/v1/devices/{dev['id']}/retire", headers=h).status_code == 200
    nxt = dict(body)
    nxt["seq"] = 1
    r4 = client.post("/v1/readings", json=nxt, headers=h)
    assert r4.status_code == 409
    # uncalibrated quarantined
    dev2 = _mk_device(client, h, "SN-B")
    qbody = {
        "device_id": dev2["id"],
        "seq": 0,
        "ts_ms": BASE,
        "temperature": "5.0",
        "unit": "C",
    }
    rq = client.post("/v1/readings", json=qbody, headers=h)
    assert rq.status_code == 201
    assert rq.json().get("quarantined") is True


def test_batch_mixed() -> None:
    client, _, h = make_client()
    dev = _mk_device(client, h, "SN-X")
    _mk_cal(client, h, dev["id"])
    items = [
        {"device_id": dev["id"], "seq": 0, "ts_ms": BASE, "temperature": "5.0"},
        {"device_id": dev["id"], "seq": 0, "ts_ms": BASE, "temperature": "5.0"},
        {"device_id": "nope", "seq": 0, "ts_ms": BASE, "temperature": "5.0"},
    ]
    r = client.post("/v1/readings/batch", json={"items": items}, headers=h)
    assert r.status_code == 200
    out = r.json()["items"]
    assert out[0]["ok"] is True
    assert out[1]["ok"] is True
    assert out[2]["ok"] is False


def test_supersede() -> None:
    client, _, h = make_client()
    dev = _mk_device(client, h, "SN-S")
    _mk_cal(client, h, dev["id"])
    r = client.post(
        "/v1/readings",
        json={"device_id": dev["id"], "seq": 0, "ts_ms": BASE, "temperature": "5.0"},
        headers=h,
    )
    rid = r.json()["id"]
    rs = client.post(
        f"/v1/readings/{rid}/supersede",
        json={"temperature": "6.0", "unit": "C", "reason": "probe offset"},
        headers=h,
    )
    assert rs.status_code == 200, rs.text
    assert rs.json()["temp_mc"] == 6000


def test_excursion_open_close() -> None:
    client, _, h = make_client()
    dev = _mk_device(client, h, "SN-E")
    _mk_cal(client, h, dev["id"])
    prof = _mk_profile(client, h, "PE", budget=30)
    ship = _mk_shipment(client, h, "SE", prof["id"], [dev["id"]], started=BASE - 60 * MIN)
    # out of range -> open
    ro = client.post(
        "/v1/readings",
        json={"device_id": dev["id"], "seq": 0, "ts_ms": BASE, "temperature": "20.0"},
        headers=h,
    )
    assert ro.status_code == 201
    rexc = client.get(f"/v1/excursions?shipment_id={ship['id']}&status=open", headers=h)
    assert rexc.status_code == 200
    assert len(rexc.json()["items"]) == 1
    # back in range -> close
    rc = client.post(
        "/v1/readings",
        json={"device_id": dev["id"], "seq": 1, "ts_ms": BASE + 5 * MIN, "temperature": "5.0"},
        headers=h,
    )
    assert rc.status_code == 201
    rclosed = client.get(f"/v1/excursions?shipment_id={ship['id']}&status=closed", headers=h)
    assert len(rclosed.json()["items"]) == 1


def test_certificate_and_approve() -> None:
    client, _, h = make_client()
    dev = _mk_device(client, h, "SN-C")
    _mk_cal(client, h, dev["id"])
    prof = _mk_profile(client, h, "PC", budget=30)
    ship = _mk_shipment(client, h, "SC", prof["id"], [dev["id"]], started=BASE)
    client.post(
        "/v1/readings",
        json={"device_id": dev["id"], "seq": 0, "ts_ms": BASE + MIN, "temperature": "5.0"},
        headers=h,
    )
    rc = client.post(
        f"/v1/shipments/{ship['id']}/complete", json={"ended_at_ms": BASE + 60 * MIN}, headers=h
    )
    assert rc.status_code == 200
    cert = client.get(f"/v1/reports/shipments/{ship['id']}/certificate", headers=h)
    assert cert.status_code == 200
    assert cert.json()["verdict"] == "pass"
    sh = cert.json()["statement_hash"]
    # approve happy
    ra = client.post(
        f"/v1/reports/shipments/{ship['id']}/approve",
        json={
            "signer_name": "Ada",
            "signer_email": "ada@example.com",
            "meaning": "approved",
            "statement_hash": sh,
        },
        headers=h,
    )
    assert ra.status_code == 201, ra.text
    # stale hash
    rb = client.post(
        f"/v1/reports/shipments/{ship['id']}/approve",
        json={
            "signer_name": "Ada",
            "signer_email": "ada@example.com",
            "meaning": "approved",
            "statement_hash": "00" * 32,
        },
        headers=h,
    )
    assert rb.status_code == 409
    assert rb.json().get("stale") is True or "stale" in str(rb.text).lower()


def test_audit_and_lock() -> None:
    client, _, h = make_client()
    dev = _mk_device(client, h, "SN-L")
    _mk_cal(client, h, dev["id"])
    client.post(
        "/v1/readings",
        json={"device_id": dev["id"], "seq": 0, "ts_ms": BASE, "temperature": "5.0"},
        headers=h,
    )
    ra = client.get("/v1/audit", headers=h)
    assert ra.status_code == 200
    assert len(ra.json()["items"]) >= 1
    rv = client.get("/v1/audit/verify", headers=h)
    assert rv.status_code == 200
    assert rv.json()["ok"] is True
    # lock current month blocks later ingest
    now_ms = int(time.time() * 1000)
    dt = datetime.fromtimestamp(now_ms / 1000, tz=UTC)
    period = f"{dt.year:04d}-{dt.month:02d}"
    rl = client.post("/v1/periods/lock", json={"period": period}, headers=h)
    assert rl.status_code == 201, rl.text
    # ingest with ts in locked period -> 409
    seq_body = {
        "device_id": dev["id"],
        "seq": 1,
        "ts_ms": now_ms,
        "temperature": "5.0",
    }
    rlocked = client.post("/v1/readings", json=seq_body, headers=h)
    assert rlocked.status_code == 409


def test_webhook_https() -> None:
    client, _, h = make_client()
    bad = client.post(
        "/v1/webhook-endpoints",
        json={"url": "http://example.com/hook", "secret": "s"},
        headers=h,
    )
    assert bad.status_code == 422
    good = client.post(
        "/v1/webhook-endpoints",
        json={"url": "https://example.com/hook", "secret": "s"},
        headers=h,
    )
    assert good.status_code == 201
    local = client.post(
        "/v1/webhook-endpoints",
        json={"url": "http://localhost:9999/hook", "secret": "s"},
        headers=h,
    )
    assert local.status_code == 201
    d = client.post("/v1/webhooks/dispatch", headers=h)
    assert d.status_code == 200
    assert "dispatched" in d.json()


def test_idempotency_keys() -> None:
    client, _, h = make_client()
    hk = dict(h)
    hk["Idempotency-Key"] = "testkey-12345678"
    r1 = client.post("/v1/devices", json={"serial": "SN-IDEM"}, headers=hk)
    assert r1.status_code == 201
    r2 = client.post("/v1/devices", json={"serial": "SN-IDEM"}, headers=hk)
    assert r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]
    # conflicting payload same key -> 409
    r3 = client.post("/v1/devices", json={"serial": "OTHER"}, headers=hk)
    assert r3.status_code == 409
    # short key -> 422
    hk2 = dict(h)
    hk2["Idempotency-Key"] = "short"
    assert client.post("/v1/devices", json={"serial": "Z"}, headers=hk2).status_code == 422


def test_statement_hash_stable_and_incomplete_unapprovable() -> None:
    client, _, h = make_client()
    dev = _mk_device(client, h, "SN-STABLE")
    _mk_cal(client, h, dev["id"])
    prof = _mk_profile(client, h, "PS", budget=30)
    # In-transit records cannot be signed.
    ship = _mk_shipment(client, h, "SSTABLE", prof["id"], [dev["id"]], started=BASE)
    c1 = client.get(f"/v1/reports/shipments/{ship['id']}/certificate", headers=h)
    assert c1.status_code == 200
    assert c1.json()["verdict"] == "incomplete"
    ra = client.post(
        f"/v1/reports/shipments/{ship['id']}/approve",
        json={
            "signer_name": "Ada",
            "signer_email": "ada@example.com",
            "meaning": "approved",
            "statement_hash": c1.json()["statement_hash"],
        },
        headers=h,
    )
    assert ra.status_code == 422, ra.text
    # Completed records are content-stable: same hash on repeat reads
    # (window end is fixed at completion, so the evidence window cannot move).
    ship2 = _mk_shipment(client, h, "SSTABLE2", prof["id"], [dev["id"]], started=BASE)
    client.post(
        "/v1/readings",
        json={"device_id": dev["id"], "seq": 0, "ts_ms": BASE + MIN, "temperature": "5.0"},
        headers=h,
    )
    rc = client.post(
        f"/v1/shipments/{ship2['id']}/complete", json={"ended_at_ms": BASE + 60 * MIN}, headers=h
    )
    assert rc.status_code == 200
    h1 = client.get(f"/v1/reports/shipments/{ship2['id']}/certificate", headers=h).json()
    h2 = client.get(f"/v1/reports/shipments/{ship2['id']}/certificate", headers=h).json()
    assert h1["verdict"] == "pass"
    assert h1["statement_hash"] == h2["statement_hash"]
