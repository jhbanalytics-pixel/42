"""core/eval/identity_eval.py: outcome classification, separate merge and fragmentation counts, and the current
matcher's results on the synthetic cases, including the shared-publisher merge that is still open."""
from datetime import date

import pytest

from core.eval import identity_eval as ie

DAY = date(2026, 9, 28)


@pytest.mark.parametrize("kind, got, expect, want", [
    ("match", "a", "a", "correct"),
    ("recurrence", "a", "a", "correct"),
    ("match", "b", "a", "incorrect_merge"),
    ("match", "a", None, "incorrect_merge"),
    ("recurrence", "a", None, "incorrect_merge"),
    ("new", None, None, "correct"),
    ("new", None, "a", "fragmented"),
    ("variant", "a", "a", "variant_split"),
    ("variant", "b", "a", "fragmented"),
    ("variant", "a", None, "variant_link"),
])
def test_outcome(kind, got, expect, want):
    assert ie.outcome({"kind": kind, "item_id": got}, expect) == want


def test_synthetic_items_sit_at_the_written_cosine():
    case = {"id": "c", "category": "x", "expect": None, "cluster": {},
            "items": [{"item_id": "i", "cosine": 0.83, "days_since_seen": 30},
                      {"item_id": "j", "cosine": 0.5, "days_since_seen": 30}]}
    [row] = ie.score([case], DAY)
    assert row["kind"] == "new"  # cosine alone is one vote
    decision = ie.cluster.assign([ie._cluster(case)], [ie._item(s, n, DAY) for n, s in enumerate(case["items"])],
                                 DAY)[0]
    assert dict(decision["candidates"]) == pytest.approx({"i": 0.83, "j": 0.5})


def test_merges_and_fragmentation_are_counted_apart():
    rows = [{"category": "a", "expect": None, "outcome": "incorrect_merge"},
            {"category": "a", "expect": "x", "outcome": "fragmented"},
            {"category": "b", "expect": "x", "outcome": "correct"}]
    s = ie.summary(rows)
    assert (s["incorrect_merges_of_separate"], s["expect_separate"]) == (1, 1)
    assert (s["fragmented_of_same"], s["expect_same"]) == (1, 2)
    assert s["by_category"] == {"a": {"incorrect_merge": 1, "fragmented": 1}, "b": {"correct": 1}}


def test_current_matcher_on_the_synthetic_cases():
    rows = {r["id"]: r for r in ie.score(ie.load(), DAY)}
    assert {k: r["outcome"] for k, r in rows.items()} == {
        "continuity-keywords": "correct",
        "continuity-sound-paraphrase": "correct",
        "continuity-low-cosine-shared-tags": "correct",
        "recurrence-dormant-creator": "correct",
        "unrelated-semantic-recent": "correct",
        "generic-hashtag-recent": "correct",
        "unrelated-trending-sound-recent": "variant_link",
        # A fragment sharing only a hashtag below the match cosine is now a variant child, not a continuation.
        "duplicate-fragment-hashtag-only": "variant_split",
        "duplicate-fragment-hashtag-keywords": "correct",
        "competing-unrelated-nearer": "correct",
        # Open: a shared publisher plus generic keyword overlap still continues an identity across subjects.
        "shared-publisher-generic-keywords": "incorrect_merge",
    }
    assert [k for k, r in rows.items() if r["known_open"]] == ["shared-publisher-generic-keywords"]
