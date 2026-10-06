"""Q02 identity kernel: observation identity, shared origin and independent evidence.

The first test is the package test from the product journeys plan, kept byte for
byte inside a formatter-off block. The file level noqa exists only for that block,
which builds its rows with dict calls. Every other test uses dict literals. The
grammar tests carry confusable characters as escapes rather than as source bytes,
because a doctored origin id is exactly a string a reader cannot tell from an honest
one and the file should not hide one from its own reader.
"""

# ruff: noqa: C408

import ast
import random
import unicodedata
from pathlib import Path

import pytest
from src.analysis.open_intelligence import general_question_context_identity as identity

PRODUCER = Path(__file__).resolve().parents[2] / "scripts" / "run_rss_now.py"

# fmt: off
# isort: off
# Package test, product journeys Q02, verbatim.
from src.analysis.open_intelligence.general_question_context_identity import evidence_groups

def test_syndicated_copies_do_not_invent_independence():
    rows = [dict(market="za", platform=p, source_row_id=str(i),
                 content_digest=str(i), origin_id="wire-story-7",
                 origin_authority_digest="a" * 64)
            for i, p in enumerate(("web", "facebook", "x"))]
    result = evidence_groups(rows)
    assert len(result["observation_keys"]) == 3
    assert result["independent_origin_count"] == 1
    assert result["unknown_origin_count"] == 0
# fmt: on
# isort: on


def _row(**overrides):
    row = {
        "market": "za",
        "platform": "web",
        "source_row_id": "row-1",
        "content_digest": "digest-1",
        "origin_id": None,
        "origin_authority_digest": None,
    }
    row.update(overrides)
    return row


def _origin_row(origin_id, **overrides):
    fields = {"origin_id": origin_id, "origin_authority_digest": "b" * 64}
    fields.update(overrides)
    return _row(**fields)


# Separate controls, product journeys Q02.


def test_same_row_id_on_different_platforms_remains_two_observations():
    result = evidence_groups([_row(platform="x"), _row(platform="facebook")])
    assert result["observation_keys"] == [("za", "facebook", "row-1"), ("za", "x", "row-1")]
    assert result["unknown_origin_count"] == 2
    assert result["independent_origin_count"] == 0


def test_same_post_matched_by_three_search_terms_is_one_observation():
    rows = [_row(term=term) for term in ("load shedding", "eskom", "stage 6")]
    result = evidence_groups(rows)
    assert result["observation_keys"] == [("za", "web", "row-1")]
    assert result["unknown_origin_count"] == 1


def test_raw_and_enriched_representations_are_one_observation():
    raw = _row(stage="raw")
    enriched = _origin_row("wire-story-7", stage="enriched", topic="energy")
    for rows in ([raw, enriched], [enriched, raw]):
        result = evidence_groups(rows)
        assert result["observation_keys"] == [("za", "web", "row-1")]
        assert result["origin_groups"] == {"wire-story-7": [("za", "web", "row-1")]}
        assert result["independent_origin_count"] == 1
        assert result["unknown_origin_count"] == 0


def test_same_publisher_ten_unrelated_stories_do_not_prove_ten_viewpoints():
    rows = [_origin_row("publisher-42", source_row_id=f"story-{i}") for i in range(10)]
    result = evidence_groups(rows)
    assert len(result["observation_keys"]) == 10
    assert list(result["origin_groups"]) == ["publisher-42"]
    assert len(result["origin_groups"]["publisher-42"]) == 10
    assert result["independent_origin_count"] == 1
    assert result["unknown_origin_count"] == 0


def test_genuinely_distinct_originating_sources_qualify():
    rows = [
        _origin_row("wire-story-7", source_row_id="a"),
        _origin_row("statement-19", source_row_id="b", origin_authority_digest="c" * 64),
        _origin_row("wire-story-7", source_row_id="c", platform="facebook"),
    ]
    result = evidence_groups(rows)
    assert result["origin_groups"] == {
        "statement-19": [("za", "web", "b")],
        "wire-story-7": [("za", "facebook", "c"), ("za", "web", "a")],
    }
    assert result["independent_origin_count"] == 2
    assert result["unknown_origin_count"] == 0


def test_missing_origin_proof_increments_unknown_not_independent():
    rows = [
        _row(source_row_id="a"),
        _row(source_row_id="b"),
        _origin_row("wire-1", source_row_id="c"),
    ]
    result = evidence_groups(rows)
    assert result["unknown_origin_count"] == 2
    assert result["independent_origin_count"] == 1
    assert result["origin_groups"] == {"wire-1": [("za", "web", "c")]}


