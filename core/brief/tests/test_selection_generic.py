"""Selection on 22 Oct 2026: generic tags are not searched or ranked as topics, and a fold keeps the pattern-first
row of a round."""
from types import SimpleNamespace

from core.brief import job
from core.collect import job as collect_job
from core.understand import cluster


def _cand(item_id, worth, post_ids, pack_ids, rank):
    ev = [{"id": p, "handle": f"h{p}"} for p in pack_ids]
    return {"row": {"item_id": item_id, "title": item_id, "worth_raw": worth, "market_scope": "market"},
            "decision": SimpleNamespace(publish=True, where="today"), "posts": set(post_ids), "_round": 1,
            "_rank": rank, "pack": {"evidence": ev, "numbers": [], "facts": []}}


def test_a_fold_keeps_the_pattern_first_row_of_a_round():
    challenge = _cand("#amapianochallenge", .40, [f"c{i}" for i in range(10)], [f"c{i}" for i in range(10)], 0)
    umbrella = _cand("#amapiano", .80, [f"c{i}" for i in range(5)] + [f"u{i}" for i in range(40)],
                     [f"u{i}" for i in range(12)], 1)
    kept, folds = job._fold_plan([challenge, umbrella])
    assert [c["row"]["item_id"] for c in kept] == ["#amapianochallenge", "#amapiano"] and folds == []


def test_collect_does_not_search_tags_the_stoplist_calls_generic():
    assert all(collect_job._tag(t) is None for t in ("tiktok", "viralvideo", "kenyantiktok", "mombasatiktokers"))
    assert collect_job._tag("amapianochallenge") == "amapianochallenge" and collect_job._tag("nairobi") == "nairobi"


def test_a_topic_named_only_by_generic_tag_words_ranks_after_named_rows():
    rows = [{"item_id": "a", "kind": "topic", "label": "tiktok, viralvideo, viral"},
            {"item_id": "b", "kind": "topic", "label": "mombasatiktokers, kenyantiktok mombasatiktokers"},
            {"item_id": "c", "kind": "topic", "label": "funny, joke, funny how"},
            {"item_id": "d", "kind": "hashtag", "label": "#amapianochallenge"}]
    assert [r["item_id"] for r in job._ranked(rows)] == ["c", "d", "a", "b"]


def test_a_new_cluster_label_skips_keywords_made_only_of_generic_tag_words():
    assert cluster._label_keywords(["tiktok", "viralvideo", "load shedding", "eskom", "stage"]) == [
        "load shedding", "eskom", "stage"]
