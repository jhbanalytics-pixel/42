import hashlib
import json

import pytest

from ops.evaluation.discovery_review import (
    MAX_SAMPLE_PER_MARKET,
    freeze_review_sample,
    gate_review_thresholds,
    score_review_sample,
)

WEEK_START = "2026-08-31"
CLOSED_AT = "2026-09-07T06:00:00Z"
OPEN_AT = "2026-09-06T23:59:59Z"
MARKETS = ("za", "ng", "ke")


def cluster(cluster_id, market="za", *, eligible=True, week_start=WEEK_START):
    return {
        "cluster_id": cluster_id,
        "market": market,
        "week_start": week_start,
        "eligible": eligible,
    }


def clusters(counts):
    return [
        cluster(f"{market}_{index:03d}", market)
        for market, count in counts.items()
        for index in range(count)
    ]


def review(cluster_id, market, *, coherent=True, duplicate=False, foreign=False):
    return {
        "cluster_id": cluster_id,
        "market": market,
        "reviewer": "reviewer_a",
        "cultural_move": f"move for {cluster_id}",
        "coherent": coherent,
        "duplicate": duplicate,
        "foreign_market": foreign,
    }


def test_open_week_is_refused():
    with pytest.raises(ValueError, match="week_open"):
        freeze_review_sample(
            clusters({"za": 3}),
            week_start=WEEK_START,
            frozen_at=OPEN_AT,
            markets=MARKETS,
        )


def test_sample_takes_at_most_thirty_per_market_and_all_when_fewer():
    sample = freeze_review_sample(
        clusters({"za": 45, "ng": 12, "ke": 30}),
        week_start=WEEK_START,
        frozen_at=CLOSED_AT,
        markets=MARKETS,
    )
    assert MAX_SAMPLE_PER_MARKET == 30
    assert sample["week_start"] == WEEK_START
    assert sample["week_end"] == "2026-09-06"
    assert {
        market: values["eligible"] for market, values in sample["markets"].items()
    } == {
        "za": 45,
        "ng": 12,
        "ke": 30,
    }
    assert {
        market: values["sampled"] for market, values in sample["markets"].items()
    } == {
        "za": 30,
        "ng": 12,
        "ke": 30,
    }
    assert sample["markets"]["ng"]["cluster_ids"] == sorted(
        sample["markets"]["ng"]["cluster_ids"]
    )
    assert set(sample["markets"]["ng"]["cluster_ids"]) == {
        f"ng_{index:03d}" for index in range(12)
    }
    assert all(values["gate"] == "sampled" for values in sample["markets"].values())


def test_sample_order_is_seeded_by_cluster_ids_and_week_not_input_order():
    rows = clusters({"za": 40})
    forward = freeze_review_sample(
        rows, week_start=WEEK_START, frozen_at=CLOSED_AT, markets=("za",)
    )
    backward = freeze_review_sample(
        list(reversed(rows)),
        week_start=WEEK_START,
        frozen_at=CLOSED_AT,
        markets=("za",),
    )
    assert (
        forward["markets"]["za"]["cluster_ids"]
        == backward["markets"]["za"]["cluster_ids"]
    )
    assert forward["sample_digest"] == backward["sample_digest"]
    other_week = [
        cluster(f"za_{index:03d}", week_start="2026-09-07") for index in range(40)
    ]
    later = freeze_review_sample(
        other_week,
        week_start="2026-09-07",
        frozen_at="2026-09-14T06:00:00Z",
        markets=("za",),
    )
    assert (
        later["markets"]["za"]["cluster_ids"] != forward["markets"]["za"]["cluster_ids"]
    )
    expected = sorted(
        (row["cluster_id"] for row in rows),
        key=lambda value: hashlib.sha256(
            f"{WEEK_START}|za|{value}".encode()
        ).hexdigest(),
    )[:30]
    assert forward["markets"]["za"]["cluster_ids"] == sorted(expected)
    canonical = dict(forward)
    canonical.pop("sample_digest")
    assert (
        forward["sample_digest"]
        == hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )


