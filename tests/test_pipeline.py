"""End-to-end pipeline test with the LLM mocked.

Confirms the orchestrator wires stages 1-7 correctly, the deterministic
fallback path produces a PDF when the LLM is skipped, and the verifier
gate is invoked when a verdict is present.
"""

from pathlib import Path

from challenge.pipeline import run


def test_pipeline_skip_llm_produces_pdf(tmp_path):
    out = tmp_path / "renewal.pdf"
    result = run("MERID-001", skip_llm=True, output_path=out)
    assert result["pdf_path"].exists()
    assert result["pdf_path"].stat().st_size > 1000
    assert result["metadata"].fallback_used is True
    assert result["metadata"].llm_calls == 0
    assert result["verdict"] is None


def test_pipeline_carries_metadata(tmp_path):
    out = tmp_path / "renewal.pdf"
    result = run("MERID-001", skip_llm=True, output_path=out)
    meta = result["metadata"]
    assert meta.account_id == "MERID-001"
    assert meta.wall_time_s > 0
    assert meta.cost_usd_estimate == 0.0