def test_hostname_is_not_origin_evidence():
    rows = [
        _row(source_row_id="a", url="https://news.example/story/1"),
        _row(source_row_id="b", url="https://news.example/story/2"),
    ]
    result = evidence_groups(rows)
    assert result["origin_groups"] == {}
    assert result["independent_origin_count"] == 0
    assert result["unknown_origin_count"] == 2


def test_news_search_music_publisher_and_creator_rows_are_preserved():
    rows = [
        _row(platform="news", source_row_id="n", market="ng"),
        _row(platform="search", source_row_id="s", market="ke"),
        _row(platform="music", source_row_id="m"),
        _origin_row("publisher-42", platform="web", source_row_id="p"),
        _origin_row("creator-handle-9", platform="youtube", source_row_id="c", creator_age=54),
    ]
    result = evidence_groups(rows)
    assert result["observation_keys"] == [
        ("ke", "search", "s"),
        ("ng", "news", "n"),
        ("za", "music", "m"),
        ("za", "web", "p"),
        ("za", "youtube", "c"),
    ]
    assert result["independent_origin_count"] == 2
    assert result["unknown_origin_count"] == 3


# Interface shape.


def test_observation_key_returns_validated_tuple():
    key = identity.observation_key(_row(market="ng", platform="x", source_row_id="17", extra=1))
    assert key == ("ng", "x", "17")


def test_evidence_groups_returns_exactly_the_four_fields():
    result = evidence_groups([])
    assert result == {
        "observation_keys": [],
        "origin_groups": {},
        "independent_origin_count": 0,
        "unknown_origin_count": 0,
    }


def test_records_must_be_a_list_of_dicts():
    with pytest.raises(ValueError, match=r"^records"):
        evidence_groups({"market": "za"})
    with pytest.raises(ValueError, match=r"^record"):
        identity.observation_key(["za", "web", "1"])


# Refusals.


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("market", "ZA"),
        ("market", "uk"),
        ("market", ""),
        ("market", None),
        ("platform", ""),
        ("platform", 7),
        ("platform", None),
        ("source_row_id", ""),
        ("source_row_id", b"1"),
        ("source_row_id", "é"),
        ("content_digest", ""),
        ("content_digest", None),
        ("content_digest", "é"),
    ],
)
def test_invalid_producer_fields_refuse_naming_the_field(field, value):
    row = _row()
    row[field] = value
    with pytest.raises(ValueError, match=rf"^{field}"):
        evidence_groups([row])


def test_missing_producer_field_refuses_naming_the_field():
    row = _row()
    del row["source_row_id"]
    with pytest.raises(ValueError, match=r"^source_row_id"):
        identity.observation_key(row)


def test_same_observation_with_different_content_digests_refuses():
    rows = [_row(content_digest="digest-1"), _row(content_digest="digest-2")]
    with pytest.raises(ValueError, match=r"^content_digest"):
        evidence_groups(rows)


def test_same_observation_with_two_origins_refuses():
    rows = [_origin_row("wire-1"), _origin_row("wire-2")]
    with pytest.raises(ValueError, match=r"^origin_id"):
        evidence_groups(rows)


@pytest.mark.parametrize(
    "digest",
    [None, "", "b" * 63, "b" * 65, "B" * 64, "g" * 64, 12, b"b" * 64],
)
def test_origin_without_validated_authority_digest_refuses(digest):
    row = _row(origin_id="wire-1", origin_authority_digest=digest)
    with pytest.raises(ValueError, match=r"^origin_authority_digest"):
        evidence_groups([row])


@pytest.mark.parametrize("origin_id", ["", 7, "é"])
def test_invalid_origin_id_refuses(origin_id):
    row = _row(origin_id=origin_id, origin_authority_digest="b" * 64)
    with pytest.raises(ValueError, match=r"^origin_id"):
        evidence_groups([row])


def test_authority_digest_without_origin_refuses():
    row = _row(origin_id=None, origin_authority_digest="b" * 64)
    with pytest.raises(ValueError, match=r"^origin_authority_digest"):
        evidence_groups([row])


@pytest.mark.parametrize("origin_id", ["rss", "RSS", " rss ", "youtube", "gdelt", "brand24"])
def test_collector_name_is_never_an_origin(origin_id):
    with pytest.raises(ValueError, match=r"^origin_id.*collector"):
        evidence_groups([_origin_row(origin_id)])


def test_pinned_collector_names_match_the_producer_registry():
    tree = ast.parse(PRODUCER.read_text(encoding="utf-8"))
    registries = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "CONNECTORS" for t in node.targets)
    ]
    assert len(registries) == 1
    producer_keys = {entry.elts[0].value for entry in registries[0].elts}
    retired_collectors = {"brand24"}
    assert set(identity.COLLECTOR_NAMES) == producer_keys | retired_collectors
    assert producer_keys.isdisjoint(retired_collectors)
    assert all(name == name.casefold() for name in identity.COLLECTOR_NAMES)


