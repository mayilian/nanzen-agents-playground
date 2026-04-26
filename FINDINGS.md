# Findings & Improvements Log

A running record of what was wrong with the system as delivered, what was
changed in response, and the measurable impact. Written for an audience that
includes both engineers and non-engineers — the *what* and *why* should be
legible without reading the code; the *how* is verifiable by file:line refs.

Entries are dated and appended chronologically. The dollar signs are EUR
because the customer (Meridian Health, `MERID-001`) is invoiced in EUR.

---

## 0. System as delivered

The repository ships a multi-agent system whose stated job is to produce a
renewal-risk report for the customer Meridian Health. The pieces:

| File | Role |
|---|---|
| `src/challenge/agent.py` | `ActorAgent` — wraps a smolagents `CodeAgent` that writes Python at runtime to use tools. |
| `src/challenge/tools/csv_reader.py` | `read_context(source, account_id?, limit=50)` — reads a CSV, optionally filtered. |
| `src/challenge/tools/pdf_report.py` | `create_report(title, filename, content_sections_json)` — renders structured JSON to PDF. |
| `src/challenge/tasks.py` | Three hard-coded tasks: `billing_summary`, `usage_trends`, `support_health`. |
| `src/challenge/runner.py` | Spawns one agent per task, optional `--parallel`. |
| `data/*.csv` | 8 CSV files (accounts, billing, contracts, product_usage, support_tickets, crm_interactions, emails, purchase_orders). |

There is **no orchestrator and no synthesis step**. Each agent produces its
own PDF. The "renewal-risk report" — which is the actual decision artefact —
is not produced by any task: it would have to be assembled by a human reading
three siloed PDFs side by side.

---

## Phase 1 — Discovery (before running anything)

### F1. The CSV reader's `account_id` filter is a silent no-op on the two largest data sources

**What.** `csv_reader.py:48-51` filters rows by `account_id` only when the
column exists in the CSV. If the column is absent, the filter is silently
skipped and *all* rows are returned. Two of the most relevant sources for
this customer have no such column:

| Source | Rows | Has `account_id`? | Effect of filter |
|---|---|---|---|
| `billing.csv` | 153 | No | Returns billing for **all 17 accounts** |
| `support_tickets.csv` | 327 | No | Returns tickets for **all accounts** |
| `crm_interactions.csv` | 84 | No | Returns interactions for **all accounts** |
| `emails.csv` | 754 | No | Returns emails for **all accounts** |
| `purchase_orders.csv` | 5 | No | Returns POs for **all accounts** |

