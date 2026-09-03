# ADR 0001 — Write-once readings

Status: accepted.

## Context

ColdChain stores sensor evidence used in compliance certificates
(pharma/food/biologics). Regulators and customers must be able to trust that
a temperature history has not been silently rewritten after the fact. At the
same time, real sensors produce bad values that need correction
(miscalibrated probe, transcription error, wrong unit).

## Decision

Measurement columns on a reading row (`device_id`, `seq`, `ts_ms`,
`temp_mc`, `humidity_bp`, `battery_mv`) are **never `UPDATE`d**. Only the
lifecycle `status` column changes, and only along
`accepted → superseded` (or initial `accepted` / `quarantined` at ingest).

Corrections go through `supersede_reading` (`ingest.py`), which:

1. Requires the original row to be `accepted` (quarantined rows are evidence
   as-is; only accepted readings can be superseded).
2. Requires a non-empty `reason`.
3. Allocates a **new row** with `seq = device_max_seq + 1` and
   `flags = "supersedes:<original-id>"`, `supersedes_id` set.
4. Flips the original row's `status` to `"superseded"` — the single allowed
   mutation of an existing row.
5. Refuses to supersede readings inside a locked period (`409 LockedPeriod`).

`models.py` documents the same invariant at the table level.

## Why

- **Compliance evidence.** An auditor can select every row for a device and
  see the full history, including what was corrected, when, and why — the
  original value is never destroyed.
- **Hash stability.** Certificates and period locks digest concrete row
  sets; in-place mutation would silently invalidate (or falsely preserve)
  previously computed hashes.
- **Simple reasoning.** "Rows are append-only" is enforceable by review
  (`grep UPDATE` finds nothing touching measurement columns) rather than by
  trusting application discipline.

## Rejected alternative: in-place correction with a history table

Correct the row and write the old value to a `reading_history` table. This
keeps the "current" query simple but splits truth across two tables: every
consumer (excursions, MKT, certificates, locks) must remember to union or
exclude history, and a bug that reads only the live table silently hides
corrections. The append-only design makes the wrong thing (losing history)
structurally impossible instead of merely discouraged.
