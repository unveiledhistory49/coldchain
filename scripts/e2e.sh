#!/usr/bin/env bash
# ColdChain end-to-end smoke test (no jq dependency).
#
# Flow: tmp sqlite -> provision-org -> serve -> device + calibration +
# profile + shipment -> readings (one out-of-range) -> excursion visible ->
# duplicate replay -> complete -> certificate (verdict=pass) -> approve (201)
# -> audit verify (ok=true).
#
# Request shapes mirror tests/test_api.py helpers (_mk_device, _mk_cal,
# _mk_profile, _mk_shipment, test_reading_lifecycle, test_excursion_open_close,
# test_certificate_and_approve, test_audit_and_lock).
set -euo pipefail

PYTHON=${PYTHON:-python3}
PORT=${PORT:-8000}
BASE_URL="http://127.0.0.1:${PORT}"

TMPDIR=$(mktemp -d)
DB_URL="sqlite:///${TMPDIR}/e2e.db"
PID=""

cleanup() {
  if [ -n "${PID:-}" ]; then
    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
  fi
  rm -rf "$TMPDIR"
}
trap cleanup EXIT

need_code() {
  local want="$1" got="$2" what="$3" body="$4"
  if [ "$got" != "$want" ]; then
    echo "FAIL: ${what}: want HTTP ${want}, got ${got}" >&2
    cat "$body" >&2
    exit 1
  fi
  echo "ok: ${what} (${got})"
}

# --- provision org (CLI output parsed with python3, no jq) ---
"$PYTHON" -m coldchain.cli provision-org --name e2e --db "$DB_URL" > "$TMPDIR/org.json"
ORG_ID=$("$PYTHON" -c "import json; print(json.load(open('$TMPDIR/org.json'))['org_id'])")
API_KEY=$("$PYTHON" -c "import json; print(json.load(open('$TMPDIR/org.json'))['api_key'])")
AUTH="X-API-Key: ${API_KEY}"

# --- serve in background ---
"$PYTHON" -m coldchain.cli serve --db "$DB_URL" --host 127.0.0.1 --port "$PORT" \
  > "$TMPDIR/serve.log" 2>&1 &
PID=$!

# --- wait for /health (explicit failure, no `cmd && break` under set -e) ---
READY=0
i=0
while [ "$i" -lt 50 ]; do
  if curl -fsS "${BASE_URL}/health" > /dev/null 2>&1; then
    READY=1
    break
  fi
  i=$((i + 1))
  sleep 0.2
done
if [ "$READY" != "1" ]; then
  echo "FAIL: server did not become ready on ${BASE_URL}/health" >&2
  cat "$TMPDIR/serve.log" >&2
  exit 1
fi
echo "ok: server ready"

# --- register device via curl ---
code=$(curl -sS -o "$TMPDIR/dev.json" -w "%{http_code}" -X POST "${BASE_URL}/v1/devices" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"serial":"SN-E2E-1"}')
need_code 201 "$code" "register device" "$TMPDIR/dev.json"
DEV_ID=$("$PYTHON" -c "import json; print(json.load(open('$TMPDIR/dev.json'))['id'])")

# --- calibration ---
code=$(curl -sS -o "$TMPDIR/cal.json" -w "%{http_code}" -X POST \
  "${BASE_URL}/v1/devices/${DEV_ID}/calibrations" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"valid_from_ms":0,"valid_until_ms":9000000000000}')
need_code 201 "$code" "add calibration" "$TMPDIR/cal.json"

# --- temperature profile (2..8 C, 30 min excursion budget) ---
code=$(curl -sS -o "$TMPDIR/prof.json" -w "%{http_code}" -X POST "${BASE_URL}/v1/profiles" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"name":"P-E2E","temp_min":"2","temp_max":"8","unit":"C","max_excursion_minutes":30}')
need_code 201 "$code" "create profile" "$TMPDIR/prof.json"
PROF_ID=$("$PYTHON" -c "import json; print(json.load(open('$TMPDIR/prof.json'))['id'])")

# --- shipment ---
T0_MS=$("$PYTHON" -c "import time; print(int(time.time()*1000))")
code=$(curl -sS -o "$TMPDIR/ship.json" -w "%{http_code}" -X POST "${BASE_URL}/v1/shipments" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"reference\":\"S-E2E\",\"profile_id\":\"${PROF_ID}\",\"started_at_ms\":${T0_MS},\"device_ids\":[\"${DEV_ID}\"]}")
need_code 201 "$code" "create shipment" "$TMPDIR/ship.json"
SHIP_ID=$("$PYTHON" -c "import json; print(json.load(open('$TMPDIR/ship.json'))['id'])")

