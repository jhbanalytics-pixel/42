"""Unit tests for the RECONCILE trust boundary.

Pure tests, no BigQuery, no Gemini. The canonical golden cases (the SA-vs-SK
regression) are pinned permanently: they are the reason this module exists, so
they must never silently change behavior.
"""

from __future__ import annotations

from src.analysis.reconcile import reconcile_claims

ISSUE_DATE = "2026-06-26"


def _sk_event(corroborating_sources, confidence=0.8, as_of="2026-06-25"):
    """The South-Korea-resolved ledger event used by several cases."""
    return {
        "entity_key": "south korea",
        "entity_aliases": ["korea republic", "south korea"],
        "event_kind": "match",
        "state_label": "resolved",
        "state_text": "SA vs South Korea: played 25 Jun, SA won 1-0",
        "as_of": as_of,
        "corroborating_sources": list(corroborating_sources),
        "source_count": len(corroborating_sources),
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# GOLDEN A: the SA-vs-SK regression. A brief still talking about an upcoming
# match the ledger says already happened, with two factual sources, must be
# corrected to the ledger state_text verbatim.
# ---------------------------------------------------------------------------


def test_golden_a_stale_correction():
    claim = "Attention turns to the upcoming match against South Korea"
    ledger = [_sk_event(["news", "search"], confidence=0.8)]

    out = reconcile_claims([claim], ledger, ISSUE_DATE)

    assert len(out) == 1
    r = out[0]
    assert r["action"] == "stale"
    assert r["claim_after"] == "SA vs South Korea: played 25 Jun, SA won 1-0"
    assert r["claim_after"] != r["claim_before"]
    assert r["receipt_ids"]  # non-empty
    assert r["matched_entity_key"] == "south korea"


# ---------------------------------------------------------------------------
# GOLDEN B: same claim, no matching ledger event. Claim untouched, no_match.
# ---------------------------------------------------------------------------


def test_golden_b_no_match():
    claim = "Attention turns to the upcoming match against South Korea"
    ledger = [
        {
            "entity_key": "amapiano",
            "entity_aliases": ["yanos"],
            "event_kind": "trend",
            "state_label": "ongoing",
            "state_text": "Amapiano still climbing",
            "as_of": "2026-06-25",
            "corroborating_sources": ["news", "youtube"],
            "source_count": 2,
            "confidence": 0.7,
        }
    ]

    out = reconcile_claims([claim], ledger, ISSUE_DATE)

    r = out[0]
    assert r["action"] == "no_match"
    assert r["claim_after"] == r["claim_before"]
    assert r["receipt_ids"] == []
    assert r["matched_entity_key"] == ""
    assert r["confidence_tier"] == "none"


# ---------------------------------------------------------------------------
# Require-a-source hard gate: a contradicting event with only social (brand24)
# corroboration, or fewer than 2 factual families, must NOT correct the claim.
# It is labelled, the claim text is unchanged.
# ---------------------------------------------------------------------------


def test_require_a_source_brand24_only_is_labelled_not_corrected():
    claim = "Attention turns to the upcoming match against South Korea"
    ledger = [_sk_event(["brand24"], confidence=0.8)]

    out = reconcile_claims([claim], ledger, ISSUE_DATE)

    r = out[0]
    assert r["action"] == "labelled"
    assert r["claim_after"] == r["claim_before"]  # NOT corrected
    assert r["matched_entity_key"] == "south korea"


def test_require_a_source_single_factual_is_labelled():
    claim = "Attention turns to the upcoming match against South Korea"
    # One factual family + social: still below the 2-factual floor.
    ledger = [_sk_event(["news", "brand24"], confidence=0.8)]

    out = reconcile_claims([claim], ledger, ISSUE_DATE)

    r = out[0]
    assert r["action"] == "labelled"
    assert r["claim_after"] == r["claim_before"]


def test_require_a_source_low_confidence_is_labelled():
    claim = "Attention turns to the upcoming match against South Korea"
    # Two factual families but confidence under the floor.
    ledger = [_sk_event(["news", "search"], confidence=0.4)]

    out = reconcile_claims([claim], ledger, ISSUE_DATE)

    r = out[0]
    assert r["action"] == "labelled"
    assert r["claim_after"] == r["claim_before"]


def test_ledger_family_names_count_as_factual_sources():
    claim = "Attention turns to the upcoming match against South Korea"
    ledger = [_sk_event(["gdelt", "trends"], confidence=0.8)]

    out = reconcile_claims([claim], ledger, ISSUE_DATE)

    r = out[0]
    assert r["action"] == "stale"
    assert r["claim_after"] == "SA vs South Korea: played 25 Jun, SA won 1-0"


# ---------------------------------------------------------------------------
# Future-tense vs resolved detection.
# ---------------------------------------------------------------------------


def test_no_future_marker_does_not_go_stale():
    # A claim that does not imply a future event must not be flagged stale even
    # when it matches a resolved event with strong sources.
    claim = "South Korea featured in the weekend results"
    ledger = [_sk_event(["news", "search"], confidence=0.8)]

    out = reconcile_claims([claim], ledger, ISSUE_DATE)

    r = out[0]
    assert r["action"] != "stale"
    assert r["claim_after"] == r["claim_before"]


def test_resolved_event_older_than_issue_date_not_stale():
    # The resolved event is older than the claim's reference window, so it is
    # not fresher truth. No stale correction.
    claim = "Attention turns to the upcoming match against South Korea"
    ledger = [_sk_event(["news", "search"], confidence=0.8, as_of="2026-06-01")]

    out = reconcile_claims([claim], ledger, ISSUE_DATE)

    r = out[0]
    assert r["action"] != "stale"
    assert r["claim_after"] == r["claim_before"]


# ---------------------------------------------------------------------------
# Entity-overlap specificity: a stopword or bare substring must NOT match.
# ---------------------------------------------------------------------------


def test_stopword_does_not_match():
    # "south" alone is a stopword; the claim mentions "south" but never the
    # full entity "south korea", so no event should bind.
    claim = "Markets in the south of the country are upcoming to watch"
    ledger = [_sk_event(["news", "search"], confidence=0.8)]

    out = reconcile_claims([claim], ledger, ISSUE_DATE)

    r = out[0]
    assert r["action"] == "no_match"
    assert r["matched_entity_key"] == ""


def test_substring_does_not_match():
    # Entity "mali" must not match inside "formality"; matching is token-based.
    claim = "There is a formality to face ahead of the upcoming launch"
    ledger = [
        {
            "entity_key": "mali",
            "entity_aliases": [],
            "event_kind": "match",
            "state_label": "resolved",
            "state_text": "Mali match resolved",
            "as_of": "2026-06-25",
            "corroborating_sources": ["news", "search"],
            "source_count": 2,
            "confidence": 0.8,
        }
    ]

    out = reconcile_claims([claim], ledger, ISSUE_DATE)

    assert out[0]["action"] == "no_match"


# ---------------------------------------------------------------------------
# Corroborated: a claim that agrees with a fresh scheduled state.
# ---------------------------------------------------------------------------


def test_corroborated_fresh_scheduled_state():
    claim = "Bafana square off against Nigeria in the qualifier"
    ledger = [
        {
            "entity_key": "nigeria",
            "entity_aliases": ["super eagles"],
            "event_kind": "match",
            "state_label": "scheduled",
            "state_text": "SA vs Nigeria: scheduled 28 Jun",
            "as_of": "2026-06-26",
            "corroborating_sources": ["news", "search"],
            "source_count": 2,
            "confidence": 0.8,
        }
    ]

    out = reconcile_claims([claim], ledger, ISSUE_DATE)

    r = out[0]
    assert r["action"] == "corroborated"
    assert r["claim_after"] == r["claim_before"]
    assert r["receipt_ids"]


# ---------------------------------------------------------------------------
# Purity / idempotence: the same inputs give the same output, and the input
# ledger is not mutated.
# ---------------------------------------------------------------------------


def test_pure_and_idempotent():
    claim = "Attention turns to the upcoming match against South Korea"
    ledger = [_sk_event(["news", "search"], confidence=0.8)]
    ledger_copy = [dict(ledger[0])]

    first = reconcile_claims([claim], ledger, ISSUE_DATE)
    second = reconcile_claims([claim], ledger, ISSUE_DATE)

    assert first == second
    # Ledger not mutated.
    assert ledger[0] == ledger_copy[0]


def test_empty_inputs():
    assert reconcile_claims([], [], ISSUE_DATE) == []
    out = reconcile_claims(["something with no entity"], [], ISSUE_DATE)
    assert out[0]["action"] == "no_match"


def test_alias_match_binds_event():
    # The claim names an alias, not the entity_key; it must still bind.
    claim = "The upcoming tie with Korea Republic is the focus this weekend"
    ledger = [_sk_event(["news", "search"], confidence=0.8)]

    out = reconcile_claims([claim], ledger, ISSUE_DATE)

    r = out[0]
    assert r["action"] == "stale"
    assert r["matched_entity_key"] == "south korea"


# ---------------------------------------------------------------------------
# EVENT IDENTITY. A shared entity is not a shared event. Both live "stale"
# corrections produced in the two weeks to 2026-08-03 replaced a true claim
# with an unrelated headline that merely named the same person or country.
# Pinned as goldens: these exact pairs must never be corrected again.
# ---------------------------------------------------------------------------


def test_golden_c_ramaphosa_unrelated_event_is_not_corrected():
    """Live za 2026-08-03. A 1976-uprisings commemoration claim was overwritten
    with an Eskom energy headline because both name Ramaphosa."""
    claim = (
        "President Cyril Ramaphosa is scheduled to address the landmark event, "
        "drawing significant online attention to the legacy of the 1976 uprisings."
    )
    ledger = [
        {
            "entity_key": "cyril ramaphosa",
            "entity_aliases": ["Ramaphosa", "President Ramaphosa"],
            "event_kind": "entity",
            "state_label": "resolved",
            "state_text": (
                "Ramaphosa hammers a nail in the coffin for Eskom’s monopoly in South Africa"  # noqa: RUF001 (verbatim live row)
            ),
            "as_of": "2026-08-02",
            "corroborating_sources": ["rss", "youtube"],
            "source_count": 2,
            "confidence": 0.7,
        }
    ]

    out = reconcile_claims([claim], ledger, "2026-08-03")

    r = out[0]
    assert r["action"] != "stale"
    assert r["claim_after"] == r["claim_before"]
    # Still audited: the anchor and its receipts survive for the shadow trail.
    assert r["matched_entity_key"] == "cyril ramaphosa"
    assert r["receipt_ids"] == ["rss", "youtube"]


def test_golden_d_south_africa_unrelated_event_is_not_corrected():
    """Live za 2026-07-27. A cricket-fixture claim was overwritten with a beer
    company profile because both name South Africa."""
    claim = (
        "The conversation also touches on strategic utility, such as using AI for "
        "search engine optimization and analyzing cricket pitch secrets ahead of "
        "major sports fixtures like the India versus South Africa series."
    )
    ledger = [
        {
            "entity_key": "south africa",
            "entity_aliases": [],
            "event_kind": "entity",
            "state_label": "resolved",
            "state_text": "The man making R294,900 a day running the biggest beer company in South Africa",
            "as_of": "2026-07-26",
            "corroborating_sources": ["rss", "trends", "youtube"],
            "source_count": 3,
            "confidence": 0.9,
        }
    ]

    out = reconcile_claims([claim], ledger, "2026-07-27")

    r = out[0]
    assert r["action"] != "stale"
    assert r["claim_after"] == r["claim_before"]


def test_golden_e_kenya_different_fixture_is_not_corrected():
    """Live ke 2026-08-17. A story about Manchester United and Arsenal fan
    caravans riding matatus was overwritten with an unrelated video-game match
    result because both texts describe a fixture. Frame agreement is a kind of
    event, not an identity, and both texts here are rich enough to be compared
    on vocabulary: they share one token where the floor is two."""
    claim = (
        "Manchester United Kenya supporters recently staged a dramatic caravan arriving on "
        "Nduthis from Mwihoko to face their Arsenal Kenya rivals, while Arsenal fans "
        "integrated Nairobi matatu culture into their matchday logistics."
    )
    ledger = [
        {
            "entity_key": "arsenal",
            "entity_aliases": [],
            "event_kind": "entity",
            "state_label": "resolved",
            "state_text": (
                "Arsenal 3 vs 0 Manchester City | THE FINAL - FA Community Shield 2026 "
                "eFootball Simulation Game"
            ),
            "as_of": "2026-08-17",
            "corroborating_sources": ["rss", "youtube"],
            "source_count": 2,
            "confidence": 0.85,
        }
    ]

    out = reconcile_claims([claim], ledger, "2026-08-17")

    r = out[0]
    assert r["action"] != "stale"
    assert r["claim_after"] == r["claim_before"]


def test_frame_agreement_needs_a_thin_side():
    """Frame agreement alone cannot confirm identity between two texts that both
    carry real vocabulary. Same frame, no shared words, means two events of the
    same kind, not one event."""
    from src.analysis.reconcile import _event_identity_confirmed

    event = {
        "entity_key": "arsenal",
        "state_text": "Arsenal beat Everton in the Emirates Cup curtain raiser on Sunday",
    }
    assert not _event_identity_confirmed(
        "Arsenal supporters filled the Nairobi bar early to watch the derby clash unfold",
        event,
        "arsenal",
    )


def test_identity_confirmed_by_frame_agreement():
    from src.analysis.reconcile import _event_identity_confirmed

    event = _sk_event(["news", "search"])
    # Both texts describe a fixture.
    assert _event_identity_confirmed(
        "Attention turns to the upcoming match against South Korea", event, "south korea"
    )
    # A speech claim against a fixture state is a different event.
    assert not _event_identity_confirmed(
        "South Korea's president is scheduled to address parliament", event, "south korea"
    )


def test_identity_confirmed_by_shared_topic_tokens():
    from src.analysis.reconcile import _event_identity_confirmed

    event = {
        "entity_key": "argentina",
        "state_text": "Egypt exit the FIFA World Cup after the Argentina thriller",
    }
    # Four distinctive tokens beyond the anchor (egypt, fifa, world, cup).
    assert _event_identity_confirmed(
        "Argentina will meet Egypt in the FIFA World Cup round of 16", event, "argentina"
    )


def test_identity_not_confirmed_by_the_anchor_alone():
    from src.analysis.reconcile import _event_identity_confirmed

    event = {"entity_key": "burna boy", "state_text": "Burna Boy sued over a sample"}
    # The anchor repeats in both texts and nothing else does.
    assert not _event_identity_confirmed(
        "Burna Boy will headline the stadium show", event, "burna boy"
    )


def test_empty_state_text_can_never_confirm_identity():
    from src.analysis.reconcile import _event_identity_confirmed

    assert not _event_identity_confirmed("anything at all", {"entity_key": "x"}, "x")


# ---------------------------------------------------------------------------
# Stopword entity keys. Live ledger rows keyed on headline filler ('live',
# 'life', 'what', 'daily', 'these', 'free', 'digital') anchored any claim that
# used the word. They must never bind.
# ---------------------------------------------------------------------------


def test_headline_filler_entity_keys_never_anchor_a_claim():
    claims_by_key = {
        "live": "The upcoming show will stream live to fans across the country",
        "life": "Creators will share how the cost of life shapes their content",
        "what": "Brands are asking what will land with audiences this weekend",
        "daily": "The daily routine will change ahead of the season",
        "these": "These formats will scale ahead of the upcoming push",
        "free": "The free tier will open ahead of the launch",
        "digital": "The digital rollout is scheduled to start next month",
    }
    for key, claim in claims_by_key.items():
        ledger = [
            {
                "entity_key": key,
                "entity_aliases": [],
                "event_kind": "entity",
                "state_label": "resolved",
                "state_text": f"Something entirely unrelated about {key}",
                "as_of": "2026-06-25",
                "corroborating_sources": ["rss", "trends"],
                "source_count": 2,
                "confidence": 0.9,
            }
        ]

        out = reconcile_claims([claim], ledger, ISSUE_DATE)

        assert out[0]["action"] == "no_match", f"{key!r} anchored a claim"
        assert out[0]["matched_entity_key"] == "", f"{key!r} became a matched_entity_key"


def test_match_event_returns_the_matched_anchor_phrase():
    from src.analysis.reconcile import _match_event

    ledger = [_sk_event(["news", "search"])]
    event, phrase = _match_event("The upcoming tie with Korea Republic", ledger)
    assert event is not None
    assert phrase == "korea republic"
    assert _match_event("nothing anchors here", ledger) == (None, "")


def test_ledger_channel_families_count_as_factual():
    # The live event ledger emits families named gdelt / rss / trends. The
    # trust boundary must count them as factual or no correction can ever
    # fire against a real ledger (4 Jul 2026 shadow finding).
    from src.analysis.reconcile import _factual_source_count

    assert _factual_source_count(["gdelt", "rss"]) == 2
    assert _factual_source_count(["trends"]) == 1
    assert _factual_source_count(["brand24"]) == 0
