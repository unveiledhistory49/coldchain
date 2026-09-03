# ADR 0002 — Device-side sequence idempotency

Status: accepted.

## Context

Sensors report over unreliable links (cellular dead zones, store-and-forward
gateways). Retries and duplicate deliveries are the norm, and devices may be
offline for hours. The ingest path must accept the same reading twice without
double-counting, while refusing genuinely conflicting data.

## Decision

Uniqueness is `(device_id, seq)` (`uq_readings_device_seq` in `models.py`;
ingest in `ingest.py`):

- **Same `(device, seq)` + identical payload** (`ts_ms`, `temp_mc`,
  humidity, battery) → idempotent replay: the existing row is returned with
  outcome `"duplicate"` (single-reading route answers `200` with
  `"duplicate": true`).
- **Same `(device, seq)` + different payload** → `InvalidReading("seq
  conflict: payload differs under same seq ...")`, mapped to HTTP `422`.
  The stored row is untouched; the sender must advance `seq`.
- Sequence allocation is device-side (0-based; `device_max_seq` returns -1
  when empty). The server never assigns sequence numbers.

Batch ingest (`POST /v1/readings/batch`) reports per-item outcomes and
additionally supports a client `Idempotency-Key` header for safe batch
retries (same key + same body replays the stored response; same key +
different body is rejected).

## Why device-side seq beats server UUIDs

- **Offline devices.** A sensor without connectivity cannot ask the server
  for an ID; a monotonic counter works with zero coordination.
- **Retry safety.** "Send seq N until acked, then move to N+1" is a complete
  at-least-once protocol implementable on an 8-bit microcontroller. Server
  UUIDs would require the device to persist server responses across reboots.
- **Conflict detection.** A UUID-per-attempt scheme treats a retry as a new
  reading (double-count) unless the device echoes a client-generated UUID —
  which is exactly a sequence number with extra steps. Reusing `(device,
  seq)` makes the dedup key visible in the data model and queryable
  (`GET /v1/devices/{id}/readings`).
- **Ordering.** Sequence numbers give excursion evaluation a stable causal
  order even when deliveries arrive late or out of order.

## Consequences

Devices must persist their counter across reboots; a device that resets its
counter will hit seq conflicts (fail-closed, `422`) rather than silently
duplicating history. Operators resolve this by retiring the device identity
and registering a fresh serial.