post_reading() {
  local seq="$1" ts="$2" temp="$3" out="$4"
  curl -sS -o "$out" -w "%{http_code}" -X POST "${BASE_URL}/v1/readings" \
    -H "$AUTH" -H "Content-Type: application/json" \
    -d "{\"device_id\":\"${DEV_ID}\",\"seq\":${seq},\"ts_ms\":${ts},\"temperature\":\"${temp}\",\"unit\":\"C\"}"
}

# --- in-range reading ---
code=$(post_reading 0 "$T0_MS" "5.0" "$TMPDIR/r0.json")
need_code 201 "$code" "post reading seq=0 (in range)" "$TMPDIR/r0.json"

# --- out-of-range reading (opens excursion) ---
code=$(post_reading 1 "$((T0_MS + 60000))" "20.0" "$TMPDIR/r1.json")
need_code 201 "$code" "post reading seq=1 (out of range)" "$TMPDIR/r1.json"
R1_ID=$("$PYTHON" -c "import json; print(json.load(open('$TMPDIR/r1.json'))['id'])")

# --- excursion must be visible as open ---
code=$(curl -sS -o "$TMPDIR/exc.json" -w "%{http_code}" \
  "${BASE_URL}/v1/excursions?shipment_id=${SHIP_ID}&status=open" -H "$AUTH")
need_code 200 "$code" "list open excursions" "$TMPDIR/exc.json"
"$PYTHON" - "$TMPDIR/exc.json" <<'EOF'
import json, sys
items = json.load(open(sys.argv[1]))["items"]
assert len(items) >= 1, f"expected >=1 open excursion, got {len(items)}"
print(f"ok: excursion open ({len(items)} item(s))")
EOF

# --- duplicate replay of seq=1: 200 + duplicate=true + same id ---
code=$(post_reading 1 "$((T0_MS + 60000))" "20.0" "$TMPDIR/r1dup.json")
need_code 200 "$code" "duplicate replay seq=1" "$TMPDIR/r1dup.json"
"$PYTHON" - "$TMPDIR/r1dup.json" "$R1_ID" <<'EOF'
import json, sys
body = json.load(open(sys.argv[1]))
assert body.get("duplicate") is True, f"expected duplicate=true: {body}"
assert body["id"] == sys.argv[2], f"id mismatch: {body['id']} != {sys.argv[2]}"
print("ok: duplicate replay returns same id")
EOF

# --- back in range (closes excursion) ---
code=$(post_reading 2 "$((T0_MS + 300000))" "5.0" "$TMPDIR/r2.json")
need_code 201 "$code" "post reading seq=2 (in range)" "$TMPDIR/r2.json"

# --- complete shipment ---
code=$(curl -sS -o "$TMPDIR/complete.json" -w "%{http_code}" -X POST \
  "${BASE_URL}/v1/shipments/${SHIP_ID}/complete" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"ended_at_ms\":$((T0_MS + 3600000))}")
need_code 200 "$code" "complete shipment" "$TMPDIR/complete.json"

# --- certificate verdict must be pass ---
code=$(curl -sS -o "$TMPDIR/cert.json" -w "%{http_code}" \
  "${BASE_URL}/v1/reports/shipments/${SHIP_ID}/certificate" -H "$AUTH")
need_code 200 "$code" "get certificate" "$TMPDIR/cert.json"
STMT_HASH=$("$PYTHON" - "$TMPDIR/cert.json" <<'EOF'
import json, sys
cert = json.load(open(sys.argv[1]))
assert cert["verdict"] == "pass", f"expected verdict=pass: {cert}"
print(cert["statement_hash"])
EOF
)
echo "ok: certificate verdict=pass"

# --- approve with current statement hash: 201 ---
code=$(curl -sS -o "$TMPDIR/approve.json" -w "%{http_code}" -X POST \
  "${BASE_URL}/v1/reports/shipments/${SHIP_ID}/approve" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"signer_name\":\"Ada\",\"signer_email\":\"ada@example.com\",\"meaning\":\"approved\",\"statement_hash\":\"${STMT_HASH}\"}")
need_code 201 "$code" "approve certificate" "$TMPDIR/approve.json"

# --- audit chain must verify ---
code=$(curl -sS -o "$TMPDIR/audit.json" -w "%{http_code}" \
  "${BASE_URL}/v1/audit/verify" -H "$AUTH")
need_code 200 "$code" "audit verify" "$TMPDIR/audit.json"
"$PYTHON" - "$TMPDIR/audit.json" <<'EOF'
import json, sys
body = json.load(open(sys.argv[1]))
assert body.get("ok") is True, f"expected ok=true: {body}"
print("ok: audit chain verifies")
EOF

echo "E2E PASS"
