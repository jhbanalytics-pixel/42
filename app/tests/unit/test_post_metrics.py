"""Per-post performance metrics: fetch columns, ref attachment, display line."""

from src.api import bq, research, synth


def _capture_sql(monkeypatch, rows=None):
    captured = {}

    def fake_run(sql, params=None):
        captured["sql"] = sql
        return rows or []

    monkeypatch.setattr(bq, "_run_query", fake_run)
    return captured


def test_fetch_research_posts_selects_granular_metrics(monkeypatch):
    captured = _capture_sql(monkeypatch)
    bq.fetch_research_posts(["ng"], ["economy_sapa_hustle"])
    for col in ("views", "likes", "comments", "shares"):
        assert col in captured["sql"]


def test_fetch_posts_and_comments_selects_granular_metrics(monkeypatch):
    captured = _capture_sql(monkeypatch)
    bq.fetch_posts_and_comments_for_topics(["ng"], ["economy_sapa_hustle"])
    for col in ("views", "likes", "comments", "shares"):
        assert col in captured["sql"]


def test_fetch_posts_for_topics_selects_granular_metrics(monkeypatch):
    captured = _capture_sql(monkeypatch)
    bq.fetch_posts_for_topics(["ng"], ["economy_sapa_hustle"])
    for col in ("views", "likes", "comments", "shares"):
        assert col in captured["sql"]


def test_post_metrics_keeps_only_real_values():
    row = {
        "likes": 12300.0,
        "shares": 0,
        "views": 2100000,
        "comments": None,
        "engagement_total": 2112396,
    }
    out = research._post_metrics(row)
    assert out == {"likes": 12300, "views": 2100000, "engagement_total": 2112396}


def test_post_metrics_handles_garbage():
    assert research._post_metrics({"likes": "abc", "views": ""}) == {}


def test_posts_candidates_attach_metrics():
    items = [
        {
            "id": "p1",
            "text": "sapa is real",
            "platform": "tiktok",
            "market": "ng",
            "likes": 12300,
            "shares": 840,
            "views": 2100000,
            "comments": 96,
            "engagement_total": 2113236,
        }
    ]
    refs = research._posts_candidates(items, "2026-07-01")
    m = refs[0]["metrics"]
    assert m["likes"] == 12300
    assert m["shares"] == 840
    assert m["views"] == 2100000
    assert m["comments"] == 96
    assert m["engagement_total"] == 2113236


def test_refs_from_voice_rows_attach_metrics():
    rows = [
        {
            "id": "p1",
            "text": "howzit",
            "platform": "tiktok",
            "likes": 500,
            "engagement_total": 700,
            "content_type": "post",
        }
    ]
    refs = research._refs_from_voice_rows(rows, "za", "music_amapiano")
    assert refs[0]["metrics"] == {"likes": 500, "engagement_total": 700}


def test_fmt_count_human_readable():
    assert synth._fmt_count(96) == "96"
    assert synth._fmt_count(12300) == "12.3k"
    assert synth._fmt_count(2100000) == "2.1M"
    assert synth._fmt_count(1500000000) == "1.5B"
    assert synth._fmt_count(None) == "0"


def test_post_metrics_line_orders_and_formats():
    ref = {
        "metrics": {
            "likes": 12300,
            "shares": 840,
            "views": 2100000,
            "comments": 96,
            "engagement_total": 2113236,
        }
    }
    assert synth._post_metrics_line(ref) == "12.3k likes · 840 shares · 2.1M views · 96 comments"


def test_post_metrics_line_falls_back_to_engagement():
    assert synth._post_metrics_line({"engagement_total": 900}) == "900 engagement"
    assert synth._post_metrics_line({}) == ""


def test_html_voice_ref_row_shows_metrics():
    ref = {
        "text": "sapa is real this side",
        "platform": "tiktok",
        "author_handle": "@a",
        "metrics": {"likes": 12300, "views": 2100000},
        "url": "https://tiktok.com/@a/1",
    }
    html = synth._html_voice_ref_row(1, ref, kind_label="Post")
    assert "12.3k likes" in html
    assert "2.1M views" in html