# Determinism.


def test_result_does_not_depend_on_input_order():
    rows = [
        _origin_row("wire-story-7", platform="web", source_row_id="0", content_digest="0"),
        _origin_row("wire-story-7", platform="facebook", source_row_id="1", content_digest="1"),
        _origin_row("wire-story-7", platform="x", source_row_id="2", content_digest="2"),
        _origin_row("publisher-42", source_row_id="p1", content_digest="p1"),
        _origin_row("publisher-42", source_row_id="p2", content_digest="p2"),
        _row(source_row_id="p2", content_digest="p2", term="second match"),
        _row(source_row_id="u1", content_digest="u1"),
        _row(source_row_id="u1", content_digest="u1", market="ng"),
        _row(source_row_id="u2", content_digest="u2", platform="x"),
    ]
    expected = evidence_groups(rows)
    assert expected["independent_origin_count"] == 2
    assert expected["unknown_origin_count"] == 3
    assert len(expected["observation_keys"]) == 8
    rng = random.Random(7)
    for _ in range(25):
        shuffled = list(rows)
        rng.shuffle(shuffled)
        result = evidence_groups(shuffled)
        assert result == expected
        assert list(result["origin_groups"]) == list(expected["origin_groups"])
    assert evidence_groups(list(reversed(rows))) == expected


# Support slots: what a cited set of receipts can carry as independent support.


def _cited(receipt_id, **overrides):
    return _row(receipt_id=receipt_id, **overrides)


def test_syndicated_copies_fill_one_independent_support_slot():
    rows = [
        _cited("gqctx_a", platform="web", source_row_id="a", content_digest="a"),
        _cited("gqctx_b", platform="facebook", source_row_id="b", content_digest="b"),
        _cited("gqctx_c", platform="x", source_row_id="c", content_digest="c"),
    ]
    for row in rows:
        row.update(origin_id="wire-story-7", origin_authority_digest="a" * 64)
    result = identity.support_slots(rows)
    assert result["slots"] == [
        {
            "origin_id": "wire-story-7",
            "independence": "verified_origin",
            "observation_keys": [
                ("za", "facebook", "b"),
                ("za", "web", "a"),
                ("za", "x", "c"),
            ],
            "receipt_ids": ["gqctx_a", "gqctx_b", "gqctx_c"],
        }
    ]
    assert result["independent_support_slots"] == 1
    assert result["unknown_support_slots"] == 0
    assert result["units"]["collected_records"] == 3
    assert result["units"]["unique_observations"] == 3
    assert result["units"]["verified_independent_origins"] == 1


def test_one_observation_cited_twice_is_one_slot():
    rows = [
        _cited("gqctx_raw", stage="raw"),
        _cited("gqctx_enriched", stage="enriched"),
    ]
    result = identity.support_slots(rows)
    assert len(result["slots"]) == 1
    assert result["slots"][0]["receipt_ids"] == ["gqctx_enriched", "gqctx_raw"]
    assert result["slots"][0]["observation_keys"] == [("za", "web", "row-1")]
    assert result["independent_support_slots"] == 0
    assert result["unknown_support_slots"] == 1
    assert result["units"]["unique_observations"] == 1


def test_unknown_origins_never_join_a_verified_slot_or_count_as_independent():
    rows = [
        _cited("gqctx_1", source_row_id="a", content_digest="a"),
        _cited("gqctx_2", source_row_id="b", content_digest="b"),
        _cited(
            "gqctx_3",
            source_row_id="c",
            content_digest="c",
            origin_id="statement-19",
            origin_authority_digest="b" * 64,
        ),
    ]
    result = identity.support_slots(rows)
    assert [slot["independence"] for slot in result["slots"]] == [
        "verified_origin",
        "unknown",
        "unknown",
    ]
    assert result["slots"][0]["receipt_ids"] == ["gqctx_3"]
    assert result["slots"][1]["receipt_ids"] == ["gqctx_1"]
    assert result["slots"][2]["receipt_ids"] == ["gqctx_2"]
    assert result["independent_support_slots"] == 1
    assert result["unknown_support_slots"] == 2
    assert result["units"]["unknown_origins"] == 2


def test_distinct_verified_origins_each_carry_one_slot():
    rows = [
        _cited("gqctx_1", source_row_id="a", content_digest="a", origin_id="wire-7"),
        _cited("gqctx_2", source_row_id="b", content_digest="b", origin_id="statement-19"),
    ]
    for row in rows:
        row["origin_authority_digest"] = "c" * 64
    result = identity.support_slots(rows)
    assert [slot["origin_id"] for slot in result["slots"]] == ["statement-19", "wire-7"]
    assert result["independent_support_slots"] == 2
    assert result["unknown_support_slots"] == 0


