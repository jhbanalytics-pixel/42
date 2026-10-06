import re
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import yaml
from scripts.run_rss_now import compute_seed_score, compute_trend_scores
from src.alerts.email_render._util import category_label
from src.analysis.open_intelligence.general_question_request import normalize_question_request
from src.analysis.pan_african import family_label
from src.enrichment import topic_classifier
from src.utils import config_loader

ROOT = Path(__file__).resolve().parents[2]
DEMOGRAPHIC_DEFAULT = re.compile(r"\b(?:gen\s*[- ]?\s*z|genz|youth)\b", re.IGNORECASE)
ALLOWED_CONTENT_TERMS = {
    "youth day south africa",
    "national youth jazz festival",
    "ndlovu youth choir",
    "youth day",
    "#youthday",
    "international youth day",
    "iheal youth impact",
    "youth impact forum",
    "innovation and youth conference",
}


def _yaml(path):
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8")) or {}


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_active_collection_queries_and_creator_routes_are_audience_neutral():
    sources = _yaml("configs/sources.yaml")
    active_queries = []
    for market in ("za", "ng", "ke"):
        active_queries.extend(sources["youtube_queries"][market]["queries"])
        active_queries.extend(sources["socialcrawl"]["markets"][market]["terms"])
        active_queries.extend(sources["socialcrawl"]["markets"][market]["reddit_queries"])
        active_queries.extend(sources["socialcrawl"]["markets"][market]["news_queries"])

    assert sources["socialcrawl"]["enabled"] is True
    assert not [
        query
        for query in active_queries
        if DEMOGRAPHIC_DEFAULT.search(query) and query.lower() not in ALLOWED_CONTENT_TERMS
    ]


def test_curated_creator_coverage_keeps_original_allocation_without_audience_claims():
    cfg = _yaml("configs/sources.yaml")["socialcrawl"]
    assert "creators" in cfg["phases"]
    assert cfg["phase_share"]["creators"] == 0.32
    assert cfg["caps"]["creators_per_platform"] == 40
    for market in ("za", "ng", "ke"):
        assert cfg["markets"][market]["creator_tiers"] == ["tier_1", "tier_2"]
        prose = (ROOT / f"configs/creators/{market}.yaml").read_text(encoding="utf-8")
        assert not DEMOGRAPHIC_DEFAULT.search(prose)


def test_named_observances_and_events_still_classify_in_original_topics():
    expected = {
        "ke": ("genz_sheng", ["international youth day", "day of the african child"]),
        "ng": (
            "politics_tinubu",
            [
                "international youth day",
                "day of the african child",
                "iheal youth impact",
                "youth impact forum",
                "innovation and youth conference",
            ],
        ),
    }
    for market, (topic, events) in expected.items():
        for event in events:
            assert topic in topic_classifier.classify_topics(event, "", "", market), event


def test_active_keywords_topics_and_mailer_carry_no_default_demographic():
    violations = []
    for market in ("za", "ng", "ke"):
        keywords = _yaml(f"configs/keywords/{market}.yaml")
        assert keywords["genz_markers"] == []
        topics = _yaml(f"configs/topic_groups/{market}.yaml")["topic_groups"]
        for source, value in (("keywords", keywords["topic_groups"]), ("topics", topics)):
            violations.extend(
                f"{market}:{source}:{text}"
                for text in _strings(value)
                if DEMOGRAPHIC_DEFAULT.search(text) and text.lower() not in ALLOWED_CONTENT_TERMS
            )
    za_topics = _yaml("configs/topic_groups/za.yaml")["topic_groups"]
    ke_topics = _yaml("configs/topic_groups/ke.yaml")["topic_groups"]
    tagline = _yaml("configs/mailer_brands/ogilvy.yaml")["tagline"]

    assert not DEMOGRAPHIC_DEFAULT.search(tagline)
    assert violations == []
    assert "genz_lifestyle" in za_topics
    assert "genz_sheng" in ke_topics
    assert "culture_lifestyle" not in za_topics
    assert "language_sheng" not in ke_topics


