# Post-rearchitecture benchmark snapshot

Frozen artefacts from the new pipeline (Phase G of `PLAN.md`) running on
the same dataset as `benchmarks/baseline/`. Each PDF was produced by a
single Anthropic call, with the verifier passing, against the same
Meridian Health (`MERID-001`) data.

## Provenance

| | |
|---|---|
| Date captured | 2026-04-27 |
| Code version | 0.2.0 (single-LLM-call pipeline) |
| Architecture | data_io → joins → summary → signals → synthesis → verify → render |
| Customer | MERID-001 (Meridian Health) |

## Files

| File | Model | Wall | Tokens (in/out) | Cost | Verification |
|---|---|---:|---:|---:|---|
| `MERID-001-renewal-haiku.pdf` | claude-haiku-4-5-20251001 | 18.02 s | 5,933 / 1,473 | $0.0133 | passed |
| `MERID-001-renewal-sonnet.pdf` | claude-sonnet-4-6 | 28.23 s | 5,934 / 1,463 | $0.0398 | passed |
| `run-newpipeline-haiku.log` | — | — | — | — | full agent trace |
| `run-newpipeline-sonnet.log` | — | — | — | — | full agent trace |

Both PDFs are the *complete* renewal-risk artefact (one combined
report, not three siloed ones), produced by a single LLM call gated
by the deterministic verifier.

## Reproduce

```bash
cd nanzen-agents-playground
make install
cp .env.example .env  # set API_KEY (Anthropic), MODEL_ID
set -a && source .env && set +a

# Haiku (fast, cheap)
MODEL_ID=claude-haiku-4-5-20251001 make pipeline ARGS="MERID-001"

# Sonnet (slower, more thoughtful)
MODEL_ID=claude-sonnet-4-6 make pipeline ARGS="MERID-001"
```
