"""CLI entry point: `python -m challenge MERID-001`."""

from __future__ import annotations

import argparse
import logging
import sys

from challenge.pipeline import run

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="challenge",
        description="Generate a renewal-risk PDF for an account",
    )
    parser.add_argument("account_id", help="e.g. MERID-001")
    parser.add_argument(
        "--skip-llm",
        action="store_true",
        help="Skip the LLM synthesis call; produce a deterministic-only PDF.",
    )
    args = parser.parse_args()

    result = run(args.account_id, skip_llm=args.skip_llm)
    meta = result["metadata"]
    verification = result["verification"]
    summary = result["summary"]

    print()
    print("=" * 70)
    print(f"Renewal-risk pipeline — {summary.account_id} ({summary.customer_name})")
    print("=" * 70)
    print(f"  PDF:           {result['pdf_path']}")
    print(f"  Wall time:     {meta.wall_time_s:.2f}s")
    print(f"  LLM calls:     {meta.llm_calls}  ({meta.model_id})")
    print(f"  Tokens:        {meta.llm_input_tokens:,} in / {meta.llm_output_tokens:,} out")
    print(f"  Cost (USD):    ${meta.cost_usd_estimate:.4f}")
    print(f"  Verification:  {verification.summary if verification else 'n/a (deterministic-only)'}")
    print(f"  Fallback used: {'YES' if meta.fallback_used else 'no'}")
    if meta.notes:
        print("  Notes:")
        for n in meta.notes:
            print(f"    - {n}")
    if verification and not verification.passed:
        print("  Verification diagnostics (first 5):")
        for d in verification.diagnostics[:5]:
            print(f"    - {d}")
    return 0 if not meta.fallback_used else 2  # 2 = soft-fail (PDF still produced)


if __name__ == "__main__":
    sys.exit(main())
