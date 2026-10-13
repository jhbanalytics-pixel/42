import pytest
from google import genai

from core.brief import explain, job
from core.brief.tests import test_brief_job as job_tests
from core.brief.tests.test_brief_gemini_path import (
    FakeModels, _gemini_env, _market_scope, _world, fake_sdk_client,
)


@pytest.mark.parametrize("stage", ["writer", "support", "critic", "all", "known"])
@pytest.mark.parametrize("malformed", [False, True])
def test_missing_usage_books_admitted_estimate_once_and_keeps_checked_content(monkeypatch, stage, malformed):
    _gemini_env(monkeypatch)
    monkeypatch.setattr(job, "read_market_scope", _market_scope)
    monkeypatch.setattr(job, "model_daily_usd", lambda: 1.0)
    monkeypatch.setattr(explain, "model_daily_usd", lambda: 1.0)
    admitted = []
    estimate = explain._estimate_usd

    def record_estimate(*args):
        value = estimate(*args)
        admitted.append(value)
        return value

    monkeypatch.setattr(explain, "_estimate_usd", record_estimate)

    class MissingUsageModels(FakeModels):
        def __init__(self):
            super().__init__()
            self.unknown = []

        def generate_content(self, **kwargs):
            response = super().generate_content(**kwargs)
            properties = kwargs["config"].response_json_schema["properties"]
            actual_stage = "writer" if "explanation" in properties else (
                "support" if "verdict" in properties else "critic")
            unknown = stage in (actual_stage, "all")
            self.unknown.append(unknown)
            if unknown:
                response.usage_metadata = None
                if malformed:
                    response.text = "{broken"
            return response

    sdk = MissingUsageModels()
    monkeypatch.setattr(genai, "Client", lambda **kwargs: fake_sdk_client(sdk))
    model = job.brief_model()
    try:
        result = job_tests.brief(_world(), model=model, workers=1)
    finally:
        model.close_deadline_calls()
    assert len(admitted) == len(sdk.calls)
    reserved = sum(value for value, unknown in zip(admitted, sdk.unknown) if unknown)
    known = sum(not unknown for unknown in sdk.unknown)
    price = explain.price_for(explain.default_model())
    per_known = ((120 + 5 - 20) * price["input"] + 20 * price["cached"]
                 + (30 + 40) * price["output"]) / 1_000_000
    if stage == "known":
        assert "model_reserved_usd" not in result.counts
    else:
        assert reserved > 0
        assert result.counts["model_reserved_usd"] == pytest.approx(reserved, abs=1e-6)
        assert result.chain.finished[0]["counts"]["model_reserved_usd"] == result.counts["model_reserved_usd"]
    assert result.counts["model_usd"] == pytest.approx(reserved + known * per_known, abs=1e-6)
    if stage == "all":
        assert result.counts["model_usd"] != pytest.approx(2 * reserved, abs=1e-6)
    if malformed and stage != "known":
        assert not job_tests.payload(result, "ZA")["cards"]
    else:
        [card] = job_tests.payload(result, "ZA")["cards"]
        assert card["explained"] is True
        assert len(card["claims"]) == 3
        assert card["specificity"]["status"] == "pass"


def test_unknown_success_marks_estimated_usage_and_counts_it_at_next_admission(monkeypatch):
    from core.brief.tests.test_brief_explain import FakeModel, good, run

    model = FakeModel([good()])
    model.usage = {"usd": None, "input_tokens": None, "output_tokens": None, "usage_unknown": True}
    monkeypatch.setattr(explain, "_estimate_usd", lambda *args: 0.1)
    monkeypatch.setattr(explain, "model_daily_usd", lambda: 0.15)
    result = run(model)
    assert result["reason"] == "model_cap"
    assert len(model.calls) == 1
    assert result["usage_unknown"] is True
    assert result["usage_usd"] == result["model_reserved_usd"] == 0.1
