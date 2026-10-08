import json

import pytest

from core.agent import checks, native_review
from core.agent.tools.sql_query import MAX_BYTES_BILLED

PROJECT_DB = '"ogilvy-trends-v2"'
AGENT = f"{PROJECT_DB}.intelligence_42_agent"
CORE = f"{PROJECT_DB}.intelligence_42_core"


class PayloadWarehouse:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def run(self, sql, params, max_bytes_billed):
        self.calls.append((sql, params, max_bytes_billed))
        return self.rows


class DuckDBWarehouse:
    def __init__(self):
        import duckdb

        self.con = duckdb.connect()
        self.con.execute(f"ATTACH ':memory:' AS {PROJECT_DB}")
        self.con.execute(f"CREATE SCHEMA {AGENT}")
        self.con.execute(f"CREATE SCHEMA {CORE}")
        self.con.execute(f"CREATE TABLE {AGENT}.feedback (what VARCHAR)")
        self.con.execute(
            f"CREATE TABLE {CORE}.post_enrichment (post_id VARCHAR, langs VARCHAR[], entities VARCHAR[], tone VARCHAR, stance VARCHAR)"
        )
        self.calls = []

    def run(self, sql, params, max_bytes_billed):
        import sqlglot

        self.calls.append((sql, params, max_bytes_billed))
        duck_sql = sqlglot.transpile(sql, read="bigquery", write="duckdb")[0]
        duck_sql = duck_sql.replace("SAFE.JSON(fb.what)", "TRY_CAST(fb.what AS JSON)")
        bound = {key: value for key, value in (params or {}).items() if f"${key}" in duck_sql}
        result = self.con.execute(duck_sql, bound)
        columns = [column[0] for column in result.description]
        self.last_rows = [dict(zip(columns, row)) for row in result.fetchall()]
        return self.last_rows


def label(language, post_id, tone_correct):
    return {"week": "2026-W40", "id": post_id, "language": language, "gloss_correct": None,
            "tone_correct": tone_correct}


def payload(rows):
    return [{"payload": json.dumps(rows)}]


def test_a_capped_za_language_caps_a_tone_claim():
    labels = [label("zu", f"za_{i}", i < 3) for i in range(5)]
    statuses = native_review.load_native_statuses(PayloadWarehouse(payload(labels)))

    capped = native_review.tone_cap_ids(
        [{"id": "za_post", "market": "ZA", "text": "The post sounds celebratory"}],
        {"za_post": ["zu"]},
        statuses,
        checks.mostly_non_english,
        native_review_loaded=True,
        native_review_available=True,
    )

    assert statuses["zu"] == "capped"
    assert capped == ["za_post"]


@pytest.mark.parametrize(
    "language, statuses",
    [
        ("zu", {}),
        ("zu", {"zu": "insufficient"}),
        ("pcm", {"pcm": "unreviewed"}),
        ("unknown", {"unknown": "cleared"}),
    ],
)
def test_absent_insufficient_unreviewed_or_unknown_review_cannot_clear_a_tone_cap(language, statuses):
    capped = native_review.tone_cap_ids(
        [{"id": "ng_post", "market": "NG", "text": "Abeg this is wahala"}],
        {"ng_post": [language]},
        statuses,
        checks.mostly_non_english,
        native_review_loaded=True,
        native_review_available=True,
    )

    assert capped == ["ng_post"]


def test_cleared_named_language_removes_only_its_own_native_cap():
    records = [
        {"id": "za_post", "market": "ZA", "text": "Post text"},
        {"id": "ng_post", "market": "NG", "text": "Abeg this is wahala"},
    ]

    capped = native_review.tone_cap_ids(
        records,
        {"za_post": ["zu"], "ng_post": ["zu", "pcm"]},
        {"zu": "cleared", "pcm": "unreviewed"},
        checks.mostly_non_english,
        native_review_loaded=True,
        native_review_available=True,
    )

    assert capped == ["ng_post"]


@pytest.mark.parametrize("rows", [[], [{"other": "field"}], [{"payload": "{"}], [{"payload": "[]"}, {"payload": "[]"}]])
def test_malformed_or_incomplete_native_payload_fails_closed(rows):
    with pytest.raises(ValueError):
        native_review.load_native_statuses(PayloadWarehouse(rows))