def test_support_units_count_source_families_from_the_record():
    rows = [
        _cited("gqctx_1", source_row_id="a", content_digest="a", source_family="news"),
        _cited("gqctx_2", source_row_id="b", content_digest="b", source_family="news"),
        _cited("gqctx_3", source_row_id="c", content_digest="c", source_family="search"),
        _cited("gqctx_4", source_row_id="d", content_digest="d", source_family=None),
    ]
    result = identity.support_slots(rows)
    assert result["units"] == {
        "collected_records": 4,
        "unique_observations": 4,
        "source_families": 2,
        "verified_independent_origins": 0,
        "unknown_origins": 4,
    }


def test_support_slots_are_independent_of_input_order():
    rows = [
        _cited("gqctx_1", source_row_id="a", content_digest="a", origin_id="wire-7"),
        _cited("gqctx_2", source_row_id="b", content_digest="b", origin_id="wire-7"),
        _cited("gqctx_3", source_row_id="c", content_digest="c", origin_id=None),
        _cited("gqctx_4", source_row_id="d", content_digest="d", origin_id="statement-19"),
    ]
    for row in rows:
        if row["origin_id"] is not None:
            row["origin_authority_digest"] = "d" * 64
    expected = identity.support_slots(rows)
    assert expected["independent_support_slots"] == 2
    rng = random.Random(11)
    for _ in range(25):
        shuffled = list(rows)
        rng.shuffle(shuffled)
        assert identity.support_slots(shuffled) == expected


def test_support_slots_refuse_a_missing_or_duplicate_receipt_id():
    row = _row()
    with pytest.raises(ValueError, match=r"^receipt_id"):
        identity.support_slots([row])
    with pytest.raises(ValueError, match=r"^receipt_id"):
        identity.support_slots(
            [_cited("gqctx_1"), _cited("gqctx_1", source_row_id="b", content_digest="b")]
        )


def test_support_slots_refuse_invalid_records_through_the_same_rules():
    with pytest.raises(ValueError, match=r"^records"):
        identity.support_slots({"receipt_id": "gqctx_1"})
    with pytest.raises(ValueError, match=r"^market"):
        identity.support_slots([_cited("gqctx_1", market="uk")])
    with pytest.raises(ValueError, match=r"^origin_id.*collector"):
        identity.support_slots(
            [_cited("gqctx_1", origin_id="rss", origin_authority_digest="a" * 64)]
        )
    with pytest.raises(ValueError, match=r"^content_digest"):
        identity.support_slots(
            [_cited("gqctx_1"), _cited("gqctx_2", content_digest="other-digest")]
        )


# Slots read back from groups the record already carries.


def _stored(receipt_id, platform, source_row_id, **overrides):
    return _row(receipt_id=receipt_id, platform=platform, source_row_id=source_row_id, **overrides)


STORED_KEYS = [
    ["za", "web", "row-a"],
    ["za", "facebook", "row-b"],
    ["za", "x", "row-c"],
]
STORED_RECORDS = [
    _stored("gqctx_a", "web", "row-a"),
    _stored("gqctx_b", "facebook", "row-b"),
    _stored("gqctx_c", "x", "row-c"),
]


def test_stored_groups_collapse_a_shared_origin_into_one_slot():
    result = identity.support_slots_from_groups(
        STORED_RECORDS, STORED_KEYS, {"wire-story-7": STORED_KEYS}
    )
    assert result["independent_support_slots"] == 1
    assert result["unknown_support_slots"] == 0
    assert result["slots"][0]["receipt_ids"] == ["gqctx_a", "gqctx_b", "gqctx_c"]
    assert result["units"]["unique_observations"] == 3


def test_stored_groups_without_an_origin_leave_every_observation_unknown():
    result = identity.support_slots_from_groups(STORED_RECORDS, STORED_KEYS, {})
    assert result["independent_support_slots"] == 0
    assert result["unknown_support_slots"] == 3
    assert all(slot["independence"] == "unknown" for slot in result["slots"])


def test_stored_groups_refuse_a_record_outside_the_derived_observations():
    with pytest.raises(ValueError, match=r"^observation_keys"):
        identity.support_slots_from_groups(
            [*STORED_RECORDS, _stored("gqctx_d", "web", "row-d")], STORED_KEYS, {}
        )


def test_stored_groups_refuse_an_origin_over_an_underived_observation():
    with pytest.raises(ValueError, match=r"^origin_groups"):
        identity.support_slots_from_groups(
            STORED_RECORDS, STORED_KEYS, {"wire-7": [["za", "web", "row-z"]]}
        )


