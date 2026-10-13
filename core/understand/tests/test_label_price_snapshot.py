import json

import pytest

from core.llm.gemini import GeminiModel
from core.llm.tests.test_llm import FakeClient, response
from core.understand import cluster
from core.understand.tests.test_video import DAY


@pytest.mark.parametrize("pin,pout,think,expected", [(100, 20, 30, 0.0002), (0, 0, 0, 0.0)])
def test_label_net_preserves_known_snapshot_cost_and_successful_label_after_price_drift(
        monkeypatch, pin, pout, think, expected):
    monkeypatch.setenv("GEMINI_PRICE_INPUT_PER_M", "1")
    monkeypatch.setenv("GEMINI_PRICE_OUTPUT_PER_M", "2")
    monkeypatch.delenv("GEMINI_PRIORITY", raising=False)
    monkeypatch.setattr(cluster, "spend_today", lambda *args: (0, 100))
    bookings = []
    monkeypatch.setattr(cluster, "book_spend", lambda *args, **kw: bookings.append(kw["usd"]) or kw["usd"])
    payload = {"clusters": [{"index": 0, "flagged_labels": [], "flagged_keywords": []}]}
    client = FakeClient(response(text=json.dumps(payload), pin=pin, pout=pout, think=think))
    generate = client.models.generate_content

    def drift(**kwargs):
        monkeypatch.setenv("GEMINI_PRICE_INPUT_PER_M", "nan")
        return generate(**kwargs)

    client.models.generate_content = drift
    current = {"cluster_id": "c", "label": "dance", "labels": ["dance"], "keywords": ["music"]}
    [out], counts = cluster.label_net(None, [current], model=GeminiModel(client=client), run_id="r", day=DAY)
    assert len(client.calls) == 1 and bookings == [pytest.approx(expected)]
    assert counts["model_usd"] == counts["booked_usd"] == pytest.approx(expected)
    assert out["label"] == "dance" and not counts["net_failed"]
    assert not counts.get("net_unknown_spend")