def test_generic_labels_do_not_coerce_content_into_demographic_topics():
    assert topic_classifier._brand24_label_match("employment and careers", "za") is None
    assert topic_classifier._brand24_label_match("fashion and beauty", "za") is None
    assert topic_classifier._brand24_label_match("nairobi social media", "ke") is None
    assert topic_classifier._slang_topic_match(["soft life"], "za") == "genz_lifestyle"
    assert topic_classifier._slang_topic_match(["sheng"], "ke") == "genz_sheng"


def test_current_classification_keeps_historical_storage_keys_and_neutral_labels():
    za = topic_classifier.classify_topics("soft life era", "", "", "za")
    ke = topic_classifier.classify_topics("sheng lesson mtaani", "", "", "ke")
    historical_baselines = {("za", "genz_lifestyle"): 0.4, ("ke", "genz_sheng"): 0.3}

    assert za == ["genz_lifestyle"]
    assert "genz_sheng" in ke
    assert ("za", za[0]) in historical_baselines
    assert any(("ke", topic) in historical_baselines for topic in ke)
    assert category_label("genz_lifestyle") == "Culture"
    assert category_label("genz_sheng") == "Culture"
    assert not DEMOGRAPHIC_DEFAULT.search(family_label("genz"))


def test_historical_alias_and_explicit_question_remain_readable():
    request = normalize_question_request(
        {"message": "What changed in Gen Z protest language?"},
        scope={
            "client_scope_id": "synthetic_scope",
            "market_scope": ["ke"],
            "brand_config_id": None,
            "audience_lens_ids": ["explicit_gen_z_lens"],
            "theme_id": None,
        },
        request_id="00000000-0000-4000-8000-000000000001",
        admitted_at=datetime(2026, 9, 9, tzinfo=UTC),
        policy_digest="a" * 64,
    )

    assert family_label("genz") == "Lifestyle and language"
    assert request["question"] == "What changed in Gen Z protest language?"
    assert request["audience_lens_ids"] == ["explicit_gen_z_lens"]


# Paired score fixture. The same rows score twice through the real scorer and the
# real loader: once under the shipped config and once under a legacy copy that
# differs only in the two default generation proxy weights. Creators, caps and
# historical topic keys are read through the real loaders from each phase's
# tree. The observance keys come from the real classifier, re-entered per phase
# with its pattern cache cleared and its topic groups directory pointed at the
# phase's tree. The audience filter reads no config at all: it is evaluated in
# both phases to show the request contract answers the same either way.
MARKETS = ("za", "ng", "ke")
LEGACY_GENZ_COMPOSITE_WEIGHT = 0.17
LEGACY_GENZ_AUDIENCE_WEIGHT = 0.35
OBSERVANCES = {
    "ke": ["international youth day", "day of the african child"],
    "ng": [
        "international youth day",
        "day of the african child",
        "iheal youth impact",
        "youth impact forum",
        "innovation and youth conference",
    ],
    "za": ["youth day south africa", "national youth jazz festival"],
}


def assert_neutrality_and_preservation(before, after):
    assert before["general_score"] == after["general_score"]
    for field in (
        "creator_handles",
        "creator_platforms",
        "creator_caps",
        "observance_keys",
        "historical_topic_keys",
    ):
        assert before[field] == after[field]
    assert after["explicit_audience_filter"] == "requested"


def _stats(genz_avg, slang_avg, families):
    return {
        "item_count": 40.0,
        "source_diversity": 4,
        "platform_diversity": 2,
        "channel_diversity": 3,
        "engagement_item_count": 40.0,
        "regional_avg": 0.6,
        "genz_avg": genz_avg,
        "slang_avg": slang_avg,
        "watchlist_avg": 0.0,
        "search_velocity_avg": 0.2,
        "engagement_sum": 60_000.0,
        "creator_spread": 12,
        "tone_avg_mean": 0.55,
        "tone_rows": 3,
        "channel_family_weights": families,
    }


def _score_inputs():
    return {
        ("za", "genz_lifestyle"): _stats(0.9, 0.8, {"ensemble": 30.0, "news": 10.0}),
        ("ng", "economy_sapa_hustle"): _stats(0.0, 0.7, {"news": 25.0, "reddit": 15.0}),
        ("ke", "genz_sheng"): _stats(0.5, 0.9, {"youtube": 20.0, "ensemble": 20.0}),
    }