def test_stored_groups_refuse_one_observation_under_two_origins():
    with pytest.raises(ValueError, match=r"^origin_id.*two origins"):
        identity.support_slots_from_groups(
            STORED_RECORDS,
            STORED_KEYS,
            {"wire-7": [STORED_KEYS[0]], "statement-19": [STORED_KEYS[0]]},
        )


def test_stored_groups_refuse_a_collector_as_an_origin():
    with pytest.raises(ValueError, match=r"^origin_id.*collector"):
        identity.support_slots_from_groups(STORED_RECORDS, STORED_KEYS, {"rss": STORED_KEYS})


@pytest.mark.parametrize(
    "keys",
    [
        [["za", "web"]],
        [["za", "web", "row-a"], ["za", "web", "row-a"]],
        [["ZA", "web", "row-a"]],
        {"za": "web"},
        [["za", "web", ""]],
        7,
        None,
        "za",
    ],
)
def test_stored_groups_refuse_invalid_derived_keys(keys):
    with pytest.raises(ValueError, match=r"^(observation_keys|market|platform|source_row_id)"):
        identity.support_slots_from_groups([], keys, {})


@pytest.mark.parametrize("groups", [[], {"wire-7": []}, {"wire-7": [["za", "web"]]}, {"": None}])
def test_stored_groups_refuse_invalid_origin_groups(groups):
    with pytest.raises(ValueError, match=r"^(origin_groups|origin_id)"):
        identity.support_slots_from_groups(STORED_RECORDS, STORED_KEYS, groups)


def test_stored_groups_are_independent_of_input_order():
    groups = {"wire-story-7": STORED_KEYS[:2]}
    expected = identity.support_slots_from_groups(STORED_RECORDS, STORED_KEYS, groups)
    assert expected["independent_support_slots"] == 1
    assert expected["unknown_support_slots"] == 1
    rng = random.Random(23)
    for _ in range(25):
        records = list(STORED_RECORDS)
        keys = list(STORED_KEYS)
        rng.shuffle(records)
        rng.shuffle(keys)
        assert identity.support_slots_from_groups(records, keys, groups) == expected


# Origin id grammar: two spellings of one origin are the same string, or both refused.


DOCTORED_ORIGINS = [
    ("trailing space", "wire-story-7 "),
    ("leading space", " wire-story-7"),
    ("internal space", "wire story 7"),
    ("upper case", "Wire-Story-7"),
    ("zero width space", "wire-story\u200b-7"),
    ("zero width joiner", "wire-story\u200d-7"),
    ("soft hyphen", "wire\u00ad-story-7"),
    ("right to left override", "wire-story-7\u202e"),
    ("fullwidth digits", "wire-story-\uff17"),
    ("cyrillic homoglyph", "wire-st\u043ery-7"),
    ("control character", "wire-story-7\x00"),
    ("newline", "wire-story-7\n"),
    ("tab", "wire-story-7\t"),
    ("non breaking space", "wire-story\u00a0-7"),
    ("empty separator run", "wire-" + "-story-7"),
    ("leading separator", "-wire-story-7"),
    ("trailing separator", "wire-story-7-"),
]


@pytest.mark.parametrize(
    ("label", "origin_id"), DOCTORED_ORIGINS, ids=[n for n, _ in DOCTORED_ORIGINS]
)
def test_origin_id_outside_the_declared_grammar_is_refused_by_name(label, origin_id):
    with pytest.raises(ValueError, match=r"^origin_id"):
        evidence_groups([_origin_row(origin_id)])
    with pytest.raises(ValueError, match=r"^origin_id"):
        identity.support_slots(
            [_row(receipt_id="gqctx_1", origin_id=origin_id, origin_authority_digest="b" * 64)]
        )
    with pytest.raises(ValueError, match=r"^origin_id"):
        identity.support_slots_from_groups(STORED_RECORDS, STORED_KEYS, {origin_id: STORED_KEYS})


@pytest.mark.parametrize(
    "origin_id",
    ["wire-story-7", "publisher-42", "statement-19", "wire_original", "a", "0", "feed.example-1"],
)
def test_origin_id_inside_the_declared_grammar_is_admitted(origin_id):
    result = evidence_groups([_origin_row(origin_id)])
    assert result["independent_origin_count"] == 1
    assert list(result["origin_groups"]) == [origin_id]


