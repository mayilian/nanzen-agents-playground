# Renewal-risk reporting pipeline

A deterministic data pipeline with a single LLM synthesis call, producing a
verified renewal-risk PDF for a B2B SaaS customer from CSV inputs. Forked
from [`nanzen-ai/nanzen-agents-playground`](https://github.com/nanzen-ai/nanzen-agents-playground)
(a `smolagents`-based take-home challenge) and re-architected end-to-end.

The original challenge ran three independent code-writing agents that
produced three disconnected PDFs with numbers wrong by 30–80%, no
verification, and ~$0.81 in tokens per customer. This rewrite produces
**one** verified renewal-risk PDF in **18 seconds for $0.013** — same
data, same model family, different architecture.

| | Original (smolagents) | This rewrite |
|---|---:|---:|
| Wall time per customer | 140 s | **18 s** (Haiku) |
| LLM calls | ~24 | **1** |
| Cost per customer | ~$0.81 | **$0.013** (Haiku) / $0.040 (Sonnet) |
| Numeric accuracy | 30–80% wrong | **100% verified** |
| Reports | 3 disconnected PDFs | **1 combined renewal report** |
| Verification | none | numeric + citation gate |

See [`BENCHMARK.md`](BENCHMARK.md) for full metrics, [`FINDINGS.md`](FINDINGS.md)
for the discovery log that drove the redesign, and [`PLAN.md`](PLAN.md) for
the architecture spec.

---

## Goal

Produce a renewal-risk report for a B2B SaaS customer (e.g. Meridian
Health, `MERID-001`) that an account manager can walk into a renewal
conversation with. The report must be:

- **Numerically correct.** Every figure traceable to source data.
- **Concretely actionable.** Not "monitor the situation" — specific
  talking points referencing real tickets, invoices, and stakeholders.
- **Auditable.** Provenance, data-quality footnotes, and a reproducible
  trail from raw CSV to final number.
- **Cheap and fast.** Generated on demand, not nightly batch.

The original framing was "let an agent figure out how to write a Python
program that aggregates the data." The actual problem decomposes into
~95% deterministic computation (sums, joins, rule-based flagging) and
~5% qualitative synthesis (reading free-text signals and assembling a
narrative). The architecture below reflects that decomposition.

---

## Architecture

```
┌─────────┐   ┌────────────┐   ┌─────────────┐   ┌────────────────┐
│  CSVs   │ → │ Ingestion  │ → │   Account   │ → │ Deterministic  │
│ (data/) │   │ + validate │   │  context    │   │   summary      │
└─────────┘   └────────────┘   └─────────────┘   └────────────────┘
                                                        │
                                            ┌───────────┴───────────┐
                                            ↓                       ↓
                                  ┌──────────────────┐   ┌─────────────────┐
                                  │ Signal retriever │   │ AccountSummary  │
                                  │ (text excerpts)  │   │  (typed facts)  │
                                  └────────┬─────────┘   └────────┬────────┘
                                           │                      │
                                           └──────────┬───────────┘
                                                      ↓
                                          ┌────────────────────────┐
                                          │  LLM synthesis (1 call)│
                                          │  → RenewalVerdict      │
                                          └───────────┬────────────┘
                                                      ↓
                                          ┌────────────────────────┐
                                          │  Verification gate     │
                                          │  (numeric + citation)  │
                                          └───────────┬────────────┘
                                                      ↓
                                          ┌────────────────────────┐
                                          │  Deterministic PDF     │
                                          │  renderer              │
                                          └───────────┬────────────┘
                                                      ↓
                                                output/*.pdf
```

One direction. No loops. The LLM never sees raw CSVs — only the typed
`AccountSummary` and a curated bundle of free-text signals.

| Stage | Module | What it does | LLM? |
|---|---|---|:---:|
| 1 | `data_io.py` | Load 8 CSVs, validate schema, canonicalise typos, log dropped rows | — |
| 2 | `joins.py` | Multi-key account assembly (4 join keys; fixes the silent no-op filter and sparse FK problems) | — |
| 3 | `summary.py` | Typed `AccountSummary` (billing / usage / support facts + rule-based flags) | — |
| 4 | `signals.py` | Curate ≤24 free-text excerpts (open high-pri tickets, keyword-tagged notes, recent CRM, stakeholder emails) within token budget | — |
| 5 | `synthesis.py` | Single Anthropic call with `tool_use` enforcing a Pydantic `RenewalVerdict` schema (verdict, narrative, 3 talking points) | ✅ |
| 6 | `verify.py` | Hard gate: every numeric token must derive from `AccountSummary` or a `TextSignal`; every entity ID must exist | — |
| 7 | `render.py` | Combined renewal-risk PDF with cover, narrative, talking points, sections, and provenance appendix | — |

---

## Optimizations

The big architectural moves, in order of impact:

### 1. Move computation out of the LLM
Sums, counts, joins, slopes, threshold flags — all deterministic in
`summary.py`. The LLM is structurally prevented from doing arithmetic:
it never sees raw rows, only the pre-computed `AccountSummary`. This
single change accounts for most of the cost reduction (~60×) and the
move from 30–80% wrong numbers to 100% verified.

### 2. One LLM call instead of an agent loop
The original `CodeAgent` ran 6–10 round-trips per task × 3 tasks ≈ 24
calls per customer, because the model was iterating to *figure out
what to compute*. With the computation done up front, the model only
needs to read structured facts and produce narrative. 1 call. No
codegen, no Python sandbox, no `additional_authorized_imports`.

### 3. Multi-key joins for account assembly
The original system filtered CSVs by `account_id`, but most relevant
sources don't have that column. Billing has it on ~36% of rows; the
rest live under `contract_reference`, `invoice_id` patterns,
`credit_note_ref`, or only in free-text descriptions. `joins.py` does
an OR-join across all four keys, with a `JoinReport` showing which
key matched how many rows. For Meridian: 146 → **148 rows correctly
attributed** vs the previous architecture's silent miss.

### 4. Hard verification gate (the missing layer)
Every number in the LLM's prose is regex-extracted and checked against
`AccountSummary` (with rounding tolerance) or numbers found in cited
`TextSignal` text. Every `TKT-`/`INV-`/`CRM-`/`EM-`/etc. token must
exist in the source. If verification fails, the run does not silently
ship a wrong PDF — it falls back to a deterministic-only narrative
with a clear marker. The original system reported `[OK]` regardless
of correctness; this one cannot.

### 5. Structured I/O at every boundary
Typed `dataclass`es for everything internal (`LoadReport`,
`AccountContext`, `BillingFacts`, `UsageFacts`, `SupportFacts`,
`AccountSummary`, `TextSignal`, `RunMetadata`); `pydantic.BaseModel`
for the LLM output (`RenewalVerdict`, `TalkingPoint`); Anthropic
`tool_use` to enforce the schema at the API layer. No JSON-as-string
parameters; no free-text intermediate state.

### 6. Loud failures, surfaced provenance
- Malformed CSV rows: counted on `LoadReport`, not silently skipped
- Categorical typos (`Enginering` → `Engineering`): canonicalised, with
  every applied normalisation logged
- Schema drift: validated at ingest, fails fast
- Account not found: lists valid IDs in the error
- Every PDF has a provenance appendix: code version, model, wall time,
  tokens, cost, rows-per-source, normalisations, join-key breakdown,
  parse warnings

### 7. Token budget discipline
- Signal retriever caps each excerpt and the total bundle deterministically
- Account summary is JSON, not pipe-delimited text — no whitespace tax
- Result: 5,933 input tokens (vs 642,803 baseline)

### 8. Pluggable seams for scale
- `synthesis.py` takes a verdict schema and prompt — adding new report
  types (QBR, churn-risk, expansion) is a new schema + prompt, not a
  new pipeline
- `data_io.py` can swap pandas → DuckDB/Polars when CSVs grow past
  ~5 M rows; downstream stages don't care
- `pipeline.run(account_id)` is the natural unit of work — scaling to
  N customers is N pipeline invocations in parallel

---

## Project structure

```
nanzen-agents-playground/
├── README.md                # this file
├── PLAN.md                  # 12-section architecture spec
├── BENCHMARK.md             # before/after metrics, methodology, history
├── FINDINGS.md              # discovery log (F1–F9, dated)
├── pyproject.toml           # anthropic + pydantic; smolagents removed
├── Makefile                 # install, test, pipeline, smoke
├── .env.example             # ANTHROPIC_API_KEY, MODEL_ID
├── data/                    # 8 CSVs (read-only)
├── src/challenge/
│   ├── __main__.py          # CLI: python -m challenge MERID-001
│   ├── pipeline.py          # orchestrator
│   ├── models.py            # typed contracts at every boundary
│   ├── data_io.py           # Stage 1
│   ├── canonical.py         # categorical normalisation tables
│   ├── joins.py             # Stage 2 (multi-key)
│   ├── summary.py           # Stage 3 (deterministic AccountSummary)
│   ├── signals.py           # Stage 4 (text excerpt retriever)
│   ├── synthesis.py         # Stage 5 (single Anthropic call)
│   ├── llm.py               # thin Anthropic SDK wrapper
│   ├── verify.py            # Stage 6 (hard verification gate)
│   └── render.py            # Stage 7 (deterministic PDF)
├── tests/                   # 9 passing tests
│   ├── test_summary.py      # AccountSummary numbers match the oracle
│   ├── test_verify.py       # verifier accepts grounded, rejects fabricated
│   └── test_pipeline.py     # e2e fallback path
├── scratch/
│   └── oracle.py            # standalone deterministic ground truth
└── benchmarks/
    ├── baseline/            # frozen artefacts from the original system
    └── post-rearchitecture/ # frozen artefacts from this pipeline
```

---

## Running it

```bash
git clone https://github.com/mayilian/nanzen-agents-playground.git
cd nanzen-agents-playground
make install                                 # uv sync
cp .env.example .env                         # set ANTHROPIC_API_KEY + MODEL_ID
set -a && source .env && set +a

# Run the full pipeline
make pipeline ARGS="MERID-001"

# Or directly
uv run python -m challenge MERID-001

# Deterministic-only — no LLM call, no API key required
uv run python -m challenge MERID-001 --skip-llm

# Tests
make test
```

Output PDF lands at `output/MERID-001-renewal.pdf`.

### Switching models

`MODEL_ID` env var. Recommended:

| Model | Wall | Cost / customer | Notes |
|---|---:|---:|---|
| `claude-haiku-4-5-20251001` | ~18 s | ~$0.013 | Default, production-ready |
| `claude-sonnet-4-6` | ~28 s | ~$0.040 | Better synthesis nuance |
| `claude-opus-4-7` | longer | higher | Reserved for hardest customers |

---

## Status

- ✅ Phases A–H of `PLAN.md` complete
- ✅ 9 tests passing
- ✅ Live runs verified on Meridian Health with both Haiku 4.5 and Sonnet 4.6
- ✅ Verification gate passes; numeric accuracy 100%
- ⚠️ Wall-time target was <15 s; achieved 18 s on Haiku (LLM latency
  dominates). Closer to target would require either prompt-caching or
  a smaller model.

For the architectural rationale, the discovery findings, and the
scalability seams (multi-customer, multi-currency, additional report
types), see [`PLAN.md`](PLAN.md).

---

## Acknowledgements

Forked from [`nanzen-ai/nanzen-agents-playground`](https://github.com/nanzen-ai/nanzen-agents-playground).
The original repository deliberately ships a rough multi-agent system
as a take-home challenge; this fork is one possible answer to it.
