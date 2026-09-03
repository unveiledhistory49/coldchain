# Security policy

## Supported versions

| Version | Supported |
| ------- | --------- |
| 1.0.x (`coldchain`, `pyproject.toml`) | Yes — current |

This is a 1.0 codebase with no prior release branches; only the latest
`main` plus `1.0.x` patch releases receive fixes.

## Reporting a vulnerability

Email the maintainers privately (do **not** open a public issue for
suspected vulnerabilities). Include: affected version/commit, steps to
reproduce, and impact assessment. Expect an acknowledgement within 3
business days; we will coordinate a fix and disclosure timeline with you
and credit reporters on request.

## What we protect (threat model)

See `docs/THREAT_MODEL.md` for assets (sensor evidence, API keys,
approvals), in-scope threats (spoofed readings, key leakage, approval
forgery, audit tampering, webhook SSRF, backdated ingestion), verified
mitigations, and honest residual risks.

Key operator duties: set `COLDCHAIN_API_KEY_PEPPER` to a unique secret
(default is `dev-pepper-change-me`), guard the SQLite file, and serve
behind TLS.

## Dependency and CI hygiene

- **pip-audit in CI:** planned — not yet configured (no `.github/`
  workflows exist in this repo). Until then, run `pip-audit` locally
  before releases.
- **Dependabot:** planned — no `.github/dependabot.yml` exists yet. Until
  then, review `fastapi`, `uvicorn`, `sqlalchemy`, `pydantic`, and `httpx`
  advisories manually.
- Microsurface: stdlib `hashlib`/`hmac`, no custom crypto; webhook HTTP via
  `httpx` with a 5 s default timeout.