@pytest.mark.parametrize("spelling", ["wire-story-7 ", "Wire-Story-7", "wire-story\u200b-7"])
def test_one_origin_can_never_occupy_two_support_slots_through_a_second_spelling(spelling):
    """Two ordinary producer rows, one origin, one alternative spelling: refused, not folded.

    The rows are the plain shape the producer emits, so nothing has to be tampered with for
    the second spelling to arrive. Under the declared grammar the two rows are either the
    same string, which fills one slot, or the doctored one is refused by name. Neither
    outcome can hand one origin a second independent slot.
    """
    honest = _origin_row("wire-story-7", platform="web", source_row_id="a", content_digest="a")
    doctored = _origin_row(spelling, platform="facebook", source_row_id="b", content_digest="b")
    with pytest.raises(ValueError, match=r"^origin_id"):
        evidence_groups([honest, doctored])
    with pytest.raises(ValueError, match=r"^origin_id"):
        evidence_groups([doctored, honest])
    same = _origin_row("wire-story-7", platform="facebook", source_row_id="b", content_digest="b")
    shared = evidence_groups([honest, same])
    assert shared["independent_origin_count"] == 1
    assert list(shared["origin_groups"]) == ["wire-story-7"]


# The collector check: an exact match against a closed set, reached only by a valid id.


@pytest.mark.parametrize(
    "doctored",
    [
        "r\u200bss",
        "\uff52\uff53\uff53",
        "rss\u00a0",
        "RSS",
        " rss ",
        "r\u00adss",
        "you\u200btube",
        "\uff47\uff44\uff45\uff4c\uff54",
    ],
)
def test_a_doctored_collector_name_is_refused_by_the_grammar_before_the_collector_check(doctored):
    """A collector cannot assert its own independence by respelling its name.

    Every doctored spelling below survives NFC, so a strip and casefold comparison lets it
    through as an origin. The grammar refuses it before the collector check sees it, which
    is why the check itself can be an exact match against the closed set.
    """
    assert unicodedata.normalize("NFC", doctored) == doctored
    assert doctored not in identity.COLLECTOR_NAMES
    with pytest.raises(ValueError, match=r"^origin_id") as caught:
        evidence_groups([_origin_row(doctored)])
    assert "grammar" in str(caught.value)
    with pytest.raises(ValueError, match=r"^origin_id"):
        identity.support_slots_from_groups(STORED_RECORDS, STORED_KEYS, {doctored: STORED_KEYS})


@pytest.mark.parametrize("name", sorted(identity.COLLECTOR_NAMES))
def test_every_pinned_collector_name_is_refused_as_an_origin_by_exact_match(name):
    with pytest.raises(ValueError, match=r"^origin_id.*collector"):
        evidence_groups([_origin_row(name)])
    with pytest.raises(ValueError, match=r"^origin_id.*collector"):
        identity.support_slots_from_groups(STORED_RECORDS, STORED_KEYS, {name: STORED_KEYS})


def test_the_collector_check_neither_strips_nor_folds():
    """The check compares the exact string; the grammar is what refuses the rest."""
    assert all(name == name.strip() == name.casefold() for name in identity.COLLECTOR_NAMES)
    assert identity.validate_origin_id("rsss") == "rsss"
    assert identity.validate_origin_id("rss-9") == "rss-9"
    with pytest.raises(ValueError, match=r"^origin_id.*collector"):
        identity.validate_origin_id("rss")


# Slot projection: what the counts count, and the order the slots come back in.


def test_collected_records_count_records_and_never_observations():
    rows = [_cited("gqctx_raw", stage="raw"), _cited("gqctx_enriched", stage="enriched")]
    units = identity.support_slots(rows)["units"]
    assert units["collected_records"] == 2
    assert units["unique_observations"] == 1
    assert units["unknown_origins"] == 1


def test_unknown_slots_come_back_in_observation_key_order_not_arrival_order():
    """The kernel claims input order never changes the result, so the order is pinned."""
    rows = [
        _cited("gqctx_z", platform="x", source_row_id="z", content_digest="z"),
        _cited("gqctx_a", platform="web", source_row_id="a", content_digest="a"),
        _cited("gqctx_m", platform="facebook", source_row_id="m", content_digest="m"),
    ]
    result = identity.support_slots(rows)
    assert [slot["observation_keys"][0] for slot in result["slots"]] == [
        ("za", "facebook", "m"),
        ("za", "web", "a"),
        ("za", "x", "z"),
    ]
    assert [slot["receipt_ids"] for slot in result["slots"]] == [
        ["gqctx_m"],
        ["gqctx_a"],
        ["gqctx_z"],
    ]


def test_verified_slots_come_back_in_origin_order_not_arrival_order():
    rows = [
        _cited("gqctx_w", source_row_id="w", content_digest="w", origin_id="wire-7"),
        _cited("gqctx_s", source_row_id="s", content_digest="s", origin_id="statement-19"),
        _cited("gqctx_p", source_row_id="p", content_digest="p", origin_id="publisher-42"),
    ]
    for row in rows:
        row["origin_authority_digest"] = "c" * 64
    result = identity.support_slots(rows)
    assert [slot["origin_id"] for slot in result["slots"]] == [
        "publisher-42",
        "statement-19",
        "wire-7",
    ]


