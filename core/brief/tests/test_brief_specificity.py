import importlib

import pytest

from core.trust.claims import _quote_fault, located_market, source_market


EXPLANATION = "Creators started posting the dance before Heritage Day weekend."
SOURCE_ONE = "We started dancing tonight at the braai."
SOURCE_TWO = "Creators made it their own this weekend."


def post(evidence_id, *, market=None, source=None, flags=None, text=SOURCE_ONE, quote_text=None):
    record = {
        "id": evidence_id,
        "market": market,
        "source_market": source,
        "flags": flags or [],
        "text": text,
    }
    if quote_text is not None:
        record["quote_text"] = quote_text
    return record


def claims_with_quote(quote, *, quote_evidence_id="located", cited_ids=None, second_evidence_id="feed"):
    return [
        {"id": "c1", "evidence_ids": cited_ids or ["located"],
         "quotes": [{"evidence_id": quote_evidence_id, "text": quote}]},
        {"id": "c2", "evidence_ids": [second_evidence_id], "quotes": []},
    ]


def good_evidence():
    return [
        post("located", market="ZA", text=SOURCE_ONE),
        post("feed", source="ZA", flags=["market_assumed"], text=SOURCE_TWO),
        post("foreign", market="NG", source="NG", text="People post this in Lagos."),
        post("assumed", market="ZA", flags=["market_assumed"]),
        post("unknown"),
    ]


def specificity():
    return importlib.import_module("core.brief.specificity")


def basis(*, claims=None, evidence=None, refs=None, explanation=EXPLANATION):
    return specificity().specificity_basis(
        explanation=explanation,
        claims=claims if claims is not None else claims_with_quote("We started dancing tonight"),
        explanation_claim_ids=refs if refs is not None else ["c1", "c2"],
        evidence=evidence if evidence is not None else good_evidence(),
        market="ZA",
    )


def test_existing_location_and_quote_helpers_keep_their_source_boundaries():
    source_only = post("feed", source="ZA", flags=["market_assumed"])
    assumed_only = post("assumed", market="ZA", flags=["market_assumed"])

    assert located_market(source_only) is None
    assert source_market(source_only) == "ZA"
    assert located_market(assumed_only) is None
    assert source_market(assumed_only) is None
    assert _quote_fault("started dancing", SOURCE_ONE) is None
    assert _quote_fault("tarted dancin", SOURCE_ONE) == "not verbatim on word boundaries"


def test_local_posts_keeps_distinct_located_and_matching_feed_records_in_input_order():
    rows = [
        post("located", market="ZA"),
        post("feed", source="ZA", flags=["market_assumed"]),
        post("located", market="NG", source="NG"),
        post("assumed", market="ZA", flags=["market_assumed"]),
        post("foreign", market="NG", source="NG"),
        post("foreign-feed", market="NG", source="ZA"),
        post("unknown"),
    ]

    assert [row["id"] for row in specificity().local_posts(rows, "ZA")] == [
        "located",
        "feed",
    ]


def test_showable_posts_are_the_unlocated_and_the_local_posts_as_the_gate_counts_them():
    rows = [
        post("located", market="ZA"),
        post("feed", source="ZA", flags=["market_assumed"]),
        post("foreign", market="NG", source="NG"),
        post("foreign-feed", market="NG", source="ZA"),
        post("unknown"),
    ]

    assert [row["id"] for row in specificity().showable_posts(rows, "ZA")] == [
        "located",
        "feed",
        "unknown",
    ]


@pytest.mark.parametrize("market", ["ZA", "NG", "KE"])
@pytest.mark.parametrize("location,source,flags,is_local,is_showable", [
    ("AE", "own", [], False, False),
    ("AE", None, [], False, False),
    ("own", None, [], True, True),
    (None, "own", ["market_assumed"], True, True),
    (None, None, [], False, True),
    (None, "invalid", [], False, True),
    ("", "own", [], True, True),
])
def test_foreign_location_veto_preserves_unlocated_own_feed_evidence(
        market, location, source, flags, is_local, is_showable):
    record = post("p", market=market if location == "own" else location,
                  source=market if source == "own" else source, flags=flags)
    original = dict(record)

    assert specificity().local_posts([record], market) == ([record] if is_local else [])
    assert specificity().showable_posts([record], market) == ([record] if is_showable else [])
    assert record == original


def test_local_posts_deduplicates_repeated_eligible_evidence_ids():
    rows = [post("repeat", market="ZA"), post("repeat", market="ZA")]

    assert [row["id"] for row in specificity().local_posts(rows, "ZA")] == ["repeat"]


