# Benchmark — baseline vs target vs achieved

Comparable metrics for the system before and after the re-architecture
in `PLAN.md`. The "Baseline" column is measured from frozen artefacts
in `benchmarks/baseline/`. The "Target" column is what `PLAN.md` Phase
G is designed to deliver. The "Achieved" column is filled in after
Phase G lands and `make benchmark` runs end-to-end.

This document is the contract between *what we said we'd improve* and
*what we actually did*. If the achieved column doesn't match the target
column, we either revise the target or admit the gap.

---

## How the baseline was measured

| | |
|---|---|
| Date | 2026-04-26 |
| Upstream commit | `64ef5d9` (`Update README.md`) |
| Model | `claude-haiku-4-5-20251001` (Anthropic) |
| Endpoint | `https://api.anthropic.com/v1/` (OpenAI-compat shim) |
| Method | `make run ARGS="--task <name>"` for each of the 3 tasks, sequentially |
| Customer | `MERID-001` (Meridian Health) |
| Hardware | Local Mac (Apple Silicon) |
| Tokens / steps / time | Read directly from smolagents step traces in `benchmarks/baseline/run-*.log` |
| Numeric accuracy | Computed by comparing PDF-extracted numbers to `scratch/oracle.py` ground truth |

Cost figures use Anthropic Haiku 4.5 list pricing as a rough estimate
(~$1/M input, ~$5/M output). Actual cost may vary.

---

## Headline metrics

| Metric | Baseline | Target (Phase G) | Achieved |
|---|---:|---:|---:|
| Wall time per renewal report | **140 s** | < 15 s | _tbd_ |
| LLM calls per renewal | **~24** (3 agents × 6–10 steps) | **1** | _tbd_ |
| Input tokens (sum across tasks) | **642,803** | < 5,000 | _tbd_ |
| Output tokens (sum across tasks) | **33,964** | < 1,000 | _tbd_ |
| Estimated cost per renewal (Haiku 4.5) | **~$0.81** | < $0.02 | _tbd_ |
| Estimated cost per renewal (Sonnet 4.6) | not tested | < $0.20 | _tbd_ |
| Reports produced | 3 siloed PDFs | 1 renewal PDF | _tbd_ |
| Verification on output | **none** (`[OK]` regardless) | numeric + citation gate | _tbd_ |
| Numeric tokens traceable to source | 0% | 100% | _tbd_ |

---

## Per-task breakdown (baseline)

| Task | Steps | Wall time | Input tokens | Output tokens |
|---|---:|---:|---:|---:|
| `billing_summary` | 6 | 28 s | 131,826 | 7,981 |
| `usage_trends` | 8 | 58 s | 284,456 | 17,100 |
| `support_health` | 10 | 54 s | 226,521 | 8,883 |
| **Total** | **24** | **140 s** | **642,803** | **33,964** |

---

## Numeric-accuracy gap (baseline)

Comparison of the agent's PDF output to `scratch/oracle.py` ground
truth. See `FINDINGS.md` Phase 2 for the full per-field gap analysis.

### Billing
| Field | Oracle | Agent | Gap |
|---|---:|---:|---:|
| Invoices issued | 28 | 19 | **−32%** |
| Total invoiced (EUR) | 509,852 | 343,529 | **−33%** |
| Total paid (EUR) | 491,682 | 324,945 | **−34%** |
| Outstanding (EUR) | 3,701 | 18,584 | **5× too high** |
| Date range covered | Nov 2023 → Mar 2026 | Nov 2023 → Jun 2025 | **−9 months** |
| Disputes resolved | 1 | 1 (with full narrative) | ✓ |
| Refunds (count, amounts) | 2 (€18,998) | 2 (with narrative) | ✓ |
| "Payment Success Rate" | n/a | "94.7%" | **fabricated metric** |

### Usage
| Field | Oracle | Agent | Gap |
|---|---:|---:|---:|
| Weeks shown in detail | 123 | 25 | **−80%** |
| Date range covered | Nov 2023 → Mar 2026 | Nov 2023 → Oct 2024 | **−17 months** |
| Trend direction | Growing | Growing | ✓ |
| Q1→Q4 growth | +15.7% | "+180% active users / +1,023% Mkt API" | **inflated** |
| `Engineering` vs `Enginering` typo | flagged | not flagged | **missed** |