def test_a_verified_slot_lists_only_the_observations_that_fill_it():
    rows = [
        _cited(
            "gqctx_1",
            source_row_id="a",
            content_digest="a",
            origin_id="wire-7",
            origin_authority_digest="c" * 64,
        ),
        _cited("gqctx_2", source_row_id="b", content_digest="b"),
        _cited("gqctx_3", source_row_id="c", content_digest="c"),
    ]
    result = identity.support_slots(rows)
    assert result["slots"][0]["independence"] == "verified_origin"
    assert result["slots"][0]["observation_keys"] == [("za", "web", "a")]
    assert result["slots"][1]["observation_keys"] == [("za", "web", "b")]
    assert result["slots"][2]["observation_keys"] == [("za", "web", "c")]


def test_stored_groups_keep_a_verified_slot_to_its_own_observations():
    result = identity.support_slots_from_groups(
        STORED_RECORDS, STORED_KEYS, {"wire-story-7": [STORED_KEYS[0]]}
    )
    assert result["slots"][0]["observation_keys"] == [("za", "web", "row-a")]
    assert result["slots"][0]["receipt_ids"] == ["gqctx_a"]
    assert [slot["observation_keys"][0] for slot in result["slots"][1:]] == [
        ("za", "facebook", "row-b"),
        ("za", "x", "row-c"),
    ]


def test_origin_authority_projection_separates_an_absent_field_from_a_measured_zero():
    absent = identity.origin_authority_projection([{"market": "za"}, {"market": "ng"}])
    assert absent == {"state": "not_projected", "projected_records": 0, "unprojected_records": 2}
    measured = identity.origin_authority_projection(
        [{"origin_id": None, "origin_authority_digest": None}, {"market": "za"}]
    )
    assert measured == {"state": "projected", "projected_records": 1, "unprojected_records": 1}
    with pytest.raises(ValueError, match=r"^records"):
        identity.origin_authority_projection({"origin_id": None})


def test_validate_record_refuses_one_row_on_its_own_terms():
    assert identity.validate_record(_row()) == ("za", "web", "row-1")
    with pytest.raises(ValueError, match=r"^source_row_id"):
        identity.validate_record(_row(source_row_id="row\u0301-1"))
    with pytest.raises(ValueError, match=r"^content_digest"):
        identity.validate_record(_row(content_digest=""))
    with pytest.raises(ValueError, match=r"^origin_id"):
        identity.validate_record(_row(origin_id="wire-7 ", origin_authority_digest="a" * 64))


# Length, separator respelling and digest shape: the classes the grammar did not bound.


def test_an_origin_id_longer_than_the_declared_bound_is_refused_by_name():
    """An origin id is published verbatim as a slot key and inside an answer limitation.

    Without a bound one row can carry a ten thousand character id into every limitation the
    answer prints, so the grammar states a length and refuses anything past it.
    """
    assert identity.ORIGIN_ID_MAXIMUM == 128
    assert str(identity.ORIGIN_ID_MAXIMUM) in identity.ORIGIN_ID_GRAMMAR
    with pytest.raises(ValueError, match=r"^origin_id"):
        identity.validate_origin_id("a" * 5000)
    longest = "a" * identity.ORIGIN_ID_MAXIMUM
    assert identity.validate_origin_id(longest) == longest
    over = "a" * (identity.ORIGIN_ID_MAXIMUM + 1)
    with pytest.raises(ValueError, match=r"^origin_id") as caught:
        identity.validate_origin_id(over)
    assert str(identity.ORIGIN_ID_MAXIMUM) in str(caught.value)
    with pytest.raises(ValueError, match=r"^origin_id"):
        evidence_groups([_origin_row(over)])
    with pytest.raises(ValueError, match=r"^origin_id"):
        identity.support_slots_from_groups(STORED_RECORDS, STORED_KEYS, {over: STORED_KEYS})
    with pytest.raises(ValueError, match=r"^origin_id"):
        identity.validate_origin_authorities({over: "a" * 64})


SEPARATOR_RESPELT_COLLECTORS = sorted(
    {
        name.replace("_", separator)
        for name in identity.COLLECTOR_NAMES
        for separator in (".", "-")
        if "_" in name
    }
)