def test_native_feedback_query_scores_all_501_rows_in_one_capped_read():
    warehouse = DuckDBWarehouse()
    rows = []
    for i in range(400):
        what = {"source": "42_review_v1", "kind": "native", **label("zu", f"a_good_{i:03}", True)}
        rows.append((json.dumps(what),))
    for i in range(101):
        what = {"source": "42_review_v1", "kind": "native", **label("zu", f"z_bad_{i:03}", False)}
        rows.append((json.dumps(what),))
    warehouse.con.executemany(f"INSERT INTO {AGENT}.feedback VALUES (?)", rows)

    statuses = native_review.load_native_statuses(warehouse)

    assert statuses["zu"] == "capped"
    assert len(warehouse.calls) == 1
    sql, params, cap = warehouse.calls[0]
    assert cap == MAX_BYTES_BILLED
    assert params == {}
    assert "who" not in sql.lower() and "reason" not in sql.lower()
    assert len(json.loads(warehouse.last_rows[0]["payload"])) == 501


def test_post_language_query_uses_scalar_ids_and_canonical_rows_without_fanout():
    warehouse = DuckDBWarehouse()
    warehouse.con.executemany(
        f"INSERT INTO {CORE}.post_enrichment VALUES (?, ?, ?, ?, ?)",
        [
            ("p1", ["zu", "zu"], ["entity"], "humorous", "neutral"),
            ("p1", ["xh"], ["entity"], "sarcastic", "neutral"),
            ("p2", ["pcm"], ["entity"], None, None),
            ("p3", ["en", "xh"], ["entity"], "angry", "neutral"),
        ],
    )

    languages = native_review.load_post_languages(warehouse, ["p1", "p2", "p3"])

    sql, params, cap = warehouse.calls[0]
    assert cap == MAX_BYTES_BILLED
    assert list(params.values()) == ["p1", "p2", "p3"]
    assert all(isinstance(value, str) for value in params.values())
    assert " JOIN " not in sql.upper()
    assert languages == {"p1": ["zu"], "p3": ["en", "xh"]}


def test_missing_post_language_metadata_uses_the_non_english_fallback():
    capped = native_review.tone_cap_ids(
        [{"id": "ng_missing", "market": "NG", "text": "Abeg this is wahala"}],
        {},
        {"zu": "cleared"},
        checks.mostly_non_english,
        native_review_loaded=True,
        native_review_available=True,
    )

    assert capped == ["ng_missing"]


def test_english_metadata_does_not_clear_the_ng_ke_text_baseline():
    capped = native_review.tone_cap_ids(
        [{"id": "ng_heuristic", "market": "NG", "text": "Abeg this is wahala"}],
        {"ng_heuristic": ["en"]},
        {"zu": "cleared"},
        checks.mostly_non_english,
        native_review_loaded=True,
        native_review_available=True,
    )

    assert capped == ["ng_heuristic"]


# Albert's decision of 8 October 2026 ("tone cap yes", W8 batch 3): a ZA non-English language with fewer than
# NATIVE_MIN (5) native-checked quotes is capped the same as no review at all. Five or more checked quotes with a
# passing score lift the cap. This replaces the earlier pin, test_insufficient_known_za_review_does_not_add_a_new_cap,
# which asserted the opposite.
def test_insufficient_known_za_review_is_capped_like_no_review():
    records = [{"id": "za_insufficient", "market": "ZA", "text": "English text"}]
    args = (records, {"za_insufficient": ["zu"]})
    flags = {"native_review_loaded": True, "native_review_available": True}
    insufficient = native_review.tone_cap_ids(*args, {"zu": "insufficient"}, checks.mostly_non_english, **flags)
    no_review = native_review.tone_cap_ids(*args, {}, checks.mostly_non_english, **flags)
    assert insufficient == no_review == ["za_insufficient"]


@pytest.mark.parametrize("checked, passing, expected", [
    (4, 4, ["za_zulu"]),    # under 5 checked quotes: insufficient, capped, even with every quote right
    (3, 3, ["za_zulu"]),
    (5, 5, []),             # 5 checked quotes, all pass: cleared, the cap lifts
    (5, 4, []),             # 80% of 5 passes
    (5, 3, ["za_zulu"]),    # 5 checked quotes under 80%: capped
])
def test_the_zulu_cap_follows_the_native_score_end_to_end(checked, passing, expected):
    labels = [label("zu", f"za_{i}", i < passing) for i in range(checked)]
    statuses = native_review.load_native_statuses(PayloadWarehouse(payload(labels)))
    capped = native_review.tone_cap_ids(
        [{"id": "za_zulu", "market": "ZA", "text": "Hhayi bo, le nto iyahlekisa kakhulu"}], {"za_zulu": ["zu"]}, statuses,
        checks.mostly_non_english, native_review_loaded=True, native_review_available=True)
    assert capped == expected