def _audience_filter(question, lens_ids):
    request = normalize_question_request(
        {"message": question},
        scope={
            "client_scope_id": "synthetic_scope",
            "market_scope": ["ke"],
            "brand_config_id": None,
            "audience_lens_ids": lens_ids,
            "theme_id": None,
        },
        request_id="00000000-0000-4000-8000-000000000002",
        admitted_at=datetime(2026, 9, 12, tzinfo=UTC),
        policy_digest="a" * 64,
    )
    # The request contract carries the requested lens ids; a non empty list is the
    # explicit audience filter, an empty list is none. No new request field.
    return "requested" if request["audience_lens_ids"] else None


def _snapshot(genz_composite_weight, genz_audience_weight):
    scoring = config_loader.load_scoring()
    # Prove the loader route is live for this phase before scoring through it.
    assert scoring["weights"]["genz_score"] == genz_composite_weight
    assert scoring["seed_score"]["audience_weights"]["genz"] == genz_audience_weight
    inputs = _score_inputs()
    rows = compute_trend_scores(
        inputs, date(2026, 9, 12), datetime(2026, 9, 12, 0, 30), velocity_scores={}
    )
    general_score = tuple(
        (
            row["market"],
            row["query_group"],
            row["trend_score"],
            row["seed_score"],
            row["seed_audience_fit"],
            compute_seed_score(
                genz=inputs[(row["market"], row["query_group"])]["genz_avg"],
                slang=row["slang_score"],
                engagement_score=row["engagement_score"],
                creator_spread=row["creator_spread"],
                visual_audio_share=row["visual_audio_share"],
                tone_score=row["tone_score"],
                tone_rows=row["tone_rows"],
                cfg=scoring["seed_score"],
            ),
        )
        for row in rows
    )
    sources = config_loader.load_sources()["socialcrawl"]
    creators = {market: config_loader.load_market_creators(market) for market in MARKETS}
    creator_handles = tuple(
        sorted(
            (market, platform, handle)
            for market, cfg in creators.items()
            for platform, tiers in cfg["watchlists"].items()
            for tier in sources["markets"][market]["creator_tiers"]
            for handle in tiers.get(tier) or []
        )
    )
    creator_platforms = tuple(
        sorted({(market, platform) for market, platform, _ in creator_handles})
    )
    creator_caps = (
        sources["caps"]["creators_per_platform"],
        sources["phase_share"]["creators"],
        tuple(sorted(sources["caps"].items())),
    )
    observance_keys = tuple(
        (market, event, tuple(topic_classifier.classify_topics(event, "", "", market)))
        for market, events in OBSERVANCES.items()
        for event in events
    )
    historical_topic_keys = tuple(
        sorted(
            (market, key)
            for market in MARKETS
            for key in config_loader.load_yaml(
                config_loader.CONFIGS_DIR / "topic_groups" / f"{market}.yaml"
            )["topic_groups"]
            if key.startswith("genz_")
        )
    )
    return {
        "general_score": general_score,
        "proxy_readback": tuple(row["genz_score"] for row in rows),
        "creator_handles": creator_handles,
        "creator_platforms": creator_platforms,
        "creator_caps": creator_caps,
        "observance_keys": observance_keys,
        "historical_topic_keys": historical_topic_keys,
        "explicit_audience_filter": _audience_filter(
            "What changed in Gen Z protest language?", ["explicit_gen_z_lens"]
        ),
        "general_audience_filter": _audience_filter(
            "What is moving in Nairobi street food this week?", []
        ),
    }