def test_ineligible_and_foreign_week_clusters_never_enter_the_sample():
    rows = clusters({"za": 5})
    rows[0]["eligible"] = False
    sample = freeze_review_sample(
        rows, week_start=WEEK_START, frozen_at=CLOSED_AT, markets=("za",)
    )
    assert sample["markets"]["za"] == {
        "eligible": 4,
        "sampled": 4,
        "cluster_ids": ["za_001", "za_002", "za_003", "za_004"],
        "gate": "sampled",
    }
    rows.append(cluster("za_late", week_start="2026-09-07"))
    with pytest.raises(ValueError, match="cluster_week_mismatch"):
        freeze_review_sample(
            rows, week_start=WEEK_START, frozen_at=CLOSED_AT, markets=("za",)
        )


def test_promised_market_without_clusters_leaves_its_gate_unmet():
    sample = freeze_review_sample(
        clusters({"za": 3}), week_start=WEEK_START, frozen_at=CLOSED_AT, markets=MARKETS
    )
    assert sample["markets"]["ng"] == {
        "eligible": 0,
        "sampled": 0,
        "cluster_ids": [],
        "gate": "unmet",
    }
    assert sample["markets"]["ke"]["gate"] == "unmet"
    assert sample["markets"]["za"]["gate"] == "sampled"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda rows: rows.append(cluster("za_000")), "duplicate_cluster_id"),
        (lambda rows: rows[0].__setitem__("eligible", 1), "eligible_flag_required"),
        (lambda rows: rows[0].__setitem__("market", "ZA"), "cluster_market_invalid"),
        (lambda rows: rows[0].pop("week_start"), "cluster_row_invalid"),
        (lambda rows: rows.append(cluster("us_000", "us")), "cluster_market_invalid"),
    ],
)
def test_malformed_clusters_are_refused(mutation, message):
    rows = clusters({"za": 3})
    mutation(rows)
    with pytest.raises(ValueError, match=message):
        freeze_review_sample(
            rows, week_start=WEEK_START, frozen_at=CLOSED_AT, markets=("za",)
        )


def test_scores_use_reviewed_denominators_and_keep_unreviewed_unknown():
    sample = freeze_review_sample(
        clusters({"za": 4, "ng": 2, "ke": 1}),
        week_start=WEEK_START,
        frozen_at=CLOSED_AT,
        markets=MARKETS,
    )
    za_ids = sample["markets"]["za"]["cluster_ids"]
    reviews = [
        review(za_ids[0], "za", coherent=True),
        review(za_ids[1], "za", coherent=False, duplicate=True),
        review(za_ids[2], "za", coherent=True, foreign=True),
        review("ng_000", "ng", coherent=True),
    ]
    result = score_review_sample(sample, reviews)

    assert result["markets"]["za"] == {
        "sampled": 4,
        "reviewed": 3,
        "unreviewed": 1,
        "coherent": 2,
        "duplicate": 1,
        "foreign_market": 1,
        "coherence": 2 / 3,
        "duplication": 1 / 3,
    }
    assert result["markets"]["ng"] == {
        "sampled": 2,
        "reviewed": 1,
        "unreviewed": 1,
        "coherent": 1,
        "duplicate": 0,
        "foreign_market": 0,
        "coherence": 1.0,
        "duplication": 0.0,
    }
    assert result["markets"]["ke"] == {
        "sampled": 1,
        "reviewed": 0,
        "unreviewed": 1,
        "coherent": 0,
        "duplicate": 0,
        "foreign_market": 0,
        "coherence": None,
        "duplication": None,
    }
    assert result["pooled"] == {
        "sampled": 7,
        "reviewed": 4,
        "unreviewed": 3,
        "coherent": 3,
        "duplicate": 1,
        "foreign_market": 1,
        "coherence": 3 / 4,
        "duplication": 1 / 4,
    }
    assert result["complete"] is False
    assert result["sample_digest"] == sample["sample_digest"]


