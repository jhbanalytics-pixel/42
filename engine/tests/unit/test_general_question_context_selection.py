# ruff: noqa: C408
# The first test is the plan's regression, kept verbatim, including its dict() calls.
import copy
import random
from fractions import Fraction

import pytest
from src.analysis.open_intelligence import general_question_context_selection as subject
from src.analysis.open_intelligence.general_question_context_selection import select_context_facts


def test_relevance_and_all_markets_survive_the_cap():
    facts = [
        dict(
            market="ke",
            row_id=str(i),
            content_digest=str(i),
            excerpt="airport opening",
            source_label="publisher",
            source_family="news",
            platform="web",
            published_at=None,
            collected_at="2026-09-01T00:00:00Z",
            lane="raw_content",
        )
        for i in range(240)
    ]
    for market in ("ke", "ng", "za"):
        facts.append(
            dict(
                facts[0],
                market=market,
                row_id="household-" + market,
                content_digest="household-" + market,
                excerpt="households change commuting routines",
            )
        )
    requirements = [dict(requirement_id="r1", mandatory=True, search_terms=["commuting"])]
    selected = select_context_facts(facts, requirements, ["ke", "ng", "za"], limit=3)
    assert {f["market"] for f in selected["facts"]} == {"ke", "ng", "za"}
    assert len(selected["coverage"]["r1"]) == 3
    assert selected["omitted_eligible_count"] == 0
    assert selected["uncovered_required_pairs"] == []


CORPUS_LIMIT = 9
FILLER = "The council published the meeting minutes without comment. "
FACT_FIELDS = (
    "market",
    "row_id",
    "content_digest",
    "excerpt",
    "source_label",
    "source_family",
    "platform",
    "published_at",
    "collected_at",
    "lane",
)


def identity(fact):
    return (fact["market"], fact["row_id"], fact["content_digest"])


def fact(**overrides):
    base = {
        "market": "ke",
        "row_id": "row",
        "content_digest": "digest",
        "excerpt": "households change commuting routines",
        "source_label": "publisher",
        "source_family": "news",
        "platform": "web",
        "published_at": None,
        "collected_at": "2026-09-01T00:00:00Z",
        "lane": "raw_content",
    }
    base.update(overrides)
    if "content_digest" not in overrides:
        base["content_digest"] = "digest-" + base["row_id"]
    return base


def requirement(requirement_id, terms, *, mandatory=True):
    return {"requirement_id": requirement_id, "mandatory": mandatory, "search_terms": terms}


