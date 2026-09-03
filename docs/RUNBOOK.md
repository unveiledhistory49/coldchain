# Runbook

Conventions: `ORG` = org id, `KEY` = raw API key from provisioning,
`DEV`/`SHIP`/`PROFILE` = resource ids. Auth header on every call:
`H "X-API-Key: $KEY"`. Base URL defaults to `http://127.0.0.1:8000`.

## Provision an org

```bash
coldchain provision-org --name "Acme Pharma"
# -> {"org_id": ..., "api_key": ... (shown ONCE), "key_prefix": ...}
```

Store the key in a secret manager; it is persisted only as
`sha256(pepper::raw)` and cannot be recovered.

## Register a device

```bash
coldchain register-device --org-id "$ORG" --serial "DEV-001" --model "T1000"
```

Serials are unique per org (`409` on duplicate). Retire with
`POST /v1/devices/{id}/retire`; retired devices reject ingest (`409`).

## Calibrate

Readings outside any calibration window are **quarantined**
(`uncalibrated`), so add a window before streaming:

```bash
curl -X POST $BASE/v1/devices/$DEV/calibrations -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{"valid_from_ms":0,"valid_until_ms":9999999999999,"certificate_ref":"CAL-2026-01"}'
```

## Simulate traffic

```bash
coldchain simulate --org-id "$ORG" --devices DEV-001 --minutes 120 \
  --interval-s 60 --seed 7 --base-c 5.0 --excursion 30,20,8.0
# -> {"sent":..,"accepted":..,"duplicates":..,"quarantined":..}
```

`--excursion start_min,duration_min,offset_c` injects a warm bump;
`--dup-rate` replays identical payloads (expect `duplicates`),
`--late-rate` backdates some timestamps (expect `late` flags).

## Certificate + approve flow

```bash
# complete the shipment first — otherwise verdict is "incomplete"
curl -X POST $BASE/v1/shipments/$SHIP/complete -H "$AUTH" -H 'Content-Type: application/json' -d '{}'

curl $BASE/v1/reports/shipments/$SHIP/certificate -H "$AUTH" > cert.json
# inspect .verdict (pass|fail|incomplete), .statement_hash, .reasons

curl -X POST $BASE/v1/reports/shipments/$SHIP/approve -H "$AUTH" \
  -H 'Content-Type: application/json' -d "{
    \"signer_name\":\"Q.A.\", \"signer_email\":\"qa@example.com\",
    \"meaning\":\"approved\",
    \"statement_hash\":\"$(jq -r .statement_hash cert.json)\"}"
# 201 approval | 409 {"stale":true} -> re-fetch cert.json and countersign
# 422 -> certificate is incomplete; resolve reasons first
```

Or via CLI: `coldchain report --org-id "$ORG" --shipment-id "$SHIP"`.

## Lock a period

```bash
coldchain lock-period --org-id "$ORG" --period 2026-08
# or: curl -X POST $BASE/v1/periods/lock -H "$AUTH" -d '{"period":"2026-08"}'
```

After locking, ingest/supersede with `ts_ms` in that month fails `409`.
Re-locking fails `409`. Lock **after** the month's data is final.

## Webhook dispatch

```bash
curl -X POST $BASE/v1/webhook-endpoints -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/hook","secret":"s3cret"}'
# http allowed only for localhost/127.0.0.1, else 422

curl -X POST $BASE/v1/webhooks/dispatch -H "$AUTH"
# -> {"dispatched":..,"failed":..,"pending":..}
```

Dispatch is manual/polling — schedule it (cron every minute). Payloads carry
an HMAC-SHA256 signature computed with the endpoint secret.

## SQLite backup

```bash
sqlite3 coldchain.db ".backup 'coldchain-$(date +%F).db'"
```

For zero-downtime copies prefer `VACUUM INTO` / the `.backup` command over
`cp` (WAL safety). Verify restores by running
`GET /v1/audit/verify` against the copy and expecting `{"ok":true,...}`.

## Triage

| Symptom | Likely cause | Action |
| ------- | ------------ | ------ |
| Reading `quarantined`, reason `uncalibrated` | No calibration window covers `ts_ms` | Add calibration window, re-check |
| Reason `future-skew` | `ts_ms` > now + 5 min | Fix device clock; data retained, flagged |
| Reason `late` (accepted) | `ts_ms` older than previous | Informational flag only |
| `422 seq conflict` | Same seq, different payload (counter reset / bug) | Advance device seq; retire + re-register if counter lost |
| `409 period locked` | Backdate into locked `YYYY-MM` | Ingest into open period or unlock policy (re-provision) |
| `GET /v1/audit/verify` → `ok:false` | Tampering or concurrent-writer seq collision | Stop writers, restore from backup, investigate `error` offset |
| Certificate `fail`, `quarantined reading(s)` | Bad data in window | Supersede with reason or accept fail; never delete |
| Approve `409 stale` | New reading arrived after fetch | Re-fetch certificate, review diff, countersign |
