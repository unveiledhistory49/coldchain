# ADR 0003 — Content-only statement hash

Status: accepted.

## Context

A shipment certificate must be approvable (signed by QA) and the signature
must demonstrably bind to *exactly* what was approved. Certificates also
carry operational metadata (when it was generated, who has signed so far)
that changes without the underlying evidence changing.

## Decision

`shipment_certificate` (`reports.py`) builds a `statement` dict covering
**content only**: shipment identity, profile bounds, reading/excursion
statistics, MKT, calibration coverage, verdict and reasons. It then:

- `statement_hash = sha256(canonical_json(statement))` (sorted keys,
  compact separators);
- attaches `generated_at_ms` and the `approvals` ledger **outside** the
  hashed statement.

Approval (`routers/reports_extra.py` + `compliance.approve_report`):

- Requires an **exact match**: if the caller supplies `statement_hash` and
  it differs from the freshly computed hash → `409 {"stale": true}`.
- Refuses to sign `incomplete` records (shipment not `complete`) → `422`.
- Records signer name/email, `meaning` (one of `reviewed`, `approved`,
  `verified`), and the hash in the approvals ledger — alongside, not inside,
  the statement.

## Why exclude `generated_at_ms` and approvals

- **Stability.** Including `generated_at_ms` would make every fetch produce
  a different hash, so no client could ever present a matching hash to
  approve. Excluding it keeps the hash a pure function of evidence.
- **Countersignatures.** Including the approvals list would make the second
  signer's hash differ from the first's, so multi-party sign-off would be
  unrepresentable. Each approval independently binds the same content hash.

## Rejected alternative: fuzzy / drift-tolerant matching

Accept an approval when the current hash is "close" (e.g. same verdict, or
stats within tolerance). This breaks signature binding: the recorded hash
would no longer identify the bytes the signer saw, so an auditor could not
distinguish "approved with one extra in-bounds reading" from "evidence
tampered with after sign-off." The exact-match rule converts every content
change into an explicit, auditable event (stale `409` → re-fetch →
countersign). Strictness here is the feature.