# Labelled relevance corpus. Labels are independent judgements about the question and never
# reach the selector; the tests compute precision and recall from them after selection.
LABELLED_CORPUS = {
    "description": "Labelled relevance corpus for the pure context selector. Labels are "
    "independent human judgements about the question and never feed the selector; "
    "the test computes precision and recall from them after selection.",
    "question": "How are commuting routines changing as public transport fares move, compared "
    "with fuel prices?",
    "markets": ["ke", "ng", "za"],
    "requirements": [
        {"requirement_id": "r1", "mandatory": True, "search_terms": ["commuting"]},
        {"requirement_id": "r2", "mandatory": True, "search_terms": ["matatu fares"]},
        {"requirement_id": "r3", "mandatory": False, "search_terms": ["fuel price"]},
    ],
    "relations": [
        {"requirement_id": "r1", "method": "alias", "term": "safari za kazi"},
        {
            "requirement_id": "r1",
            "method": "entity",
            "term": "Gautrain",
            "identity": {
                "market": "za",
                "row_id": "gautrain-za",
                "content_digest": "digest-gautrain-za",
            },
        },
        {
            "requirement_id": "r2",
            "method": "topic",
            "term": "public_transport_fares",
            "identity": {
                "market": "za",
                "row_id": "minibus-za",
                "content_digest": "digest-minibus-za",
            },
        },
    ],
    "generated": [
        {
            "case": "early_irrelevant",
            "label": "irrelevant",
            "count": 240,
            "row_id_prefix": "bulk-",
            "fact": {
                "market": "ke",
                "excerpt": "airport opening",
                "source_label": "publisher",
                "source_family": "news",
                "platform": "web",
                "published_at": None,
                "collected_at": "2026-09-01T00:00:00Z",
                "lane": "raw_content",
            },
        },
        {
            "case": "literal_irrelevant",
            "label": "irrelevant",
            "count": 24,
            "row_id_prefix": "lit-",
            "fact": {
                "market": "ke",
                "excerpt": "Airport staff commuting allowance confirmed for the opening weekend",
                "source_label": "publisher",
                "source_family": "news",
                "platform": "web",
                "published_at": "2026-09-02T00:00:00Z",
                "collected_at": "2026-09-02T01:00:00Z",
                "lane": "raw_content",
            },
        },
    ],
    "records": [
        {
            "case": "comparison_source_ke",
            "label": "relevant",
            "market": "ke",
            "row_id": "households-ke",
            "content_digest": "digest-households-ke",
            "excerpt": "Households change commuting routines as matatu fares rise",
            "source_label": "daily nation",
            "source_family": "news",
            "platform": "web",
            "published_at": "2026-09-05T08:00:00Z",
            "collected_at": "2026-09-05T09:00:00Z",
            "lane": "enriched_content",
        },
        {
            "case": "local_language_ke",
            "label": "relevant",
            "market": "ke",
            "row_id": "local-language-ke",
            "content_digest": "digest-local-language-ke",
            "excerpt": "Abiria wanabadilisha safari za kazi kila siku baada ya nauli kupanda",
            "source_label": "x account",
            "source_family": "social",
            "platform": "x",
            "published_at": "2026-09-05T07:00:00Z",
            "collected_at": "2026-09-05T09:00:00Z",
            "lane": "enriched_content",
        },
        {
            "case": "paraphrase_za",
            "label": "relevant",
            "market": "za",
            "row_id": "gautrain-za",
            "content_digest": "digest-gautrain-za",
            "excerpt": "Gautrain riders now reach the office by minibus after the fare change",
            "source_label": "x account",
            "source_family": "social",
            "platform": "x",
            "published_at": "2026-09-04T09:00:00Z",
            "collected_at": "2026-09-04T10:00:00Z",
            "lane": "enriched_content",
        },
        {
            "case": "comparator_za",
            "label": "relevant",
            "market": "za",
            "row_id": "minibus-za",
            "content_digest": "digest-minibus-za",
            "excerpt": "Minibus taxi fares rise in Gauteng as operators cite diesel costs",
            "source_label": "news24",
            "source_family": "news",
            "platform": "web",
            "published_at": "2026-09-04T10:00:00Z",
            "collected_at": "2026-09-04T11:00:00Z",
            "lane": "enriched_content",
        },
        {
            "case": "comparator_ke",
            "label": "relevant",
            "market": "ke",
            "row_id": "matatu-fuel-ke",
            "content_digest": "digest-matatu-fuel-ke",
            "excerpt": "Matatu fares climb after the fuel price hike",
            "source_label": "the standard",
            "source_family": "news",
            "platform": "web",
            "published_at": "2026-09-03T12:00:00Z",
            "collected_at": "2026-09-03T13:00:00Z",
            "lane": "enriched_content",
        },
        {
            "case": "term_after_4000",
            "label": "relevant",
            "market": "ke",
            "row_id": "long-ke",
            "content_digest": "digest-long-ke",
            "excerpt_padding_chars": 4100,
            "excerpt": "Commuting routines are shifting toward earlier departures across the city",
            "source_label": "county gazette",
            "source_family": "news",
            "platform": "web",
            "published_at": "2026-09-05T06:00:00Z",
            "collected_at": "2026-09-05T09:00:00Z",
            "lane": "enriched_content",
        },
        {
            "case": "duplicated_title_a",
            "label": "relevant",
            "market": "ke",
            "row_id": "dup-a-ke",
            "content_digest": "digest-dup-a-ke",
            "excerpt": "Commuting routines shift as households adapt to new fares",
            "source_label": "facebook page",
            "source_family": "social",
            "platform": "facebook",
            "published_at": "2026-09-04T12:00:00Z",
            "collected_at": "2026-09-04T13:00:00Z",
            "lane": "raw_content",
        },
        {
            "case": "duplicated_title_b",
            "label": "relevant",
            "market": "ke",
            "row_id": "dup-b-ke",
            "content_digest": "digest-dup-b-ke",
            "excerpt": "Commuting routines shift as households adapt to new fares",
            "source_label": "facebook page",
            "source_family": "social",
            "platform": "facebook",
            "published_at": "2026-09-04T12:00:00Z",
            "collected_at": "2026-09-04T13:00:00Z",
            "lane": "raw_content",
        },
        {
            "case": "foreign_local_counterexample",
            "label": "irrelevant",
            "market": "gh",
            "row_id": "accra-gh",
            "content_digest": "digest-accra-gh",
            "excerpt": "Accra commuting routines change as the new bus corridor opens",
            "source_label": "ghana web",
            "source_family": "news",
            "platform": "web",
            "published_at": "2026-09-05T09:00:00Z",
            "collected_at": "2026-09-05T10:00:00Z",
            "lane": "enriched_content",
        },
    ],
}


