"""B2 grounding verifier shadow stub."""

from src.analysis.grounding_verifier import run_grounding_verifier_shadow


def test_shadow_stub_returns_empty():
    out = run_grounding_verifier_shadow("2026-07-01", ["claim one"], market="za")
    assert out == []