def test_paired_score_fixture_changes_only_the_default_generation_proxy(tmp_path, monkeypatch):
    shipped = config_loader.CONFIGS_DIR
    legacy = tmp_path / "configs"
    shutil.copytree(shipped, legacy)
    scoring = yaml.safe_load((legacy / "scoring.yaml").read_text(encoding="utf-8"))
    scoring["weights"]["genz_score"] = LEGACY_GENZ_COMPOSITE_WEIGHT
    scoring["seed_score"]["audience_weights"]["genz"] = LEGACY_GENZ_AUDIENCE_WEIGHT
    (legacy / "scoring.yaml").write_text(yaml.safe_dump(scoring, sort_keys=False), encoding="utf-8")

    shipped_topics = topic_classifier.TOPIC_GROUPS_DIR
    monkeypatch.setattr(config_loader, "CONFIGS_DIR", legacy)
    monkeypatch.setattr(topic_classifier, "TOPIC_GROUPS_DIR", legacy / "topic_groups")
    topic_classifier._cached_patterns.cache_clear()
    before = _snapshot(LEGACY_GENZ_COMPOSITE_WEIGHT, LEGACY_GENZ_AUDIENCE_WEIGHT)
    assert topic_classifier._cached_patterns.cache_info().misses >= 1
    monkeypatch.setattr(config_loader, "CONFIGS_DIR", shipped)
    monkeypatch.setattr(topic_classifier, "TOPIC_GROUPS_DIR", shipped_topics)
    topic_classifier._cached_patterns.cache_clear()
    after = _snapshot(0.0, 0.0)
    assert topic_classifier._cached_patterns.cache_info().misses >= 1

    assert_neutrality_and_preservation(before, after)
    assert before["explicit_audience_filter"] == "requested"
    assert before["general_audience_filter"] is None
    assert after["general_audience_filter"] is None
    # The proxy stays readable on every scored row as the input it was, not a weight.
    assert after["proxy_readback"] == before["proxy_readback"] == (0.9, 0.0, 0.5)
    assert len({score[2] for score in after["general_score"]}) == 3
    assert after["creator_handles"]
    assert after["creator_platforms"]
    assert any(keys for _market, _event, keys in after["observance_keys"])
    assert {("za", "genz_lifestyle"), ("ke", "genz_sheng")} <= set(after["historical_topic_keys"])


# B02 control: the client overlay is a lens, never a change to the default engine.
DEFAULT_QUESTIONS = (
    "What is moving in Nairobi street food this week?",
    "How could neighbourhood repair cafes change shared access to tools?",
    "Which commuter routes into Johannesburg changed during the taxi strike?",
    "Free bets on the Bokke game tonight, odds 2.1, bet now",
    "FT: South Africa 22-17 Australia",
)
REPLAY_CONTROL = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "open_intelligence"
    / "v2"
    / "journey_brand_south_africa_narratives_v1.json"
)


def _default_classification(texts):
    return tuple(
        (market, text[:80], tuple(sorted(topic_classifier.classify_topics("", text, "", market))))
        for market in MARKETS
        for text in texts
    )


def _default_request_digest():
    request = normalize_question_request(
        {"message": DEFAULT_QUESTIONS[0]},
        scope={
            "client_scope_id": "ogilvy_default",
            "market_scope": ["ke", "ng", "za"],
            "brand_config_id": None,
            "audience_lens_ids": [],
            "theme_id": None,
        },
        request_id="00000000-0000-4000-8000-000000000003",
        admitted_at=datetime(2026, 9, 12, tzinfo=UTC),
        policy_digest="a" * 64,
    )
    return request["request_digest"], request["brand_config_id"]


def _general_state(texts):
    scoring = config_loader.load_scoring()
    return {
        **_snapshot(0.0, 0.0),
        "classification": _default_classification(texts),
        "source_weights": (
            scoring["weights"],
            scoring["seed_score"],
            scoring.get("channel_family_weights"),
        ),
        "default_request": _default_request_digest(),
    }