def test_local_posts_ignores_non_record_rows_and_invalid_evidence_ids():
    rows = [
        None,
        "not a record",
        {},
        {"id": None, "market": "ZA"},
        {"id": "  ", "market": "ZA"},
        {"id": ["post"], "market": "ZA"},
    ]

    assert specificity().local_posts(rows, "ZA") == []


@pytest.mark.parametrize(
    "flags",
    [
        "",
        "market_assumed",
        0,
        1,
        {"market_assumed": True},
        ["market_assumed", 1],
        (1,),
    ],
)
def test_local_posts_rejects_malformed_flag_containers(flags):
    record = {"id": "bad-flags", "market": "ZA", "source_market": "ZA", "flags": flags}

    assert specificity().local_posts([record], "ZA") == []


@pytest.mark.parametrize(
    "flags",
    [
        [" MARKET_ASSUMED "],
        ("Market_Assumed\t",),
    ],
)
def test_local_posts_normalizes_assumed_flag_without_mutating_the_record(flags):
    record = post("assumed", market="ZA", flags=flags)
    original_flags = flags.copy() if isinstance(flags, list) else flags

    assert specificity().local_posts([record], "ZA") == []
    assert record["flags"] == original_flags


def test_local_posts_accepts_missing_or_null_flags_and_source_only_assumed_records():
    rows = [
        {"id": "missing-flags", "market": "ZA"},
        {"id": "null-flags", "market": "ZA", "flags": None},
        post("feed", source="ZA", flags=["market_assumed"]),
    ]

    assert [row["id"] for row in specificity().local_posts(rows, "ZA")] == [
        "missing-flags",
        "null-flags",
        "feed",
    ]


def test_basis_uses_only_referenced_local_claims_and_returns_a_verbatim_short_quote():
    claims = claims_with_quote("We started dancing tonight")
    claims.append({"id": "c3", "evidence_ids": ["unreferenced"], "quotes": []})
    evidence = good_evidence() + [post("unreferenced", market="ZA", text="A third post in the pack.")]

    result = basis(claims=claims, evidence=evidence)

    assert result == {
        "local_evidence_ids": ["located", "feed"],
        "quote": {"evidence_id": "located", "text": "We started dancing tonight"},
        "why_now": EXPLANATION,
        "reason": None,
    }


def test_quote_uses_nonempty_full_source_text_instead_of_card_excerpt():
    evidence = good_evidence()
    evidence[0] = post(
        "located",
        market="ZA",
        text="Preview excerpt only.",
        quote_text=SOURCE_ONE,
    )

    result = basis(claims=claims_with_quote("We started dancing tonight"), evidence=evidence)

    assert result["quote"] == {"evidence_id": "located", "text": "We started dancing tonight"}
    assert result["reason"] is None


def test_empty_full_source_text_falls_back_to_card_excerpt():
    evidence = good_evidence()
    evidence[0] = post("located", market="ZA", text=SOURCE_ONE, quote_text="  ")

    result = basis(claims=claims_with_quote("We started dancing tonight"), evidence=evidence)

    assert result["quote"] == {"evidence_id": "located", "text": "We started dancing tonight"}
    assert result["reason"] is None


def test_quote_object_does_not_accept_noncanonical_quote_text_alias():
    claims = claims_with_quote(None)
    claims[0]["quotes"] = [{"evidence_id": "located", "quote_text": "We started dancing tonight"}]

    result = basis(claims=claims)

    assert result["quote"] is None
    assert result["reason"] == "quote_not_verbatim"


@pytest.mark.parametrize(
    ("source_text", "quote"),
    [
        ("We   started dancing tonight at the braai.", "We   started dancing tonight"),
        ("Caption: “We started dancing tonight” at the braai.", "“We started dancing tonight”"),
    ],
)
def test_quote_boundary_check_accepts_normalized_verbatim_whitespace_and_unicode(source_text, quote):
    evidence = good_evidence()
    evidence[0] = post("located", market="ZA", text=source_text)

    result = basis(claims=claims_with_quote(quote), evidence=evidence)

    assert result["quote"] == {"evidence_id": "located", "text": quote}
    assert result["reason"] is None


