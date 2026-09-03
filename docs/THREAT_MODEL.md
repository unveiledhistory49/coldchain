# Threat model

Scope: the ColdChain API + CLI + SQLite/Postgres store in
`src/coldchain/`. Each mitigation below was verified by grepping `src`;
residual risks are listed honestly at the end.

## Assets

- **Sensor evidence integrity** — readings, excursions, certificates.
- **API keys** — per-org bearer credentials.
- **Approvals** — Part-11-style electronic signatures binding a signer to a
  report hash.

## Threats and mitigations

### 1. Spoofed / forged readings

- **Seq-conflict detection.** `(device_id, seq)` is unique
  (`uq_readings_device_seq`, `models.py`). A replay with identical payload
  is a harmless `duplicate`; a forged value under a used seq is rejected
  (`InvalidReading("seq conflict...")` → `422`, `ingest.py`, `api.py`).
  An attacker who observes traffic cannot overwrite history — only append
  new seqs.
- **Calibration windows.** Readings with no covering calibration row are
  stored as `quarantined` with flag `uncalibrated` (`ingest.py`
  `calibration_covering`), and any quarantined row in-window forces a
  `fail` verdict (`reports.py`). Spoofing "good" data from an uncalibrated
  device does not yield a passing certificate.
- **Range / skew checks.** Out-of-physical-range temps rejected (`422`,
  `temp.py`); future-dated readings quarantined as `future-skew`
  (`MAX_FUTURE_SKEW_MS`, `ingest.py`).

### 2. API-key leakage

- Keys are stored as `sha256(pepper + "::" + raw)` (`auth.py`), compared
  with `hmac.compare_digest` (`auth.py`, `deps.py`). The raw key is shown
  once at `provision-org` and never persisted.
- Residual: the default pepper is `dev-pepper-change-me` (`config.py`) —
  rotate `COLDCHAIN_API_KEY_PEPPER` in production (see `SECURITY.md`).

### 3. Approval forgery / stale sign-off

- Signatures bind the exact `statement_hash` (content-only sha256,
  `reports.py`); `approve` recomputes and demands an exact match, else
  `409 stale` (`routers/reports_extra.py`). Approving an `incomplete`
  shipment is refused (`422`). `meaning` is restricted to
  `reviewed/approved/verified` and email format is validated
  (`compliance.py`).

### 4. Audit-trail tampering

- Append-only hash chain per org: `hash = sha256(prev, org, actor, action,
  target, detail, seq)` with `genesis` anchor (`compliance.py`).
  `GET /v1/audit/verify` recomputes every link and reports
  `{ok, checked, error}` (`routers/audit.py`).
- Residual: chain integrity is *detectable*, not *preventable*, against an
  attacker with direct DB write access — pair with DB backups and access
  control (see `docs/RUNBOOK.md`).

### 5. Webhook SSRF / credential theft

- Endpoint URLs must be `https` (or `http` only for `localhost`/`127.0.0.1`);
  otherwise `422` (`_url_allowed`, `routers/misc.py`). Payloads are
  HMAC-SHA256 signed with the endpoint secret (`webhooks.py`).

### 6. Backdated ingestion into locked periods

- `period_locked()` is checked on both `ingest_reading` and
  `supersede_reading`; hits raise `LockedPeriod` → HTTP `409` (`ingest.py`,
  `api.py`). Locks snapshot a readings hash and refuse re-lock
  (`compliance.py`).

## Residual risks (honest)

- Single org-wide bearer key: no scopes, rotation, or per-device
  credentials; a leaked key grants full org access until re-provisioned.
- No rate limiting, TLS termination, or request-size hardening in-app —
  deploy behind a reverse proxy.
- Audit `seq` is read-max-then-insert: concurrent writers can collide;
  single-writer deployment assumed.
- Webhook dispatch is operator-triggered (`POST /v1/webhooks/dispatch`),
  so alert latency depends on polling; HMAC secret is stored in plaintext.
- SQLite default has no encryption at rest; file permissions + volume
  encryption are the operator's job.