def test_fully_reviewed_sample_is_complete_and_exact():
    sample = freeze_review_sample(
        clusters({"za": 2}), week_start=WEEK_START, frozen_at=CLOSED_AT, markets=("za",)
    )
    reviews = [
        review(cluster_id, "za")
        for cluster_id in sample["markets"]["za"]["cluster_ids"]
    ]
    result = score_review_sample(sample, reviews)
    assert result["complete"] is True
    assert result["pooled"]["coherence"] == 1.0
    assert result["pooled"]["duplication"] == 0.0


def test_empty_review_set_reports_unknown_rates_not_zero():
    sample = freeze_review_sample(
        clusters({"za": 2}), week_start=WEEK_START, frozen_at=CLOSED_AT, markets=("za",)
    )
    result = score_review_sample(sample, [])
    assert result["pooled"]["reviewed"] == 0
    assert result["pooled"]["coherence"] is None
    assert result["pooled"]["duplication"] is None
    assert result["complete"] is False


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda reviews: reviews.append(review("za_000", "za")), "duplicate_review"),
        (
            lambda reviews: reviews.append(review("za_999", "za")),
            "review_outside_sample",
        ),
        (
            lambda reviews: reviews.append(review("za_001", "ng")),
            "review_outside_sample",
        ),
        (
            lambda reviews: reviews[0].__setitem__("cultural_move", ""),
            "cultural_move_required",
        ),
        (
            lambda reviews: reviews[0].__setitem__("coherent", "yes"),
            "review_flag_required",
        ),
        (
            lambda reviews: reviews[0].__setitem__("duplicate", None),
            "review_flag_required",
        ),
        (lambda reviews: reviews[0].pop("reviewer"), "review_row_invalid"),
    ],
)
def test_malformed_reviews_are_refused(mutation, message):
    sample = freeze_review_sample(
        clusters({"za": 2}), week_start=WEEK_START, frozen_at=CLOSED_AT, markets=("za",)
    )
    reviews = [review("za_000", "za")]
    mutation(reviews)
    with pytest.raises(ValueError, match=message):
        score_review_sample(sample, reviews)


def test_scoring_refuses_a_sample_whose_digest_moved():
    sample = freeze_review_sample(
        clusters({"za": 2}), week_start=WEEK_START, frozen_at=CLOSED_AT, markets=("za",)
    )
    sample["markets"]["za"]["cluster_ids"].append("za_extra")
    with pytest.raises(ValueError, match="sample_digest_mismatch"):
        score_review_sample(sample, [])


def test_threshold_gate_reports_met_unmet_and_unknown_with_explicit_thresholds():
    sample = freeze_review_sample(
        clusters({"za": 4, "ng": 2, "ke": 1}),
        week_start=WEEK_START,
        frozen_at=CLOSED_AT,
        markets=MARKETS,
    )
    za_ids = sample["markets"]["za"]["cluster_ids"]
    reviews = [
        review(za_ids[0], "za", coherent=True),
        review(za_ids[1], "za", coherent=False, duplicate=True),
        review(za_ids[2], "za", coherent=True, foreign=True),
        review("ng_000", "ng", coherent=True),
    ]
    result = score_review_sample(sample, reviews)
    gate = gate_review_thresholds(
        result, minimum_coherence=0.7, maximum_duplication=0.2
    )

    assert gate == {
        "sample_digest": sample["sample_digest"],
        "thresholds": {"minimum_coherence": 0.7, "maximum_duplication": 0.2},
        "markets": {"za": "incomplete", "ng": "incomplete", "ke": "unknown"},
        "pooled": "incomplete",
        "complete": False,
    }
    with pytest.raises(TypeError):
        gate_review_thresholds(result)
    with pytest.raises(TypeError):
        gate_review_thresholds(result, minimum_coherence=0.7)
    for bad in (True, -0.1, 1.5, "0.7", None, float("nan")):
        with pytest.raises(ValueError, match="threshold_invalid"):
            gate_review_thresholds(
                result, minimum_coherence=bad, maximum_duplication=0.2
            )
        with pytest.raises(ValueError, match="threshold_invalid"):
            gate_review_thresholds(
                result, minimum_coherence=0.7, maximum_duplication=bad
            )


