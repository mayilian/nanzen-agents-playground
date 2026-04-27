# Benchmark snapshot — v0.3.0 (post staff-engineer review)

Frozen artifacts after the second pass of refactoring: configuration
extracted from code, customer dictionary as first-class data,
multi-key resolver registry, tiered canonicalisation with fuzzy match,
LLM-call hardening (timeout + bounded retry), real code version, and a
synthetic multi-customer regression suite.

## Provenance

| | |
|---|---|
| Date captured | 2026-04-27 |
| Code version | git short SHA at run time (visible in PDF appendix) |
| Architecture | data_io → joins (multi-key, customer-dict-driven) → summary → signals → synthesis → verify → render |
| Customer | MERID-001 (Meridian Health) |

## Files

| File | Model | Wall | Tokens (in/out) | Cost | Verification |
|---|---|---:|---:|---:|---|
| `MERID-001-renewal-haiku.pdf` | claude-haiku-4-5-20251001 | 15.8 s | 5,776 / 1,317 | $0.0124 | passed |

## What changed since post-rearchitecture

- All hardcoded data tables (NORMALISATIONS, KEYWORDS, pricing, prompt)
  moved to `config/`.
- Per-customer YAMLs (`config/customers/<id>.yaml`) drive resolver
  behaviour — onboarding a new customer is a YAML, not a code diff.
- The "treat all rows as customer X" fallback is gone; replaced with a
  per-source resolver registry that uses email domains, departments,
  invoice patterns, and aliases. Soft-failure is now a per-customer
  policy choice (skip / hard_fail / include_with_warning).
- Tiered canonicalisation (alias → fuzzy-match → surface-as-unknown)
  via `rapidfuzz`. Logged on `LoadReport`.
- LLM call: bounded retries with exponential backoff via `tenacity`,
  explicit 60s timeout per request.
- Code version derived from `git rev-parse --short HEAD`.
- 15 tests passing (was 9), including 6 new multi-customer regression
  tests that would have caught the original "treat all as MERID" bug.