The agent's task prompt (`tasks.py:18`) tells it to "analyze billing for
account MERID-001". The agent calls `read_context(source="billing",
account_id="MERID-001")`, sees a tidy 50-row response, and computes totals
*as if those rows were MERID-only*. They aren't.

**Why it matters.** Every numeric answer the agent produces for billing,
support, CRM, emails, and POs is computed across the whole customer base.
This is a structural source of wrong numbers — independent of the LLM. No
amount of better prompting fixes a broken filter.

**Correct join path.** Billing must be joined to `contracts.csv` via
`contract_reference` to recover `account_id`. MERID-001 has 2 contracts:
`CTR-2023-MH-001` (13 events) and `CTR-2024-MH-002` (42 events) — 55
relevant rows out of 153. Support tickets, CRM, emails, and POs need a
similar join (likely via `customer_department`, `email_thread_id`, or
`po_number` — to be confirmed).

**Status.** Identified, not fixed. The fix lives in the deterministic
summary layer (Phase 3+).

---

### F2. The reader silently truncates to 50 rows by default

**What.** `csv_reader.py:34` defaults `limit=50` and stops reading once that
many rows match. The function returns nothing to indicate truncation.

**Why it matters.** `support_tickets.csv` has 327 rows — almost 7× the
default. An agent that doesn't override the limit (the prompt doesn't tell
it to) computes ticket statistics on a 15% sample and reports them as totals.

**Status.** Identified, not fixed. The fix is to either page or — better —
have the tool return aggregates plus relevant samples, not raw rows.

---

### F3. `support_tickets.csv` has a malformed row that breaks pandas

**What.** Line 153 has an unterminated quoted field. Reading with the C
parser raises `Expected 21 fields in line 153, saw 22`. Reading with
`engine="python", on_bad_lines="skip"` recovers 156 rows out of 327 — the
parser drops not just one row but the entire ticket whose `content` field
spans the broken quote, plus subsequent rows until it can resync.

**Why it matters.** Roughly half the support data is silently dropped
even when you parse defensively. The agent — operating in a sandbox without
pandas — would experience the same or worse.

**Status.** Identified, not fixed. The right fix is to repair the source CSV;
the secondary fix is to use a tolerant parser and report the row-loss count.

---

### F4. The agent's sandbox forbids pandas and numpy despite both being project deps

**What.** `agent.py:74-83` whitelists only `json, csv, datetime, collections,
statistics, math, re` for the agent's Python sandbox. `pandas` and `numpy`
are listed as core dependencies in `pyproject.toml` but are not authorized
for the sandbox.

**Why it matters.** The agent has to reinvent groupby, mean, regression,
date math, and CSV parsing in the standard library. This is the most
plausible cause of the README's "the agent wastes steps" symptom — every
nontrivial aggregation is many lines of Python the model has to write
correctly under a step budget of 15.

**Status.** Identified, not fixed. The deterministic summary layer (Phase 3+)
removes the agent from the calculation path entirely, which is a stronger
fix than authorizing pandas. We may still authorize them as a stopgap.

---

### F6. Dirty data: department name has a typo in the source CSV

**What.** `product_usage.csv` contains both `Engineering` (correct) and
`Enginering` (typo, single `e`) as department values. They refer to the
same department.

**Why it matters.** Any agent or aggregator that does
`groupby("department")` will report two separate departments with split
totals. The "top department by usage" answer will be wrong, and a
human-readable report will list a non-existent department called
`Enginering`. This is a representative example of what the agent cannot
catch on its own — the fix has to live in code (canonicalize names) or in
data (repair the source).

**Status.** Identified, not fixed. Oracle normalizes the spelling so the
ground truth is correct; agent baseline will likely fail on this.

---

### F7. ~~The billing extract has a temporal gap~~ — RETRACTED

**Original claim (wrong):** that `billing.csv` only contained Meridian
payments from Nov 2025 onwards, leaving €440k phantom-outstanding.

**What actually happened:** my first oracle filtered billing rows by
`contract_reference ∈ {CTR-2023-MH-001, CTR-2024-MH-002}`, which catches
only 55 of the 153 Meridian-related rows. The other 98 are operational
events (payments, reminders, disputes, refunds, manual notes) where the
`contract_reference` field was left blank by upstream systems. With a
correct join (see F9), payments span the full Nov 2023 → Feb 2026 window
and total €491,682 — not €73k. Outstanding is **~€3,700**, essentially
nil. Meridian is a healthy paying customer.

**Lesson worth keeping.** This iteration *only worked* because we had a
deterministic oracle to check against the agent's output. The agent caught
several real events (dispute `DISP-2024-MH-001`, the SLA credit, the
duplicate-payment refund) that my oracle missed. Without something
checkable on each side, neither party would have caught the other's bug.

---

### F9. `contract_reference` is sparsely populated — joining on it alone misses ~64% of relevant rows

**What.** Operational billing events frequently leave the
`contract_reference` field blank, even when the row is unambiguously about
a known invoice. Examples for Meridian:

- `dispute_opened` (BIL-5057) and `dispute_resolved` (BIL-5059) — blank
  `contract_reference`, but reference `INV-2024-MH-013` directly.
- `refund_completed` (BIL-5146, BIL-5158) — blank.
- All `payment_received` events from Nov 2023 → Oct 2025 — blank.
- All `email_sent` and `payment_reminder_sent` events — blank.

For Meridian: 55 rows match `contract_reference`, but 153 rows are actually
about Meridian.

**Why it matters.** Any system that joins billing → account purely on
`contract_reference` silently drops most of the truth. This was the bug in
my first oracle. It's also the bug that any deterministic summary layer
would inherit unless it joins on multiple keys (contract_reference,
invoice_id pattern, credit_note_ref).

**Correct join logic (in `scratch/oracle.py`):** match a row if (a) its
`contract_reference` matches a known contract, OR (b) its `invoice_id`
matches the customer's invoice prefix (e.g. `INV-*-MH-*` for Meridian),
OR (c) its `credit_note_ref` matches a known credit note for the customer.

**Status.** Fixed in the oracle. The agent — by reading raw rows without
filtering — naturally avoided this trap, which is why its narrative
catches some real events the strict-join oracle misses. The proper fix in
the production architecture is an explicit relational join with multiple
keys, not "let the LLM read everything."

---

### F8. Renewal-critical signals live in free-text fields, not structured columns

**What.** Several signals that would change a renewal verdict are present
only as English prose inside `notes` and `content` fields:

- `INV-2024-MH-013.notes`: *"BILLING ERROR: Line items pulled from old
  contract."* — a buried billing dispute with no `dispute_id`.
- `INV-2025-MH-015.notes`: *"SLA credit from Jan 14 outage applied. Net
  amount reduced."* — credit triggered by `TKT-4893` (3-hour outage).
- `TKT-4887.notes`: *"customer flagged that this is part of the CFO's
  vendor review"* — a buying-stakeholder risk signal.
- `TKT-4909` and `TKT-4920.content`: references to AM Elena handling the
  renewal directly — relationship signals.

**Why it matters.** A pure rules engine will miss all of these. They are
exactly the kind of input where an LLM earns its keep — classifying or
surfacing relevant text that doesn't fit a fixed schema. They are also
exactly the kind of input the *current* agent architecture (which is
focused on calculating numbers) has no instruction to look for.

**Status.** Identified. Will be addressed in the synthesis layer (Phase 6) —
the LLM's job becomes reading notes for risk signals, not adding integers.

---

### F5. The actual deliverable has no task

**What.** The README states the goal is a renewal-risk report. The three
existing tasks each produce a single-domain PDF (billing OR usage OR
support). No task ties them together. The renewal-risk report — the thing
the account manager actually walks into a renegotiation with — does not
exist as code.

**Why it matters.** Even if all three agents succeeded perfectly, the human
output still wouldn't be a renewal-risk report. The system is solving a
different problem than the one it claims to.

**Status.** Identified, not fixed. The new architecture's terminal step is a
single synthesis call producing the renewal report from the deterministic
`AccountSummary`.

---

## Ground truth (oracle)

Before running the agent, we compute the canonical answers deterministically
in `scratch/oracle.py` so we have something to grade the agent's output
against. The oracle does *no* LLM calls — it is pure pandas.

### Oracle output for MERID-001 (corrected after F7 retraction + F9 fix)

| Metric | Ground truth |
|---|---:|
| Contracts | `CTR-2023-MH-001`, `CTR-2024-MH-002` |
| Billing rows for account | 153 |
| Invoices issued | 28 |
| Payments received | 31 (3 partial) |
| Credit notes issued | 2 (net €-4,529.18) |
| Refunds completed | 2 (€-18,997.50: dup payment €18,150 + misdirected €847.50) |
| Disputes opened / resolved | 1 / 1 (`DISP-2024-MH-001`, line-item display issue, resolved same day) |
| Reminders sent | 16 (all from Oct 2024 onwards) |
| Overdue-status events | 17 |
| Total invoiced | €509,852.11 |
| Total paid | €491,682.45 |
| Outstanding | **€3,701.34** (effectively settled) |
| Max days past due | 17 |
| Weeks of usage data | 123 (Nov 2023 → Mar 2026) |
| Trend slope (sessions/wk) | +0.41 |
| Q1 → Q4 sessions | 213.3 → 246.8 (**+15.7%**, growing) |
| Top dept by sessions | Engineering |
| Departments (raw vs clean) | 3 raw (`Engineering`, `Enginering`, `Marketing`) → 2 clean |
| Support: tickets total | 22 |
| Support: resolved / open | 12 / 10 |
| Support: SLA breaches | 8 (36% of tickets) |
| Support: mean CSAT | 4.5 / 5 |
| Support: mean resolution time | 5.49 days |
| Support: parser-dropped rows | 170 of 327 raw lines |

**Renewal verdict from the oracle alone (rules):**
- 16 payment reminders + 17 overdue events, max 17 days late — **operational late-payment pattern from Oct 2024 onwards** (correlates with new CFO + AP workflow per CRM/email notes).
- 8 SLA breaches across 22 tickets — meaningful at this volume.
- Otherwise healthy: usage growing, CSAT high, payments clearing, dispute resolved, no open issues of consequence.

**Signals rules will MISS** (require reading text — F8):
- Jan 14 2025 outage with 3-hour downtime → SLA credit on Feb invoice (`TKT-4893` ↔ `INV-2025-MH-015`).
- AM Elena handling renewal directly (`TKT-4909`, `TKT-4920`).
- CFO vendor-review reference in `TKT-4887`.
- Open Kubernetes 1.29 compatibility tickets (`TKT-4891` etc.) — recurring pattern visible only in ticket *content*.

The full oracle output is in `scratch/oracle_output.txt`.

---

## Phase 2 — Baseline runs (agent as delivered)

Three tasks run against the unmodified codebase using Anthropic Haiku 4.5
(`claude-haiku-4-5-20251001`) via the OpenAI-compat endpoint. Logs in
`scratch/run-baseline-{1,2,3}-*.log`; PDFs in `output/`.

### Aggregate run stats

| Task | Steps | Wall time | Input tokens | Output tokens |
|---|---:|---:|---:|---:|
| `billing_summary` | 6 | 28 s | 131,826 | 7,981 |
| `usage_trends` | 8 | 58 s | 284,456 | 17,100 |
| `support_health` | 10 | 54 s | 226,521 | 8,883 |
| **Total** | **24** | **140 s** | **642,803** | **33,964** |

**Cost ≈ $0.81 per customer** for three siloed reports (Haiku 4.5
$1/M in, $5/M out — rough). The deterministic oracle that produced the
ground truth ran in **<100ms for €0**.

All three tasks report `status: success`. There is **no automated check
that the numbers are correct.**

### Per-task gap analysis

#### `billing_summary` — narrative correct, numerics ~33% off

| Metric | Oracle | Agent PDF | Verdict |
|---|---:|---:|---|
| Invoices issued | 28 | 19 | **−9 missing** (likely `limit=50` truncation) |
| Total invoiced | €509,852 | €343,529 | **−33%** |
| Total paid | €491,682 | €324,945 | **−34%** |
| Outstanding | €3,701 | €18,584 | **5× too high** |
| Date range | Nov 2023 → Mar 2026 | Nov 2023 → Jun 2025 | **−9 months** |
| Disputes resolved | 1 | 1, with full narrative | ✓ |
| Refunds (count, amounts) | 2 (€18,998 total) | 2, with full narrative | ✓ |
| SLA credit | −€4,529 (two notes) | −€3,600 (one note) | partial |
| "Payment Success Rate 94.7%" | n/a | fabricated metric | ✗ |
| Named entities (CFO, AP contact) | not in oracle | all real, drawn from notes | ✓ |

**Pattern.** Specific events (dispute IDs, refund amounts, SEPA references)
are correct because they're literally quoted from the `notes` column the
agent read. Aggregate sums are wrong because the agent only saw a window
of the data and recomputed totals on that window.

#### `usage_trends` — direction correct, magnitudes inflated

| Metric | Oracle | Agent PDF | Verdict |
|---|---:|---:|---|
| Weeks shown in detail | 123 | 25 (Engineering only) | **−80% of data** |
| Date range | Nov 2023 → Mar 2026 | Nov 2023 → Oct 2024 | **−17 months** |
| Marketing dept rows | 41 weeks of data | not in detail table | missed in body |
| Trend direction | Growing | Growing | ✓ |
| Q1→Q4 growth | +15.7% | "+180% active users, +1,023% Marketing API calls" | **inflated** |
| `Engineering` vs `Enginering` typo | flagged, normalised | not flagged | **missed** |

The agent picked the first vs last visible data point as "growth" rather
than fitting a trend, so the percentages are dramatically inflated. The
typo would have made any per-department total wrong if it had read all
weeks.

#### `support_health` — best of the three; valuable qualitative findings

| Metric | Oracle | Agent PDF | Verdict |
|---|---:|---:|---|
| Total tickets | 22 | 14 | **−8 missing** (parser drops + truncation) |
| Resolved | 12 (55%) | 8 (57%) | proportional, magnitude off |
| Avg resolution time | 5.49 days | 22.1 hours (0.92 days) | **6× too low** |
| SLA breaches | 8 | not reported | **missed** |
| Mean CSAT | 4.5 / 5 | "no surveys completed yet" | **wrong** — 14 surveys exist |
| Date range | Nov 2023 → Feb 2026 | Nov 2023 → Feb 2025 | **−1 year** |
| K8s 1.29 recurring pattern | not in oracle | identified | ✓ value-add |
| `TKT-4891` still open / escalation | not in oracle | flagged | ✓ |
| AM Elena handling renewal | not in oracle | flagged | ✓ |
| CFO Margaret Walsh as buying stakeholder | not in oracle | flagged | ✓ |

The support task is where the agent earns its keep. It surfaces four
qualitative findings (`TKT-4891` blocking, K8s 1.29 pattern, CFO scrutiny,
renewal touchpoint) that pure rules would have missed. These are the
exact F8 signals worth keeping.

### Cross-cutting observations

1. **Zero verification.** The runner emits `[OK]` for all three tasks
   despite ~30% numerical error. There is no oracle, no test, no
   plausibility check. This is the single biggest architectural gap.
   Confirms the README's hint: *"How would you know if the agent's output
   is any good without reading it?"*

2. **Truncation everywhere.** All three tasks under-report the date range
   by ≥9 months. Consistent with `limit=50` per call and the agent
   stopping after one or two reads.

3. **Cross-source bleed already happens.** The billing PDF mentions
   Kubernetes issues from support; the support PDF mentions billing
   accuracy from finance. The "siloed agents" framing is incorrect — the
   agents *do* read multiple sources, they just don't synthesise into a
   single renewal artefact.

4. **No renewal report exists.** None of the three PDFs is the actual
   deliverable. The customer-facing artefact (a renewal-risk verdict)
   would still need to be assembled by a human reading three PDFs.

5. **The agent's strength is text, its weakness is arithmetic.** Names,
   dates, dispute IDs, ticket numbers, qualitative themes — all accurate.
   Sums, counts, averages, percentages — wrong by 30–80%. This vindicates
   the architectural direction: **deterministic code computes the
   numbers; the LLM only reads text and synthesises.**

### Where this leaves the renewal verdict

The agent's billing PDF paints Meridian as nearly-perfect ("STRONG
RELATIONSHIP — 18 of 19 invoices paid — 94.7% Payment Success Rate"). The
truth is more textured: a healthy customer with a real **late-payment
pattern from Oct 2024 onwards** (16 reminders, 17 overdue events) tied to
a CFO/AP-workflow change, alongside genuine product growth and
high CSAT. That nuance is exactly what the account manager needs walking
into the renewal — and the system loses it.

---

## Phase 3 — First fix (proposed)

*(next: deterministic summary layer + verification harness; agent retained
only for the qualitative synthesis the baseline shows it's good at)*