def test_threshold_gate_stays_incomplete_until_every_sampled_cluster_is_reviewed():
    sample = freeze_review_sample(
        clusters({"za": 4, "ng": 2}),
        week_start=WEEK_START,
        frozen_at=CLOSED_AT,
        markets=("za", "ng"),
    )
    za_ids = sample["markets"]["za"]["cluster_ids"]
    ng_ids = sample["markets"]["ng"]["cluster_ids"]
    first_half = [
        review(za_ids[0], "za", coherent=True),
        review(za_ids[1], "za", coherent=False, duplicate=True),
        review(ng_ids[0], "ng", coherent=True),
    ]
    partial = gate_review_thresholds(
        score_review_sample(sample, first_half),
        minimum_coherence=0.7,
        maximum_duplication=0.2,
    )
    assert partial["markets"] == {"za": "incomplete", "ng": "incomplete"}
    assert partial["pooled"] == "incomplete"
    assert partial["complete"] is False

    second_half = [
        review(za_ids[2], "za", coherent=True),
        review(za_ids[3], "za", coherent=True),
        review(ng_ids[1], "ng", coherent=True),
    ]
    full = gate_review_thresholds(
        score_review_sample(sample, [*first_half, *second_half]),
        minimum_coherence=0.7,
        maximum_duplication=0.2,
    )
    assert full["markets"] == {"za": "unmet", "ng": "met"}
    assert full["pooled"] == "met"
    assert full["complete"] is True
    one_market_done = gate_review_thresholds(
        score_review_sample(sample, [*first_half, *second_half[:2]]),
        minimum_coherence=0.7,
        maximum_duplication=0.2,
    )
    assert one_market_done["markets"] == {"za": "unmet", "ng": "incomplete"}
    assert one_market_done["pooled"] == "incomplete"


def test_threshold_gate_refuses_a_result_whose_rates_were_edited():
    sample = freeze_review_sample(
        clusters({"za": 2}), week_start=WEEK_START, frozen_at=CLOSED_AT, markets=("za",)
    )
    result = score_review_sample(sample, [review("za_000", "za")])
    result["markets"]["za"]["coherence"] = 0.0
    with pytest.raises(ValueError, match="result_inconsistent"):
        gate_review_thresholds(result, minimum_coherence=0.7, maximum_duplication=0.2)


def partial_result():
    sample = freeze_review_sample(
        clusters({"za": 4, "ng": 2}),
        week_start=WEEK_START,
        frozen_at=CLOSED_AT,
        markets=("za", "ng"),
    )
    za_ids = sample["markets"]["za"]["cluster_ids"]
    ng_ids = sample["markets"]["ng"]["cluster_ids"]
    reviews = [
        review(za_ids[0], "za"),
        review(za_ids[1], "za"),
        review(ng_ids[0], "ng"),
    ]
    return sample, reviews, score_review_sample(sample, reviews)


def complete_result():
    sample = freeze_review_sample(
        clusters({"za": 2}), week_start=WEEK_START, frozen_at=CLOSED_AT, markets=("za",)
    )
    ids = sample["markets"]["za"]["cluster_ids"]
    reviews = [review(ids[0], "za"), review(ids[1], "za")]
    return sample, reviews, score_review_sample(sample, reviews)


def test_threshold_gate_refuses_an_edited_unreviewed_count():
    _sample, _reviews, result = partial_result()
    assert result["markets"]["za"]["unreviewed"] == 2
    result["markets"]["za"]["unreviewed"] = 0
    with pytest.raises(ValueError, match="result_inconsistent"):
        gate_review_thresholds(result, minimum_coherence=0.7, maximum_duplication=0.2)


