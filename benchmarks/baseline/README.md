# Baseline benchmark snapshot

Frozen artefacts of the system as delivered, run unmodified against the
provided dataset for `MERID-001` (Meridian Health). These are the
"before" half of the before/after we will compare against once the
re-architecture in `PLAN.md` lands.

## Provenance

| | |
|---|---|
| Date captured | 2026-04-26 |
| Upstream commit (`upstream/main`) | `64ef5d9` (`Update README.md`) |
| Fork | `mayilian/nanzen-agents-playground` |
| Model | `claude-haiku-4-5-20251001` (Anthropic) |
| Endpoint | `https://api.anthropic.com/v1/` (OpenAI-compat shim) |
| Code state | unchanged from upstream |
| Data state | unchanged from upstream `data/*.csv` |

## What's in here

| File | Source | Notes |
|---|---|---|
| `billing_summary_merid001.pdf` | task `billing_summary` | The agent's billing PDF — see `BENCHMARK.md` for accuracy gap |
| `usage_trends_merid001.pdf` | task `usage_trends` | The agent's usage PDF |
| `support_health_merid001.pdf` | task `support_health` | The agent's support PDF |
| `run-baseline-1-billing.log` | full agent trace for billing run | shows codegen steps, tool calls, token usage |
| `run-baseline-2-usage.log` | full agent trace for usage run | |
| `run-baseline-3-support.log` | full agent trace for support run | |
| `oracle_output.txt` | output of `scratch/oracle.py` | the deterministic ground truth used to grade the agent |

## How to reproduce

```bash
cd nanzen-agents-playground
make install
cp .env.example .env  # set MODEL_ID=claude-haiku-4-5-20251001, API_KEY, API_BASE
set -a && source .env && set +a

# baseline — produces the three PDFs in output/ and prints the agent traces
make run ARGS="--task billing_summary"
make run ARGS="--task usage_trends"
make run ARGS="--task support_health"

# ground truth (deterministic, no LLM)
uv run python scratch/oracle.py
```

## Do not modify

These files are immutable. They represent a single point in time. If
re-running the baseline becomes necessary (e.g. with a different model),
create a sibling directory under `benchmarks/` with a descriptive name —
do not overwrite this snapshot.

The active analysis is in `FINDINGS.md` and `BENCHMARK.md` at the repo
root.