def test_quote_still_requires_a_raw_substring_when_normalized_text_matches():
    result = basis(claims=claims_with_quote("We started   dancing tonight"))

    assert result["quote"] is None
    assert result["reason"] == "quote_not_verbatim"


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("claims", "unsupported_claim_reference"),
        ("evidence", "insufficient_local_evidence"),
        ("claim_quotes", "missing_local_quote"),
    ],
)
def test_malformed_container_shapes_fail_closed(case, expected_reason):
    claims = claims_with_quote("We started dancing tonight")
    evidence = good_evidence()
    refs = ["c1", "c2"]
    if case == "claims":
        claims = 1
    elif case == "evidence":
        evidence = 1
    else:
        claims[0]["quotes"] = 1

    result = basis(claims=claims, evidence=evidence, refs=refs)

    assert result["reason"] == expected_reason


def test_assessment_passes_only_with_a_true_local_why_now_verdict():
    result = specificity().assess_specificity(
        explanation=EXPLANATION,
        claims=claims_with_quote("We started dancing tonight"),
        explanation_claim_ids=["c1", "c2"],
        evidence=good_evidence(),
        market="ZA",
        local_why_now=True,
    )

    assert result == {
        "status": "pass",
        "local_evidence_ids": ["located", "feed"],
        "quote": {"evidence_id": "located", "text": "We started dancing tonight"},
        "why_now": EXPLANATION,
        "reason": None,
    }


@pytest.mark.parametrize("local_why_now", [None, False, 1])
def test_missing_false_or_non_boolean_local_why_now_fails(local_why_now):
    kwargs = {} if local_why_now is None else {"local_why_now": local_why_now}
    result = specificity().assess_specificity(
        explanation=EXPLANATION,
        claims=claims_with_quote("We started dancing tonight"),
        explanation_claim_ids=["c1", "c2"],
        evidence=good_evidence(),
        market="ZA",
        **kwargs,
    )

    assert result["status"] == "fail"
    assert result["reason"] == "local_why_now_not_checked"


def test_empty_explanation_fails_before_claims_are_used():
    result = basis(explanation="  ")

    assert result["reason"] == "missing_explanation"
    assert result["why_now"] is None


@pytest.mark.parametrize("refs", [["c1", "missing"], ["c1", {}], []])
def test_unsupported_explanation_claim_reference_fails(refs):
    result = basis(refs=refs)

    assert result["reason"] == "unsupported_claim_reference"


def test_duplicate_or_unreferenced_posts_do_not_make_two_local_examples():
    claims = [
        {"id": "c1", "evidence_ids": ["located", "located"], "quotes": []},
        {"id": "c2", "evidence_ids": ["foreign"], "quotes": []},
        {"id": "c3", "evidence_ids": ["feed"], "quotes": []},
    ]

    result = basis(claims=claims)

    assert result["local_evidence_ids"] == ["located"]
    assert result["reason"] == "insufficient_local_evidence"


def test_repeated_claim_reference_does_not_duplicate_local_evidence():
    claims = [{"id": "c1", "evidence_ids": ["located"], "quotes": []}]

    result = basis(claims=claims, refs=["c1", "c1"])

    assert result["local_evidence_ids"] == ["located"]
    assert result["reason"] == "insufficient_local_evidence"


@pytest.mark.parametrize(
    ("quote", "quote_evidence_id", "cited_ids", "expected_reason"),
    [
        (None, "located", None, "missing_local_quote"),
        ("tonight", "located", None, "quote_too_short"),
        ("We started dancing tonight", "feed", ["located"], "quote_not_cited"),
        ("People post this in Lagos", "foreign", ["located", "foreign"], "quote_not_local"),
        ("We started dancing tomorrow", "located", None, "quote_not_verbatim"),
        ("tarted dancin", "located", None, "quote_not_word_bounded"),
    ],
)
def test_invalid_quotes_fail_for_their_specific_source_fault(quote, quote_evidence_id, cited_ids, expected_reason):
    claims = (
        claims_with_quote(quote, quote_evidence_id=quote_evidence_id, cited_ids=cited_ids)
        if quote is not None
        else [
            {"id": "c1", "evidence_ids": ["located"], "quotes": []},
            {"id": "c2", "evidence_ids": ["feed"], "quotes": []},
        ]
    )

    result = basis(claims=claims)

    assert result["reason"] == expected_reason
    assert result["quote"] is None


@pytest.mark.parametrize(
    "quote",
    [
        " ".join(f"word{index}" for index in range(26)),
        " ".join(f"longword{index:02}" for index in range(19)),
    ],
)
def test_quotes_over_word_or_character_limit_fail(quote):
    evidence = good_evidence()
    evidence[0] = post("located", market="ZA", text=quote)
    claims = claims_with_quote(quote)

    result = basis(claims=claims, evidence=evidence)

    assert result["reason"] == "quote_too_long"
    assert result["quote"] is None