def test_loading_the_client_overlay_changes_no_default_topic_generation_weight_or_framing():
    import inspect
    import json

    from src.analysis.open_intelligence import client_overlay
    from src.enrichment import topic_overlays

    from tests.unit.test_client_overlay import BSA_DIMENSION_FIXTURES, BSA_NOISE_FIXTURES

    replay = json.loads(REPLAY_CONTROL.read_text(encoding="utf-8"))
    replay_texts = tuple(text for text in _strings(replay) if 12 <= len(text) <= 400)[:60]
    fixture_texts = tuple(
        text
        for _d, _n, positives, negatives in BSA_DIMENSION_FIXTURES
        for text in (*positives, *negatives)
    ) + tuple(text for _c, stripped, kept in BSA_NOISE_FIXTURES for text in (*stripped, *kept))
    texts = (*DEFAULT_QUESTIONS, *fixture_texts, *replay_texts)
    assert replay_texts

    topic_classifier._cached_patterns.cache_clear()
    before = _general_state(texts)
    assert topic_overlays.overlay_for_scope({"brand_config_id": None}) is None
    compiled = client_overlay.compiled_overlay_for_brand("bsa")
    exercised = [
        (
            client_overlay.classify_dimensions(compiled, t),
            client_overlay.apply_noise_controls(compiled, t),
        )
        for t in texts
    ]
    assert any(noise["excluded"] for _d, noise in exercised)
    after = _general_state(texts)

    assert_neutrality_and_preservation(before, after)
    assert before["classification"] == after["classification"]
    assert before["source_weights"] == after["source_weights"]
    assert before["default_request"] == after["default_request"]
    assert after["default_request"][1] is None
    assert before["general_audience_filter"] is None
    assert after["general_audience_filter"] is None
    assert before["proxy_readback"] == after["proxy_readback"]
    # The default classifier never consults the overlay, so no client exclusion can reach it.
    source = inspect.getsource(topic_classifier)
    assert "client_overlay" not in source
    assert "topic_overlays" not in source
    # A text the client lens strips keeps its default classification: the exclusion is
    # the client's and the default engine never sees a drop sentinel for it.
    excluded = [t for t in texts if client_overlay.apply_noise_controls(compiled, t)["excluded"]]
    assert excluded
    earlier = {(m, t): topics for m, t, topics in before["classification"]}
    later = {(m, t): topics for m, t, topics in after["classification"]}
    for text in excluded:
        for market in MARKETS:
            assert topic_classifier.DROP_SENTINEL not in later[(market, text[:80])]
            assert later[(market, text[:80])] == earlier[(market, text[:80])]


# Paired generation fixture. The pair above changes the two weights a proxy could
# enter a score through; this pair changes the proxy's own generator, the market
# keyword files' default demographic marker lists, and nothing else. The same
# collected rows run twice through the real enrichment, the real topic aggregator,
# the real trend and seed scorer, and the real seed candidate ranker with its
# config vocabulary, eligibility gate, persisted fit and prose rationale. The proxy
# the generator produces must stay readable as observed evidence, and none of the
# general outputs may move with it.
LEGACY_GENZ_MARKERS = ["gen z", "genz", "zoomer", "zoomers", "no cap"]
PROBE_TERM = "zoomer"
PROBE_DATE = date(2026, 9, 12)


def _collected_rows():
    import pandas as pd

    rows = [
        ("soft life era, gen z weekend", "zoomer delulu brunch, no cap", "@one", "tiktok"),
        ("soft life era, zoomers at the market", "delulu is the solulu", "@two", "instagram"),
        ("amapiano weekend in soweto", "the groove was packed", "@three", "youtube"),
        ("taxi fares up again in joburg", "commuters count the cost", "@four", "web"),
    ]
    return pd.DataFrame(
        [
            {
                "title": title,
                "text": text,
                "author_handle": handle,
                "platform": platform,
                "source": "youtube" if platform == "youtube" else "socialcrawl",
                "query_group": "",
                "views": 1000.0 * (index + 1),
                "likes": 40.0,
                "comments": 4.0,
                "shares": 1.0,
                "content_type": "video",
                "market": "za",
                "published_at": "2026-09-11T10:00:00Z",
            }
            for index, (title, text, handle, platform) in enumerate(rows)
        ]
    )


def _seed_graph_rows(proxy):
    # One seed graph row per platform for the probe term, carrying the proxy the
    # generator produced for the rows the term rides.
    return [
        {
            "market": "za",
            "term": PROBE_TERM,
            "term_type": "slang",
            "platform": platform,
            "trend_date": PROBE_DATE,
            "event_date": PROBE_DATE - timedelta(days=1),
            "row_count": 3,
            "avg_genz_score": proxy,
            "slang_row_share": 0.5,
            "topic_groups": ["genz_lifestyle"],
            "near_topics": [],
            "sample_row_ids": [f"{platform}_{index}" for index in range(3)],
        }
        for platform in ("tiktok", "instagram")
    ]