@pytest.mark.parametrize("respelt", SEPARATOR_RESPELT_COLLECTORS)
def test_a_collector_respelt_with_another_separator_is_still_not_an_origin(respelt):
    """The grammar accepts three separators, so a collector can respell its own name.

    ``apple-music`` is inside the grammar and is not the exact string ``apple_music``, so an
    exact membership test admits it as a verified independent origin. The separators carry
    no meaning of their own, so the collector check compares on a separator normalised key
    and every respelling of a pinned collector is refused as a collector.
    """
    assert respelt not in identity.COLLECTOR_NAMES
    assert identity._ORIGIN_ID.fullmatch(respelt) is not None
    with pytest.raises(ValueError, match=r"^origin_id.*collector"):
        identity.validate_origin_id(respelt)
    with pytest.raises(ValueError, match=r"^origin_id.*collector"):
        evidence_groups([_origin_row(respelt)])
    with pytest.raises(ValueError, match=r"^origin_id.*collector"):
        identity.support_slots_from_groups(STORED_RECORDS, STORED_KEYS, {respelt: STORED_KEYS})


def test_a_separator_respelling_of_a_collector_never_becomes_an_independent_origin():
    """Twelve respellings of six collectors would otherwise be six extra origin slots."""
    assert len(SEPARATOR_RESPELT_COLLECTORS) == 12
    for respelt in SEPARATOR_RESPELT_COLLECTORS:
        with pytest.raises(ValueError, match=r"^origin_id.*collector"):
            identity.support_slots(
                [_row(receipt_id="gqctx_1", origin_id=respelt, origin_authority_digest="b" * 64)]
            )


@pytest.mark.parametrize(
    "digest",
    ["a" * 64 + "not-hex-at-all", "a" * 65, "a" * 64 + "\n", "a" * 63, "A" * 64, ""],
)
def test_an_origin_authority_digest_is_matched_over_its_whole_length(digest):
    """Sixty four hex characters followed by anything is not sixty four hex characters."""
    with pytest.raises(ValueError, match=r"^origin_authorities"):
        identity.validate_origin_authorities({"wire-7": digest})
    with pytest.raises(ValueError, match=r"^origin_authority_digest"):
        evidence_groups([_origin_row("wire-7", origin_authority_digest=digest)])


def test_a_derived_key_of_four_elements_is_refused_by_name():
    """A four element key is refused where it is read, not inside a strict zip."""
    with pytest.raises(ValueError, match=r"^observation_keys") as caught:
        identity.support_slots_from_groups([], [["za", "web", "row-a", "extra"]], {})
    assert "market, platform and source row id" in str(caught.value)


def test_a_scalar_origin_group_member_is_refused_as_a_value_error():
    """A scalar member must refuse by name, not raise a TypeError out of the reader.

    ``read_context_evidence_support`` catches ValueError only, so a TypeError here escapes
    the answer projection instead of refusing the identity record by name.
    """
    for member in (7, None, "za", ["za", "web", "row-a", "extra"], ["za", "web"]):
        with pytest.raises(ValueError, match=r"^origin_groups") as caught:
            identity.support_slots_from_groups(STORED_RECORDS, STORED_KEYS, {"wire-7": [member]})
        assert "market, platform and source row id" in str(caught.value)


def test_stored_groups_refuse_records_that_are_not_a_list():
    """The records argument is held to its own type before any record is read."""
    with pytest.raises(ValueError, match=r"^records"):
        identity.support_slots_from_groups({"gqctx_1": "row"}, STORED_KEYS, {})
    with pytest.raises(ValueError, match=r"^records"):
        identity.support_slots_from_groups(7, STORED_KEYS, {})


def test_the_projection_counts_a_record_carrying_either_origin_field():
    """A row carries the origin id and its authority digest together or not at all.

    Either field alone still means the capture projected origin authority for that row, so
    counting only one of the two would read a half projected capture as an absent column.
    """
    only_id = identity.origin_authority_projection([{"origin_id": "wire-7"}])
    assert only_id == {"state": "projected", "projected_records": 1, "unprojected_records": 0}
    only_digest = identity.origin_authority_projection([{"origin_authority_digest": "a" * 64}])
    assert only_digest == {"state": "projected", "projected_records": 1, "unprojected_records": 0}
    neither = identity.origin_authority_projection([{"market": "za"}])
    assert neither == {"state": "not_projected", "projected_records": 0, "unprojected_records": 1}


def test_origin_groups_come_back_in_origin_order_not_first_observation_order():
    """The groups are published and read as a mapping, so their order is part of the result."""
    rows = [
        _origin_row("wire-story-7", source_row_id="a"),
        _origin_row("statement-19", source_row_id="b", origin_authority_digest="c" * 64),
        _origin_row("alpha-1", source_row_id="c", origin_authority_digest="d" * 64),
    ]
    result = evidence_groups(rows)
    assert list(result["origin_groups"]) == ["alpha-1", "statement-19", "wire-story-7"]
    assert list(result["origin_groups"]) == sorted(result["origin_groups"])