def test_threshold_gate_refuses_an_edited_pooled_unreviewed_count():
    _sample, _reviews, result = partial_result()
    result["pooled"]["unreviewed"] = 0
    with pytest.raises(ValueError, match="result_inconsistent"):
        gate_review_thresholds(result, minimum_coherence=0.7, maximum_duplication=0.2)


def test_threshold_gate_refuses_a_forged_complete_flag():
    _sample, _reviews, result = partial_result()
    result["complete"] = True
    with pytest.raises(ValueError, match="result_inconsistent"):
        gate_review_thresholds(result, minimum_coherence=0.7, maximum_duplication=0.2)


def test_threshold_gate_refuses_a_cleared_complete_flag():
    _sample, _reviews, result = complete_result()
    assert result["complete"] is True
    result["complete"] = False
    with pytest.raises(ValueError, match="result_inconsistent"):
        gate_review_thresholds(result, minimum_coherence=0.7, maximum_duplication=0.2)


def test_threshold_gate_refuses_a_pooled_tally_that_does_not_sum_its_markets():
    _sample, _reviews, result = partial_result()
    assert result["pooled"]["sampled"] == 6
    assert result["pooled"]["reviewed"] == 3
    # The forged pool is internally consistent, it is still incomplete, and the
    # completion flag the result carries is left as scored. So the derived pool
    # and the forged pool agree on completion whichever of the two the gate
    # reads, and the comparison against the sum of the markets is the only
    # thing left that can refuse this result. A forged completion flag and an
    # edited pooled remainder are each their own test.
    result["pooled"] = {
        "sampled": 9,
        "reviewed": 3,
        "unreviewed": 6,
        "coherent": 3,
        "duplicate": 0,
        "foreign_market": 0,
        "coherence": 1.0,
        "duplication": 0.0,
    }
    assert result["complete"] is False
    with pytest.raises(ValueError, match="result_inconsistent"):
        gate_review_thresholds(result, minimum_coherence=0.7, maximum_duplication=0.2)


def test_threshold_gate_refuses_a_flag_count_above_the_reviewed_count():
    """A flag count above the reviewed count is refused by its own bound.

    The pooled count is raised with the market count so the pool stays a
    faithful sum of its markets. Left at zero, the comparison against that sum
    refuses the result before the bound this test is named for is reached, and
    the test passes with that bound deleted.
    """
    _sample, _reviews, result = complete_result()
    result["markets"]["za"]["foreign_market"] = 5
    result["pooled"]["foreign_market"] = 5
    with pytest.raises(ValueError, match="result_inconsistent"):
        gate_review_thresholds(result, minimum_coherence=0.7, maximum_duplication=0.2)


def test_threshold_gate_completion_is_derived_from_the_market_tallies():
    _sample, _reviews, result = complete_result()
    gate = gate_review_thresholds(
        result, minimum_coherence=0.7, maximum_duplication=0.2
    )
    assert gate["complete"] is True
    assert gate["markets"] == {"za": "met"}
    assert gate["pooled"] == "met"


def test_threshold_gate_refuses_a_boolean_where_a_count_belongs():
    """A flag is not a count, so a boolean in a count slot is refused."""
    _sample, _reviews, result = complete_result()
    result["markets"]["za"]["duplicate"] = False
    result["pooled"]["duplicate"] = False
    with pytest.raises(ValueError, match="result_inconsistent"):
        gate_review_thresholds(result, minimum_coherence=0.7, maximum_duplication=0.2)


@pytest.mark.parametrize("rate", [True, 1, 0])
def test_threshold_gate_refuses_a_rate_that_is_not_a_fraction(rate):
    """Both rate slots hold a fraction, so an integer or a flag is refused."""
    _sample, _reviews, result = complete_result()
    result["markets"]["za"]["coherence"] = rate
    result["pooled"]["coherence"] = rate
    result["markets"]["za"]["coherent"] = 2 if rate else 0
    result["pooled"]["coherent"] = 2 if rate else 0
    with pytest.raises(ValueError, match="result_inconsistent"):
        gate_review_thresholds(result, minimum_coherence=0.7, maximum_duplication=0.2)