### Support
| Field | Oracle | Agent | Gap |
|---|---:|---:|---:|
| Total tickets | 22 | 14 | **−36%** |
| Resolved | 12 (55%) | 8 (57%) | proportional ✓ |
| Mean resolution time | 5.49 days | 22.1 hours (0.92 days) | **6× too low** |
| SLA breaches | 8 | not reported | **missed** |
| Mean CSAT | 4.5 | "no surveys completed yet" | **wrong** (14 surveys exist) |
| Date range covered | Nov 2023 → Feb 2026 | Nov 2023 → Feb 2025 | **−1 year** |
| K8s 1.29 recurring pattern | not in oracle | identified | ✓ value-add |
| `TKT-4891` still open / escalation | not in oracle | flagged | ✓ |
| AM Elena handling renewal | not in oracle | flagged | ✓ |

**Pattern.** The agent's qualitative observations (named entities,
themes, specific events drawn from `notes`) are accurate. Aggregate
*numbers* are wrong by 30–80%. This is the architectural inversion
`PLAN.md` addresses.

---

## Failure-mode coverage (baseline → target)

| Finding | Baseline behaviour | Target after Phase G |
|---|---|---|
| F1 silent no-op `account_id` filter | active: returns all-account data | removed; multi-key joins with audit trail |
| F2 silent `limit=50` truncation | active: silently caps reads | removed; pagination or aggregate-first |
| F3 malformed CSV row | drops 170 of 327 lines silently | counted, logged in `LoadReport`, footnoted in PDF |
| F4 sandbox blocks pandas/numpy | active: agent reinvents groupby | irrelevant — agent removed |
| F5 no synthesis task / renewal report does not exist | active | one combined renewal PDF is the output |
| F6 `Enginering` typo silently splits aggregates | active | canonicalised in `canonical.py`, normalisations logged |
| F7 retracted (oracle bug, not data bug) | n/a | n/a |
| F8 renewal signals only in free text | uncaught (or accidental) | retrieved deterministically by `signals.py`, fed to LLM |
| F9 sparse `contract_reference` | drops 64% of relevant rows | OR-joined across keys in `joins.py` |

---

## Per-stage micro-benchmarks (target)

Baseline is "no equivalent" for these — the current architecture
doesn't have stages, just an agent loop. Targets are budget allowances
for Phase G.

| Stage | Target |
|---|---:|
| Stage 1 — Ingestion + validate | < 200 ms |
| Stage 2 — Multi-key account assembly | < 100 ms |
| Stage 3 — Deterministic summary | < 100 ms |
| Stage 4 — Signal retriever | < 100 ms |
| Stage 5 — LLM synthesis (Sonnet 4.6) | 5–10 s |
| Stage 6 — Verification gate | < 50 ms |
| Stage 7 — PDF render | < 1 s |
| **Total** | **< 15 s** |

---

## Reproducing this

### Baseline (frozen)

The baseline artefacts in `benchmarks/baseline/` are immutable. To
reproduce them from scratch:

```bash
git checkout 64ef5d9                           # upstream baseline commit
cp .env.example .env                           # set MODEL_ID=claude-haiku-4-5-20251001
set -a && source .env && set +a
make install
make run ARGS="--task billing_summary"
make run ARGS="--task usage_trends"
make run ARGS="--task support_health"
uv run python scratch/oracle.py
```

### Achieved column (after Phase G)

`make benchmark` will run the full new pipeline on Meridian, capture
wall time / tokens / cost / numeric accuracy, and append a row to a
"history" section below.

---

## History

_(populated by `make benchmark` after each milestone)_

| Date | Commit | Wall time | LLM calls | Tokens (in/out) | Cost (Haiku) | Numeric accuracy | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| 2026-04-26 | `64ef5d9` | 140 s | 24 | 642,803 / 33,964 | ~$0.81 | 30–80% wrong | Baseline (smolagents `CodeAgent`, 3 siloed agents, no verification) |