def load_corpus():
    """Materialise the labelled corpus. Labels stay beside the records, never in the selector."""
    corpus = copy.deepcopy(LABELLED_CORPUS)
    facts, labels = [], {}
    for group in corpus["generated"]:
        for index in range(group["count"]):
            row_id = f"{group['row_id_prefix']}{index:03d}"
            item = dict(group["fact"], row_id=row_id, content_digest="digest-" + row_id)
            facts.append(item)
            labels[identity(item)] = (group["case"], group["label"])
    for record in corpus["records"]:
        item = {field: record[field] for field in FACT_FIELDS}
        padding = record.get("excerpt_padding_chars", 0)
        if padding:
            item["excerpt"] = (FILLER * (padding // len(FILLER) + 1))[:padding] + item["excerpt"]
        facts.append(item)
        labels[identity(item)] = (record["case"], record["label"])
    return corpus, facts, labels


def select_corpus(corpus, facts):
    return select_context_facts(
        facts,
        corpus["requirements"],
        corpus["markets"],
        limit=CORPUS_LIMIT,
        relations=corpus["relations"],
    )


def corpus_metrics(result, labels):
    selected = [identity(item) for item in result["facts"]]
    relevant = {key for key, (_, label) in labels.items() if label == "relevant"}
    hits = [key for key in selected if key in relevant]
    precision = Fraction(len(hits), len(selected)) if selected else Fraction(0)
    recall = Fraction(len(hits), len(relevant))
    return selected, relevant, precision, recall


def assert_corpus_expectations(result, labels):
    selected, _relevant, precision, recall = corpus_metrics(result, labels)
    assert len(result["facts"]) == CORPUS_LIMIT, "eligible evidence survived the cap"
    assert result["uncovered_required_pairs"] == [
        {"requirement_id": "r1", "market": "ng"},
        {"requirement_id": "r2", "market": "ng"},
    ], "uncovered required pairs name the absent market"
    assert recall == 1, f"labelled recall {recall} below 1"
    assert precision == Fraction(8, 9), f"labelled precision {precision}"
    assert {item["market"] for item in result["facts"]} == {"ke", "za"}, "markets"
    assert selected == [
        ("ke", "households-ke", "digest-households-ke"),
        ("za", "gautrain-za", "digest-gautrain-za"),
        ("za", "minibus-za", "digest-minibus-za"),
        ("ke", "matatu-fuel-ke", "digest-matatu-fuel-ke"),
        ("ke", "local-language-ke", "digest-local-language-ke"),
        ("ke", "long-ke", "digest-long-ke"),
        ("ke", "dup-a-ke", "digest-dup-a-ke"),
        ("ke", "lit-000", "digest-lit-000"),
        ("ke", "dup-b-ke", "digest-dup-b-ke"),
    ], "allocation order"
    assert result["omitted_eligible_count"] == 23, "omitted eligible count"
    by_key = {identity(item): item for item in result["facts"]}
    long_fact = by_key[("ke", "long-ke", "digest-long-ke")]
    assert long_fact["excerpt"].lower().index("commuting") > 4000
    assert long_fact["retrieval_signals"] == [
        {"requirement_id": "r1", "method": "normalized_literal", "term": "commuting"}
    ]
    assert by_key[("ke", "local-language-ke", "digest-local-language-ke")]["retrieval_signals"] == [
        {"requirement_id": "r1", "method": "alias", "term": "safari za kazi"}
    ]
    assert by_key[("za", "gautrain-za", "digest-gautrain-za")]["retrieval_signals"] == [
        {"requirement_id": "r1", "method": "entity", "term": "Gautrain"}
    ]
    assert by_key[("za", "minibus-za", "digest-minibus-za")]["retrieval_signals"] == [
        {"requirement_id": "r2", "method": "topic", "term": "public_transport_fares"}
    ]
    assert ("gh", "accra-gh", "digest-accra-gh") not in by_key
    assert result["coverage"] == {
        "r1": [0, 1, 4, 5, 6, 7, 8],
        "r2": [0, 2, 3],
        "r3": [3],
    }


@pytest.fixture
def corpus():
    return load_corpus()


def test_labelled_corpus_relevant_rows_survive_the_cap(corpus):
    spec, facts, labels = corpus
    literal_irrelevant = [key for key, (case, _) in labels.items() if case == "literal_irrelevant"]
    first_relevant = min(
        index for index, item in enumerate(facts) if labels[identity(item)][1] == "relevant"
    )
    assert len(literal_irrelevant) >= 24
    assert all(
        facts.index(item) < first_relevant for item in facts if identity(item) in literal_irrelevant
    )
    assert sum(1 for item in facts if item["excerpt"] == "airport opening") == 240
    assert not any(item["market"] == "ng" for item in facts)
    duplicated = [item for item in facts if item["excerpt"].startswith("Commuting routines shift")]
    assert len(duplicated) == 2
    assert len({identity(item) for item in duplicated}) == 2
    result = select_corpus(spec, facts)
    assert_corpus_expectations(result, labels)
    original_excerpts = {identity(item): item["excerpt"] for item in facts}
    for item in result["facts"]:
        assert item["excerpt"] == original_excerpts[identity(item)]
    assert not any("retrieval_signals" in item for item in facts)


def test_shuffled_input_order_does_not_change_the_result(corpus):
    spec, facts, _labels = corpus
    baseline = select_corpus(spec, facts)
    shuffled_facts = copy.deepcopy(facts)
    random.Random(42).shuffle(shuffled_facts)
    shuffled_spec = copy.deepcopy(spec)
    shuffled_spec["requirements"].reverse()
    shuffled_spec["markets"].reverse()
    shuffled_spec["relations"].reverse()
    assert select_corpus(shuffled_spec, shuffled_facts) == baseline


def test_mutation_cap_before_relevance_fails_the_survival_assertion(corpus, monkeypatch):
    spec, facts, labels = corpus
    original = subject._match

    def capped_before_relevance(items, requirements):
        return original(items[:CORPUS_LIMIT], requirements) + [{} for _ in items[CORPUS_LIMIT:]]

    monkeypatch.setattr(subject, "_match", capped_before_relevance)
    result = select_corpus(spec, facts)
    with pytest.raises(AssertionError, match="survived the cap"):
        assert_corpus_expectations(result, labels)


def test_mutation_lexical_market_ordering_fails_the_coverage_assertion(corpus, monkeypatch):
    spec, facts, labels = corpus

    def lexical_market_ordering(candidates, requirements, markets, limit):
        chosen = sorted(candidates, key=lambda item: identity(item["fact"]))[:limit]
        covered = {(rid, item["fact"]["market"]) for item in chosen for rid in item["signals"]}
        uncovered = [
            {"requirement_id": rid, "market": market}
            for rid, item in sorted(requirements.items())
            if item["mandatory"]
            for market in sorted(markets)
            if (rid, market) not in covered
        ]
        return chosen, uncovered

    monkeypatch.setattr(subject, "_allocate", lexical_market_ordering)
    result = select_corpus(spec, facts)
    with pytest.raises(AssertionError, match="uncovered required pairs"):
        assert_corpus_expectations(result, labels)


def test_mutation_clipped_excerpt_matching_fails_the_recall_assertion(corpus, monkeypatch):
    spec, facts, labels = corpus
    original = subject._normalize
    monkeypatch.setattr(subject, "_normalize", lambda text: original(text[:4000]))
    result = select_corpus(spec, facts)
    with pytest.raises(AssertionError, match="labelled recall"):
        assert_corpus_expectations(result, labels)


@pytest.mark.parametrize("limit", [0, -1, "3", True, 2.0, None])
def test_invalid_limit_is_rejected(limit):
    with pytest.raises(ValueError, match="context_selection_invalid"):
        select_context_facts([fact()], [requirement("r1", ["commuting"])], ["ke"], limit=limit)


def test_duplicate_identity_is_rejected():
    facts = [fact(row_id="a"), fact(row_id="a")]
    with pytest.raises(ValueError, match="context_selection_invalid"):
        select_context_facts(facts, [requirement("r1", ["commuting"])], ["ke"], limit=2)


@pytest.mark.parametrize(
    "mutation",
    [
        {"drop": "content_digest"},
        {"drop": "source_family"},
        {"market": ""},
        {"excerpt": None},
        {"collected_at": "2026-09-01T00:00:00"},
        {"published_at": "not a time"},
    ],
)
def test_malformed_fact_is_rejected(mutation):
    item = fact()
    if "drop" in mutation:
        del item[mutation["drop"]]
    else:
        item.update(mutation)
    with pytest.raises(ValueError, match="context_selection_invalid"):
        select_context_facts([item], [requirement("r1", ["commuting"])], ["ke"], limit=1)


@pytest.mark.parametrize(
    ("markets", "requirements"),
    [
        ([], [requirement("r1", ["commuting"])]),
        (["ke", "ke"], [requirement("r1", ["commuting"])]),
        (["ke"], []),
        (["ke"], [requirement("r1", ["commuting"]), requirement("r1", ["fares"])]),
        (["ke"], [requirement("r1", [" "])]),
        (["ke"], [dict(requirement("r1", ["commuting"]), mandatory="yes")]),
    ],
)
def test_malformed_scope_is_rejected(markets, requirements):
    with pytest.raises(ValueError, match="context_selection_invalid"):
        select_context_facts([fact()], requirements, markets, limit=1)


@pytest.mark.parametrize(
    "relation",
    [
        {"requirement_id": "r9", "method": "alias", "term": "commute"},
        {"requirement_id": "r1", "method": "guess", "term": "commute"},
        {"requirement_id": "r1", "method": "alias", "term": ""},
        {
            "requirement_id": "r1",
            "method": "alias",
            "term": "commute",
            "identity": {"market": "ke", "row_id": "row", "content_digest": "digest-row"},
        },
        {"requirement_id": "r1", "method": "entity", "term": "Gautrain"},
        {"requirement_id": "r1", "method": "topic", "term": "fares", "identity": {"row_id": "row"}},
    ],
)
def test_malformed_relation_is_rejected(relation):
    with pytest.raises(ValueError, match="context_selection_invalid"):
        select_context_facts(
            [fact()], [requirement("r1", ["commuting"])], ["ke"], limit=1, relations=[relation]
        )


def test_matches_are_computed_per_requirement_without_union():
    facts = [
        fact(row_id="taxi", excerpt="Electric taxi pilot launches in Nairobi"),
        fact(row_id="matatu", excerpt="Matatu fares rise on the Thika road"),
        fact(row_id="both", excerpt="Electric taxi rivals the matatu on price"),
    ]
    requirements = [requirement("r1", ["electric taxi"]), requirement("r2", ["matatu"])]
    result = select_context_facts(facts, requirements, ["ke"], limit=3)
    by_key = {item["row_id"]: item for item in result["facts"]}
    assert set(by_key) == {"taxi", "matatu", "both"}
    assert [item["requirement_id"] for item in by_key["taxi"]["retrieval_signals"]] == ["r1"]
    assert [item["requirement_id"] for item in by_key["matatu"]["retrieval_signals"]] == ["r2"]
    assert [item["requirement_id"] for item in by_key["both"]["retrieval_signals"]] == ["r1", "r2"]
    ordered = [item["row_id"] for item in result["facts"]]
    assert ordered == ["both", "matatu", "taxi"]
    assert result["coverage"]["r1"] == sorted([0, ordered.index("taxi")])
    assert result["coverage"]["r2"] == sorted([0, ordered.index("matatu")])
    assert result["uncovered_required_pairs"] == []


def test_normalized_literal_match_is_one_recorded_signal():
    facts = [fact(row_id="spaced", excerpt="Households change COMMUTING\n   routines daily")]
    requirements = [requirement("r1", ["Commuting routines"])]
    result = select_context_facts(facts, requirements, ["ke"], limit=1)
    assert result["facts"][0]["excerpt"] == "Households change COMMUTING\n   routines daily"
    assert result["facts"][0]["retrieval_signals"] == [
        {"requirement_id": "r1", "method": "normalized_literal", "term": "commuting routines"}
    ]
    assert set(result["facts"][0]) == set(FACT_FIELDS) | {"retrieval_signals"}


def test_empty_term_list_is_a_broad_requirement_not_universal_support():
    facts = [
        fact(row_id="fares", excerpt="Matatu fares rise on the Thika road"),
        fact(row_id="weather", excerpt="Rain expected across the coast"),
    ]
    requirements = [requirement("r1", []), requirement("r2", ["matatu"], mandatory=False)]
    result = select_context_facts(facts, requirements, ["ke"], limit=2)
    by_key = {item["row_id"]: item for item in result["facts"]}
    assert by_key["weather"]["retrieval_signals"] == [
        {"requirement_id": "r1", "method": "broad", "term": None}
    ]
    assert by_key["fares"]["retrieval_signals"] == [
        {"requirement_id": "r1", "method": "broad", "term": None},
        {"requirement_id": "r2", "method": "normalized_literal", "term": "matatu"},
    ]
    assert result["coverage"]["r2"] == [result["facts"].index(by_key["fares"])]
    assert result["uncovered_required_pairs"] == []


def test_nonliteral_relation_matches_keep_their_method_and_ignore_unknown_rows():
    facts = [
        fact(row_id="paraphrase", excerpt="Riders now reach the office by minibus"),
        fact(row_id="swahili", excerpt="Abiria wanabadilisha safari za kazi"),
        fact(row_id="other", excerpt="Rain expected across the coast"),
    ]
    relations = [
        {
            "requirement_id": "r1",
            "method": "entity",
            "term": "Gautrain",
            "identity": {
                "market": "ke",
                "row_id": "paraphrase",
                "content_digest": "digest-paraphrase",
            },
        },
        {
            "requirement_id": "r1",
            "method": "topic",
            "term": "commuting_routines",
            "identity": {"market": "ke", "row_id": "missing", "content_digest": "digest-missing"},
        },
        {"requirement_id": "r1", "method": "alias", "term": "Safari za Kazi"},
    ]
    result = select_context_facts(
        facts, [requirement("r1", ["commuting"])], ["ke"], limit=3, relations=relations
    )
    by_key = {item["row_id"]: item for item in result["facts"]}
    assert set(by_key) == {"paraphrase", "swahili"}
    assert by_key["paraphrase"]["retrieval_signals"] == [
        {"requirement_id": "r1", "method": "entity", "term": "Gautrain"}
    ]
    assert by_key["swahili"]["retrieval_signals"] == [
        {"requirement_id": "r1", "method": "alias", "term": "safari za kazi"}
    ]
    assert result["omitted_eligible_count"] == 0


def test_partial_result_reports_uncovered_pairs_and_never_drops_a_market():
    facts = [
        fact(row_id=f"{market}-{term}", market=market, excerpt=f"{term} in {market}")
        for market in ("ke", "ng", "za")
        for term in ("alpha", "beta")
    ]
    requirements = [requirement("r2", ["beta"]), requirement("r1", ["alpha"])]
    result = select_context_facts(facts, requirements, ["za", "ng", "ke"], limit=4)
    assert [item["row_id"] for item in result["facts"]] == [
        "ke-alpha",
        "ng-alpha",
        "za-alpha",
        "ke-beta",
    ]
    assert result["uncovered_required_pairs"] == [
        {"requirement_id": "r2", "market": "ng"},
        {"requirement_id": "r2", "market": "za"},
    ]
    assert result["omitted_eligible_count"] == 2
    absent = select_context_facts(facts, requirements, ["ke", "ng", "tz"], limit=10)
    assert result["coverage"] == {"r1": [0, 1, 2], "r2": [3]}
    assert absent["uncovered_required_pairs"] == [
        {"requirement_id": "r1", "market": "tz"},
        {"requirement_id": "r2", "market": "tz"},
    ]
    assert {item["market"] for item in absent["facts"]} == {"ke", "ng"}
    assert absent["omitted_eligible_count"] == 0


def test_optional_pairs_follow_mandatory_pairs_and_fill_order_is_deterministic():
    facts = [
        fact(row_id="p", excerpt="alpha beta", published_at="2026-09-05T00:00:00Z"),
        fact(row_id="n1", excerpt="alpha only", published_at="2026-09-04T00:00:00Z"),
        fact(row_id="n2", excerpt="alpha only", published_at="2026-09-03T00:00:00Z"),
        fact(row_id="n3", excerpt="alpha only", published_at="2026-09-02T00:00:00Z"),
        fact(
            row_id="s1",
            excerpt="alpha only",
            source_family="social",
            published_at="2026-09-01T00:00:00Z",
        ),
        fact(
            row_id="s2",
            excerpt="alpha beta",
            source_family="social",
            published_at="2026-09-01T00:00:00Z",
        ),
        fact(
            row_id="g",
            excerpt="gamma only",
            source_family="social",
            published_at="2026-09-06T00:00:00Z",
        ),
    ]
    requirements = [
        requirement("r1", ["alpha"]),
        requirement("r2", ["beta"], mandatory=False),
        requirement("r3", ["gamma"], mandatory=False),
    ]
    result = select_context_facts(facts, requirements, ["ke"], limit=6)
    assert [item["row_id"] for item in result["facts"]] == ["p", "g", "s2", "n1", "n2", "s1"]
    assert result["coverage"] == {"r1": [0, 2, 3, 4, 5], "r2": [0, 2], "r3": [1]}
    assert result["omitted_eligible_count"] == 1
    assert result["uncovered_required_pairs"] == []


def test_observed_time_ties_break_by_lane_then_identity():
    facts = [
        fact(row_id="b", lane="raw_content"),
        fact(row_id="a", lane="raw_content"),
        fact(row_id="c", lane="enriched_content"),
    ]
    result = select_context_facts(facts, [requirement("r1", ["commuting"])], ["ke"], limit=2)
    assert [item["row_id"] for item in result["facts"]] == ["c", "a"]
    assert result["omitted_eligible_count"] == 1


def test_published_time_outranks_collected_time_when_present():
    facts = [
        fact(
            row_id="old-published",
            published_at="2026-09-01T00:00:00Z",
            collected_at="2026-09-09T00:00:00Z",
        ),
        fact(row_id="unpublished", published_at=None, collected_at="2026-09-03T00:00:00Z"),
    ]
    result = select_context_facts(facts, [requirement("r1", ["commuting"])], ["ke"], limit=1)
    assert [item["row_id"] for item in result["facts"]] == ["unpublished"]
