import json

import pytest

from core.understand import cluster, enrich, video
from core.understand.tests.test_enrich import good_output
from core.understand.tests.test_video import DAY, FakeExecute, clip, good_read, run


UNKNOWN = {"input_tokens": None, "output_tokens": None, "usd": None, "usage_unknown": True}


@pytest.mark.parametrize("deadline", [None, 10.0])
def test_enrichment_retains_output_books_estimate_and_stops_new_calls(monkeypatch, deadline):
    monkeypatch.setattr(enrich, "GEMINI_SYNC_MAX_INFLIGHT", 1)

    class Model:
        def complete_json(self, **kwargs):
            return good_output(), dict(UNKNOWN)

    posts = [{"post_id": "p1", "content": "dance"}, {"post_id": "p2", "content": "dance"}]
    result = enrich.SyncRunner(Model()).run(posts, deadline=deadline, monotonic=lambda: 0.0)
    assert len(result.outputs) == 1 and result.outputs[0]["output"] == good_output()
    assert result.skipped == ["p2"] and result.unknown_spend_usd == enrich.est_usd(posts[0], batched=False) > 0
    counts = {"enriched": 0, "failed": 0, "skipped": 0}
    writes, bookings = [], []

    def execute(sql, params):
        writes.extend(json.loads(params["rows"]))
        return {"num_dml_affected_rows": 1}

    enrich._book(execute, result, posts, counts, set(), batched=False,
                 spend=lambda amount, what: bookings.append(amount))
    assert writes[0]["post_id"] == "p1" and counts["enriched"] == 1
    assert counts["unknown_spend"] and sum(bookings) == result.unknown_spend_usd


def test_label_net_retains_passed_labels_and_books_unknown_success_estimate(monkeypatch):
    monkeypatch.setattr(cluster, "spend_today", lambda *args: (0, 100))
    bookings = []
    monkeypatch.setattr(cluster, "book_spend", lambda *args, **kwargs: bookings.append(kwargs["usd"]) or kwargs["usd"])

    class Model:
        def complete_json(self, **kwargs):
            return {"clusters": [{"index": 0, "flagged_labels": [], "flagged_keywords": []}]}, dict(UNKNOWN)

    current = {"cluster_id": "c1", "label": "amapiano", "labels": ["amapiano"], "keywords": ["dance"]}
    [row], counts = cluster.label_net(None, [current], model=Model(), run_id="r", day=DAY)
    assert row["label"] == "amapiano" and not counts["net_failed"]
    assert counts["net_unknown_spend"] and counts["model_usd"] == counts["booked_usd"] == sum(bookings) > 0


@pytest.mark.parametrize("failed", [False, True])
def test_video_books_unknown_usage_estimate_and_preserves_a_successful_read(failed):
    class Model:
        def complete_json(self, **kwargs):
            if failed:
                error = ValueError("bad JSON")
                error.usage = error.usd = None
                error.reserve_model_estimate = True
                raise error
            return good_read(), dict(UNKNOWN)

    post = clip("p")
    fake = FakeExecute([post])
    counts = run(fake, model=Model(), k=1)
    assert counts["unknown_spend"] is True
    assert counts["model_usd"] == counts["booked_usd"] == video.est_usd(post) > 0
    assert sum(row["model_usd"] for row in fake.booked()) == counts["model_usd"]
    assert counts["read"] == int(not failed) and counts["failed"] == int(failed)
    assert len(fake.inserted()) == int(not failed)
