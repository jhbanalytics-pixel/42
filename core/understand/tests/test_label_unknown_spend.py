from datetime import date

import pytest

from core.understand import cluster


@pytest.mark.parametrize("error", [TimeoutError("timed out"), RuntimeError("lost connection")])
def test_label_net_records_unknown_spend_before_neutral_fallback(monkeypatch, error):
    monkeypatch.setattr(cluster, "spend_today", lambda *args: (0, 100))
    bookings = []
    monkeypatch.setattr(cluster, "book_spend", lambda *args, **kwargs: bookings.append(kwargs) or kwargs["usd"])

    class Model:
        def complete_json(self, **kwargs):
            raise error

    current = {"cluster_id": "c1", "label": "amapiano", "labels": ["amapiano"], "keywords": ["dance"]}
    rows, counts = cluster.label_net(None, [current], model=Model(), run_id="r1", day=date(2026, 10, 13))
    assert counts["net_failed"] and rows[0]["label"] == cluster._neutral(current)
    assert counts["net_unknown_spend"] is True
    assert counts["model_usd"] == counts["booked_usd"] > 0
    assert len(bookings) == 1 and bookings[0]["usd"] == counts["model_usd"]


@pytest.mark.parametrize("pre_dispatch", [False, True])
def test_refused_or_known_pre_dispatch_label_errors_do_not_reserve(monkeypatch, pre_dispatch):
    monkeypatch.setattr(cluster, "spend_today", lambda *args: (0, 100))
    error = RuntimeError("not generated")
    error.known_pre_dispatch = pre_dispatch
    if not pre_dispatch:
        error.code = 429

    class Model:
        def complete_json(self, **kwargs):
            raise error

    current = {"cluster_id": "c1", "label": "amapiano", "labels": ["amapiano"], "keywords": ["dance"]}
    _, counts = cluster.label_net(None, [current], model=Model(), run_id="r1", day=date(2026, 10, 13))
    assert counts["model_usd"] == 0 and not counts.get("net_unknown_spend")