def test_threshold_gate_refuses_a_tally_carrying_a_field_it_never_derives():
    """A tally is its exact field set, so an added field is refused."""
    _sample, _reviews, result = complete_result()
    result["markets"]["za"]["reviewer_note"] = "looked fine"
    with pytest.raises(ValueError, match="result_inconsistent"):
        gate_review_thresholds(result, minimum_coherence=0.7, maximum_duplication=0.2)


def test_threshold_gate_refuses_a_completion_flag_that_is_not_a_boolean():
    """Completion is compared by identity, so a truthy stand-in is refused."""
    _sample, _reviews, result = complete_result()
    assert result["complete"] is True
    result["complete"] = 1
    with pytest.raises(ValueError, match="result_inconsistent"):
        gate_review_thresholds(result, minimum_coherence=0.7, maximum_duplication=0.2)


def test_a_sample_with_nothing_in_it_is_never_complete():
    """Nothing reviewed out of nothing sampled is unknown, never complete."""
    sample = freeze_review_sample(
        [], week_start=WEEK_START, frozen_at=CLOSED_AT, markets=("za",)
    )
    result = score_review_sample(sample, [])
    assert result["pooled"]["sampled"] == 0
    assert result["pooled"]["unreviewed"] == 0
    assert result["complete"] is False
    gate = gate_review_thresholds(
        result, minimum_coherence=0.7, maximum_duplication=0.2
    )
    assert gate["complete"] is False
    assert gate["pooled"] == "unknown"


@pytest.mark.parametrize("field", ["markets", "pooled"])
def test_threshold_gate_names_the_inconsistency_when_a_tally_is_a_list(field):
    """Callers catch the inconsistency error, so no other error escapes."""
    _sample, _reviews, result = complete_result()
    tally = result[field]
    result[field] = [tally] if field == "pooled" else list(tally.values())
    with pytest.raises(ValueError, match="result_inconsistent"):
        gate_review_thresholds(result, minimum_coherence=0.7, maximum_duplication=0.2)


def test_threshold_gate_refuses_a_tally_reviewed_more_than_it_sampled():
    """More reviews than clusters sampled is refused before anything reads it.

    Such a tally derives a negative remainder. A negative remainder does not
    read as incomplete, and in the pooled sum it cancels another market's
    genuine unreviewed remainder, so the pool can read complete while clusters
    are still unreviewed, which is the state this gate exists to prevent.

    Every other field here follows from the forged counts and the pool is a
    faithful sum of them, so the bound on the reviewed count is the only thing
    that can refuse this result.
    """
    _sample, _reviews, result = complete_result()
    forged = {
        "sampled": 2,
        "reviewed": 5,
        "unreviewed": -3,
        "coherent": 5,
        "duplicate": 0,
        "foreign_market": 0,
        "coherence": 1.0,
        "duplication": 0.0,
    }
    result["markets"]["za"] = dict(forged)
    result["pooled"] = dict(forged)
    result["complete"] = False
    with pytest.raises(ValueError, match="result_inconsistent"):
        gate_review_thresholds(result, minimum_coherence=0.7, maximum_duplication=0.2)


def test_threshold_gate_refuses_a_count_below_zero():
    """A count is never negative, and no rate here would betray this one.

    The flag counts are read out of the tally and written straight back into
    the derived tally, so comparing them establishes nothing; a negative flag
    count passes every other comparison in the function.
    """
    _sample, _reviews, result = complete_result()
    result["markets"]["za"]["foreign_market"] = -1
    result["pooled"]["foreign_market"] = -1
    with pytest.raises(ValueError, match="result_inconsistent"):
        gate_review_thresholds(result, minimum_coherence=0.7, maximum_duplication=0.2)