def _generation_phase(tree, monkeypatch):
    from scripts.run_rss_now import _aggregate_by_topic
    from src.analysis import seed_candidates
    from src.ingestion.enrichment import enrich_dataframe

    monkeypatch.setattr(config_loader, "CONFIGS_DIR", tree)
    monkeypatch.setattr(topic_classifier, "TOPIC_GROUPS_DIR", tree / "topic_groups")
    monkeypatch.setattr(seed_candidates, "_ROOT", tree.parent)
    monkeypatch.setattr(seed_candidates, "_STOPLIST_CACHE", None)
    topic_classifier._cached_patterns.cache_clear()
    markers = config_loader.load_market_keywords("za")["genz_markers"]

    enriched = enrich_dataframe(_collected_rows(), "za")
    general_columns = [column for column in enriched.columns if column != "genz_score"]
    enriched_general = tuple(
        tuple(str(value) for value in row)
        for row in enriched[general_columns].itertuples(index=False)
    )
    stats, unclassified = _aggregate_by_topic(enriched, "za")
    scored = compute_trend_scores(
        stats, PROBE_DATE, datetime(2026, 9, 12, 0, 30), velocity_scores={}
    )
    ranking = tuple(
        (row["query_group"], row["trend_score"], row["seed_score"], row["seed_audience_fit"])
        for row in sorted(scored, key=lambda row: (-row["trend_score"], row["query_group"]))
    )
    proxy = stats[("za", "genz_lifestyle")]["genz_avg"]

    config_terms = seed_candidates.load_config_terms("za")
    aggregated = seed_candidates.aggregate_terms(_seed_graph_rows(proxy), "za")[PROBE_TERM]
    score, fit = seed_candidates.compute_c2_score(aggregated, set(), {})
    eligible = seed_candidates.is_eligible(
        aggregated, config_terms, set(), "fast", cap_remaining=5, trend_date=PROBE_DATE
    )
    candidate = seed_candidates.build_candidate_rows(
        aggregated,
        lane="fast",
        proposed_date=PROBE_DATE,
        score=score,
        seed_fit=fit,
        safety_flags=[],
    )
    return {
        "markers": markers,
        "proxy_rows": tuple(enriched["genz_score"]),
        "proxy_topic": proxy,
        "proxy_term": aggregated["avg_genz_score"],
        "general": {
            "enriched": enriched_general,
            "unclassified": unclassified,
            "ranking": ranking,
            "classification": tuple(tuple(topics) for topics in enriched["topic_groups"]),
            "config_terms": tuple(sorted(config_terms)),
            "seed": (score, tuple(sorted(fit.items())), eligible),
            "candidate": (
                candidate["candidate_id"],
                candidate["score"],
                tuple(sorted(candidate["seed_fit"].items())),
                candidate["status"],
                candidate["rationale"],
            ),
        },
    }


def test_paired_proxy_generator_moves_only_the_observed_proxy(tmp_path, monkeypatch):
    shipped = config_loader.CONFIGS_DIR
    legacy = tmp_path / "configs"
    shutil.copytree(shipped, legacy)
    for market in MARKETS:
        path = legacy / "keywords" / f"{market}.yaml"
        keywords = yaml.safe_load(path.read_text(encoding="utf-8"))
        keywords["genz_markers"] = list(LEGACY_GENZ_MARKERS)
        path.write_text(yaml.safe_dump(keywords, sort_keys=False), encoding="utf-8")

    before = _generation_phase(legacy, monkeypatch)
    after = _generation_phase(shipped, monkeypatch)
    topic_classifier._cached_patterns.cache_clear()

    # Each phase read its own generator.
    assert before["markers"] == LEGACY_GENZ_MARKERS
    assert after["markers"] == []
    # The observed proxy moved with its generator and stays readable on the rows,
    # the topic and the seed term, so observed audience evidence is preserved.
    assert any(value > 0 for value in before["proxy_rows"])
    assert set(after["proxy_rows"]) == {0.0}
    assert before["proxy_topic"] > 0.0 == after["proxy_topic"]
    assert before["proxy_term"] == before["proxy_topic"]
    assert after["proxy_term"] == 0.0
    # Nothing general moved: enrichment, classification, ranking, seed scoring,
    # the config vocabulary behind the novelty gate, eligibility and the prose.
    for field in after["general"]:
        assert before["general"][field] == after["general"][field], field
    assert after["general"]["seed"][2] is True
    assert "genz" not in after["general"]["candidate"][4]
    assert not DEMOGRAPHIC_DEFAULT.search(after["general"]["candidate"][4])
    assert {"genz_lifestyle", "music_amapiano"} <= {
        topic for topics in after["general"]["classification"] for topic in topics
    }