def test_failed_native_read_caps_non_english_tone_even_when_language_metadata_is_english():
    capped = native_review.tone_cap_ids(
        [{"id": "za_failed", "market": "ZA", "text": "Abeg this is wahala"}],
        {"za_failed": ["en"]},
        {},
        checks.mostly_non_english,
        native_review_loaded=True,
        native_review_available=False,
    )

    assert capped == ["za_failed"]


@pytest.mark.parametrize(
    "languages, statuses",
    [
        (["zu", "xh"], {"zu": "cleared", "xh": "insufficient"}),
        (["zu", "xh"], {"zu": "cleared"}),
        (["zu", "und"], {"zu": "cleared", "und": "cleared"}),
    ],
)
def test_standing_ng_cap_needs_every_non_english_language_cleared(languages, statuses):
    capped = native_review.tone_cap_ids(
        [{"id": "ng_mixed", "market": "NG", "text": "Abeg this is wahala"}],
        {"ng_mixed": languages},
        statuses,
        checks.mostly_non_english,
        native_review_loaded=True,
        native_review_available=True,
    )

    assert capped == ["ng_mixed"]


def test_standing_ng_cap_clears_when_every_non_english_language_is_eligible_and_cleared():
    capped = native_review.tone_cap_ids(
        [{"id": "ng_cleared", "market": "NG", "text": "Abeg this is wahala"}],
        {"ng_cleared": ["zu", "xh"]},
        {"zu": "cleared", "xh": "cleared"},
        checks.mostly_non_english,
        native_review_loaded=True,
        native_review_available=True,
    )

    assert capped == []


@pytest.mark.parametrize("languages", [["pcm"], ["zu"], ["en", "pcm"], ["und"]])
def test_failed_native_read_caps_known_non_english_languages_without_text_markers(languages):
    capped = native_review.tone_cap_ids(
        [{"id": "za_failed", "market": "ZA", "text": "English text"}],
        {"za_failed": languages},
        {},
        checks.mostly_non_english,
        native_review_loaded=True,
        native_review_available=False,
    )

    assert capped == ["za_failed"]


def test_failed_native_read_leaves_known_english_metadata_and_english_text_uncapped():
    text = "Everyone is talking about the price, and it is up again."
    assert not checks.mostly_non_english(text)
    capped = native_review.tone_cap_ids(
        [{"id": "za_english", "market": "ZA", "text": text}],
        {"za_english": ["en"]},
        {},
        checks.mostly_non_english,
        native_review_loaded=True,
        native_review_available=False,
    )

    assert capped == []


def test_unknown_language_cannot_clear_a_standing_ng_cap():
    capped = native_review.tone_cap_ids(
        [{"id": "ng_unknown", "market": "NG", "text": "Abeg this is wahala"}],
        {"ng_unknown": ["und"]},
        {"und": "cleared"},
        checks.mostly_non_english,
        native_review_loaded=True,
        native_review_available=True,
    )

    assert capped == ["ng_unknown"]


# N63. A ZA post has no standing cap, so a non-English language with no score at all (no labels yet, or a language
# outside the review rotation) and an "en" tag on non-English text both fell through uncapped, against TRUST C5 (tone
# on non-English posts stays at single_source until the language passes native checks). An "insufficient" score for a
# known ZA language is capped too, by the decision pinned in test_insufficient_known_za_review_is_capped_like_no_review.
ZULU = "Hhayi bo, le nto iyahlekisa kakhulu"


@pytest.mark.parametrize("languages, statuses", [
    (["zu"], {}),
    (["ve"], {"zu": "cleared"}),
    (["zu", "xh"], {"zu": "cleared"}),
    (["en"], {"zu": "cleared"}),
])
def test_a_za_post_in_a_language_with_no_cleared_review_is_capped(languages, statuses):
    capped = native_review.tone_cap_ids(
        [{"id": "za_zulu", "market": "ZA", "text": ZULU}], {"za_zulu": languages}, statuses, checks.mostly_non_english,
        native_review_loaded=True, native_review_available=True)
    assert capped == ["za_zulu"]


def test_a_za_post_in_a_cleared_language_stays_uncapped():
    capped = native_review.tone_cap_ids(
        [{"id": "za_zulu", "market": "ZA", "text": ZULU}], {"za_zulu": ["zu"]}, {"zu": "cleared"},
        checks.mostly_non_english, native_review_loaded=True, native_review_available=True)
    assert capped == []


def test_a_za_english_post_tagged_english_stays_uncapped():
    text = "Everyone is talking about the price, and it is up again."
    capped = native_review.tone_cap_ids(
        [{"id": "za_en", "market": "ZA", "text": text}], {"za_en": ["en"]}, {"zu": "cleared"},
        checks.mostly_non_english, native_review_loaded=True, native_review_available=True)
    assert capped == []
