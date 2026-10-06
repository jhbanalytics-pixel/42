"""Unit tests for the Phase 2 brief block in src/alerts/email_digest.py.

Verifies the new ``briefs_by_topic`` kwarg flows through render_html,
render_text, and send_email_digest, and that the per-topic brief block
renders the full Jo-mock shape (status badge, description, activation,
key metrics, platforms, sentiment).
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

from src.alerts.email_digest import (
    _normalize_sentiment_text,
    _render_brief_block_html,
    _render_brief_block_text,
    _sentiment_dot,
    _status_badge_colour,
    render_html,
    render_text,
)


def test_sentiment_dot_treats_zero_tone_as_neutral_not_negative():
    """When sentiment_summary mentions 'tone score of 0.00' alongside the
    word 'negative', polarity dot must short-circuit to 🟡 unknown.
    Earlier versions matched 'negative' literally and returned 🔴,
    inflating polarity on cards with no real tone signal (Thapelo
    flagged 7 May 2026: same 0.00 tone got 4 different reads)."""
    # Phrasings observed in production briefs across multiple cards.
    contradictory_phrasings = [
        "The average tone score of 0.00 indicates a predominantly very negative sentiment.",
        "The average tone score is 0.00, indicating a predominantly negative sentiment despite cultural focus.",
        "tone score of 0.0 indicates very negative sentiment",
    ]
    for s in contradictory_phrasings:
        assert _sentiment_dot(s) == "\U0001f7e1", f"expected 🟡 for: {s!r}"

    # Real negative still returns 🔴.
    assert _sentiment_dot("70% negative tone, public outrage rising") == "\U0001f534"
    # Real positive still returns 🟢.
    assert _sentiment_dot("Sentiment is largely positive across creators") == "\U0001f7e2"
    # Default neutral.
    assert _sentiment_dot("Mixed sentiment with no clear lean") == "\U0001f7e1"


def test_normalize_sentiment_text_rewrites_zero_tone_negative_contradiction():
    """The render layer rewrites Gemini outputs that read 'tone score
    of 0.00 indicates negative' so the downstream sentence says 'tone
    signal not available' instead. Idempotent for already-clean
    sentences."""
    contradictory = (
        "The average tone score of 0.00 indicates a predominantly very "
        "negative sentiment across the content."
    )
    normalized = _normalize_sentiment_text(contradictory)
    assert "Tone signal not available" in normalized
    assert "negative" not in normalized.lower()

    # Real-negative claim WITHOUT zero-tone reference passes through.
    real_negative = "70% of mentions are negative; this poses risk for brand association."
    assert _normalize_sentiment_text(real_negative) == real_negative

    # Empty / None passes through.
    assert _normalize_sentiment_text("") == ""
    assert _normalize_sentiment_text(None) is None


def _sample_brief() -> dict[str, Any]:
    return {
        "description_rationale": "Amapiano dominates SA TikTok this week, Kabza on top.",
        "activation_idea": "Lyria audio drop tied to a Kabza loop, distributed via Reels.",
        "visual_anchor": (
            "Joburg rooftop at golden hour with three dancers mid-amapiano "
            "move, mzansi-trendy streetwear, Vodacom tower in the distance."
        ),
        "nano_banana_prompt": (
            "Photo-realistic vertical Reel of a Joburg rooftop sundowner, "
            "amapiano dancers in mzansi-trendy fashion. 'Created with Gemini' "
            "subtle bottom-right tag. 9:16 portrait, golden hour."
        ),
        "lyria_prompt": "Amapiano with log drum and shaker, 113 BPM, 20 sec, upbeat mood.",
        "key_metrics": [
            "100 mentions today",
            "22 distinct sources",
            "Velocity 0.07 vs baseline",
        ],
        "platforms": ["TikTok", "Instagram Reels", "YouTube Shorts"],
        "sentiment_summary": "Positive 80%, neutral 15%, negative 5%",
        "status_tag": "Key",
        "top_creators": [
            "@kabza_de_small | TikTok | 12 mentions",
            "@dj_zinhle | Instagram | 8 mentions",
        ],
        "social_refs": [
            "https://www.tiktok.com/@x/video/1 | TikTok | Amapiano dance trend",
            "https://www.instagram.com/p/abc | Instagram Reels | Kabza loop reel",
        ],
        "platform_counts": [
            "tiktok | 78 items",
            "youtube | 24 items",
            "instagram | 12 items",
        ],
    }


def test_status_badge_colour_branches():
    """Key returns red, Rising returns blue."""
    assert _status_badge_colour("Key") == "#c0392b"
    assert _status_badge_colour("Rising") == "#2874a6"
    # Unknown tags fall through to Rising colour.
    assert _status_badge_colour("Other") == "#2874a6"


def test_render_brief_block_html_carries_every_field():
    briefs = {("za", "music_amapiano"): _sample_brief()}
    html = _render_brief_block_html(briefs)

    # Section title (post Jo-mock restyle 2026-05-05).
    assert "Creative concept" in html
    assert "activation briefs" in html
    # Country flag + label rather than bare market code.
    assert "South Africa" in html
    assert "music_amapiano" in html
    assert "Key" in html
    assert "Amapiano dominates" in html
    assert "Lyria audio drop" in html
    assert "100 mentions today" in html
    assert "TikTok" in html
    assert "Instagram Reels" in html
    assert "Positive 80%" in html
    # Brief v2 fields (Workstream E, brief alignment).
    assert "Joburg rooftop sundowner" in html  # nano_banana_prompt body
    assert "Created with Gemini" in html  # mandate language
    assert "log drum and shaker" in html  # lyria_prompt body
    assert "@kabza_de_small" in html  # top creators chip
    assert "Amapiano dance trend" in html  # social ref title
    # Per-platform counts surface in the platforms row (Thapelo's request).
    assert "tiktok (78)" in html
    assert "youtube (24)" in html
    # AI Layer 2 visual anchor renders before the prompt blocks.
    assert "Visual anchor:" in html
    assert "Vodacom tower" in html


def test_render_brief_block_html_empty_returns_empty_string():
    assert _render_brief_block_html({}) == ""


def test_render_brief_block_text_carries_every_field():
    briefs = {
        ("za", "music_amapiano"): _sample_brief(),
        ("ng", "music_afrobeats"): {
            "description_rationale": "Afrobeats spikes",
            "activation_idea": "Nano Banana cover art series",
            "key_metrics": ["123 mentions", "20 sources"],
            "platforms": ["TikTok", "Instagram"],
            "sentiment_summary": "Positive split",
            "status_tag": "Rising",
        },
    }
    lines = _render_brief_block_text(briefs)
    body = "\n".join(lines)

    assert "Daily briefs" in body
    assert "ZA" in body
    assert "NG" in body
    assert "music_amapiano [Key]" in body
    assert "music_afrobeats [Rising]" in body
    assert "Activation: Lyria audio drop tied to a Kabza loop" in body
    assert "100 mentions today" in body
    assert "Platforms: TikTok / Instagram Reels / YouTube Shorts" in body


def test_render_html_includes_brief_block_when_provided():
    briefs = {("za", "music_amapiano"): _sample_brief()}
    out = render_html(
        market_to_alerts={},
        run_date=date(2026, 5, 5),
        briefs_by_topic=briefs,
    )
    assert "Creative concept" in out
    assert "activation briefs" in out
    assert "music_amapiano" in out


def test_render_html_skips_brief_block_when_none():
    out = render_html(
        market_to_alerts={},
        run_date=date(2026, 5, 5),
        briefs_by_topic=None,
    )
    # Brief section header absent when no briefs supplied.
    assert "Creative concept" not in out


def test_render_text_includes_brief_block_when_provided():
    briefs = {("ng", "music_afrobeats"): _sample_brief()}
    out = render_text(
        market_to_alerts={},
        run_date=date(2026, 5, 5),
        briefs_by_topic=briefs,
    )
    assert "Daily briefs" in out
    assert "music_afrobeats" in out


def test_render_html_includes_daily_summary_when_provided():
    """Cross-trend summary block lands above brief cards when supplied."""
    summary = {
        "summary_text": "Music nostalgia spans KE and ZA, politics dominates KE narrative.",
        "through_line": "Music + politics carry the day across SSA.",
        "call_to_action": "Brief 5 KE creators on a Gengetone Lyria challenge by Friday.",
        "key_topics": ["ke/politics_maandamano", "za/music_amapiano"],
        "rising_topics": ["ke/music_gengetone"],
    }
    out = render_html(
        market_to_alerts={},
        run_date=date(2026, 5, 5),
        briefs_by_topic={("za", "music_amapiano"): _sample_brief()},
        daily_summary=summary,
    )
    assert "Today's through-line" in out
    assert "Music + politics carry the day" in out
    assert "Music nostalgia spans KE and ZA" in out
    assert "Action this week" in out
    assert "Brief 5 KE creators" in out


def test_render_html_skips_daily_summary_block_when_none():
    """No summary block when daily_summary kwarg omitted."""
    out = render_html(
        market_to_alerts={},
        run_date=date(2026, 5, 5),
        briefs_by_topic={("za", "music_amapiano"): _sample_brief()},
    )
    assert "Today's through-line" not in out


def test_render_html_includes_branded_header():
    """Every digest carries the Google x Ogilvy lockup at the top."""
    out = render_html(
        market_to_alerts={},
        run_date=date(2026, 5, 5),
        briefs_by_topic=None,
    )
    assert "Google" in out
    assert "Ogilvy" in out
    assert "Trend Engine" in out
    # Run date visible in header subtitle.
    assert "2026-05-05" in out


def test_render_html_summary_card_counts():
    """Summary card surfaces the top-line numbers a stakeholder skims first."""
    briefs = {
        ("za", "music_amapiano"): _sample_brief(),
        ("ng", "music_afrobeats"): {**_sample_brief(), "status_tag": "Rising"},
        ("ke", "politics_maandamano"): {**_sample_brief(), "status_tag": "Key"},
    }
    out = render_html(
        market_to_alerts={},
        run_date=date(2026, 5, 5),
        briefs_by_topic=briefs,
        stats={"briefs_skipped": 1},
    )
    # 3 briefs landed, 2 tagged Key, 1 skipped.
    assert "briefs landed" in out
    assert "tagged Key" in out
    assert "briefs skipped" in out
    # Sentinel: the briefs_landed count cell is present with value "3".
    assert ">3<" in out


def test_brief_block_html_escapes_payload():
    """User content with HTML special chars must be escaped, not rendered."""
    briefs = {
        ("za", "<script>alert(1)</script>"): {
            "description_rationale": "<b>raw html</b>",
            "activation_idea": "",
            "key_metrics": [],
            "platforms": [],
            "sentiment_summary": "",
            "status_tag": "Rising",
        }
    }
    html = _render_brief_block_html(briefs)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<b>raw html</b>" not in html
    assert "&lt;b&gt;raw html&lt;/b&gt;" in html


# ---------------------------------------------------------------------------
# PULSE v2 mailer flag (MAILER_V2_ENABLED)
# ---------------------------------------------------------------------------


def _v2_brief() -> dict[str, Any]:
    """A brief dict in the shape the pipeline hands to the v2 path: the
    v1 fields plus a ``display`` bundle, ``headline`` and ``trend_score``.
    market + topic come from the briefs_by_topic key, not the dict.
    """
    return {
        **_sample_brief(),
        "headline": "Amapiano goes mainstream",
        "trend_score": 0.55,
        "trend_synthesis": "Amapiano stopped being a scene and became the baseline.",
        "cultural_context": "Pair a lifestyle creator with a Kabza loop on a Reel series.",
        "display": {
            "state": {"badge": "Day 2, accelerating", "direction": "up"},
            "phase": "Peaking",
            "window": "ride now, ~2 weeks",
            "in_market_pct": 88,
            "channels": [("tiktok", 4), ("youtube", 2)],
            "confidence": "Confirmed, 2 channels",
            "search": "rising",
        },
    }


def test_render_html_v1_when_flag_off(monkeypatch):
    """With MAILER_V2_ENABLED unset, render_html returns the v1 body. The
    PULSE wordmark (the v2 masthead) must be absent; the v1 Google x Ogilvy
    header is what ships."""
    monkeypatch.delenv("MAILER_V2_ENABLED", raising=False)
    out = render_html(
        market_to_alerts={},
        run_date=date(2026, 5, 29),
        briefs_by_topic={("za", "music_amapiano"): _v2_brief()},
    )
    assert "PULSE" not in out
    # v1 markers still present.
    assert "Trend Engine" in out
    assert "Creative concept" in out


def test_render_html_v2_when_flag_on(monkeypatch):
    """With MAILER_V2_ENABLED=true and briefs+displays supplied, render_html
    composes the PULSE v2 body (the wordmark appears)."""
    monkeypatch.setenv("MAILER_V2_ENABLED", "true")
    out = render_html(
        market_to_alerts={},
        run_date=date(2026, 5, 29),
        briefs_by_topic={
            ("za", "music_amapiano"): _v2_brief(),
            ("ng", "music_afrobeats"): {**_v2_brief(), "headline": "Afrobeats holds"},
        },
    )
    assert "PULSE" in out
    # The v2 body binds the real headline, not the v1 section title.
    assert "Amapiano goes mainstream" in out
    assert "Creative concept" not in out  # v1 block is not rendered in v2


def test_render_html_v2_explicit_false_stays_v1(monkeypatch):
    """A non-'true' value (e.g. 'false') keeps the v1 body. Guards against
    the flag being present-but-disabled in env."""
    monkeypatch.setenv("MAILER_V2_ENABLED", "false")
    out = render_html(
        market_to_alerts={},
        run_date=date(2026, 5, 29),
        briefs_by_topic={("za", "music_amapiano"): _v2_brief()},
    )
    assert "PULSE" not in out
    assert "Trend Engine" in out


def test_briefs_displays_for_pulse_aligned_and_ordered():
    """The list builder returns briefs + displays aligned by index, ordered by
    trend_score desc across ALL markets (score-first, not market-first), with
    market/topic injected from the key and the display pulled from each brief.
    Score-first is what makes the hero the single strongest trend and the
    moves + shift line span markets rather than reading as one-market."""
    from src.alerts.email_digest import _briefs_displays_for_pulse

    briefs_by_topic = {
        ("za", "low"): {**_v2_brief(), "trend_score": 0.20, "headline": "ZA low"},
        ("za", "high"): {**_v2_brief(), "trend_score": 0.90, "headline": "ZA high"},
        ("ng", "mid"): {**_v2_brief(), "trend_score": 0.50, "headline": "NG mid"},
    }
    briefs, displays = _briefs_displays_for_pulse(briefs_by_topic)

    assert len(briefs) == len(displays) == 3
    # score desc across markets: za/high (0.90), ng/mid (0.50), za/low (0.20)
    assert briefs[0]["headline"] == "ZA high"
    assert briefs[0]["market"] == "za"
    assert briefs[0]["topic"] == "high"
    assert briefs[1]["headline"] == "NG mid"
    assert briefs[1]["market"] == "ng"
    assert briefs[2]["headline"] == "ZA low"
    # displays are positionally aligned to briefs and carry the renderer keys
    for brief, display in zip(briefs, displays, strict=True):
        assert display is briefs_by_topic[(brief["market"], brief["topic"])]["display"]
        assert "state" in display


def test_briefs_displays_for_pulse_missing_display_degrades_to_empty():
    """A brief with no display field yields an empty dict in the displays
    list (the renderers drop those elements rather than crash)."""
    from src.alerts.email_digest import _briefs_displays_for_pulse

    briefs_by_topic = {
        ("za", "music_amapiano"): {"headline": "no display", "trend_score": 0.4},
    }
    briefs, displays = _briefs_displays_for_pulse(briefs_by_topic)
    assert displays == [{}]
    assert briefs[0]["market"] == "za"


def test_guard_inline_body_size_is_market_aware_keeps_all_markets(monkeypatch):
    """Oversize low-score day: the inline trim must cap per market and never drop
    a whole market. ZA sits at the bottom of the global score order; the old flat
    top-N cut dropped it entirely (28 Jun incident). Assert all 3 markets survive.
    """
    import src.alerts.email_render as er
    from src.alerts.email_digest import INLINE_BODY_CEILING_BYTES, _guard_inline_body_size

    briefs_by_topic = {
        ("za", "t1"): {**_v2_brief(), "trend_score": 0.10, "headline": "ZA1", "market": "za"},
        ("za", "t2"): {**_v2_brief(), "trend_score": 0.12, "headline": "ZA2", "market": "za"},
        ("za", "t3"): {**_v2_brief(), "trend_score": 0.14, "headline": "ZA3", "market": "za"},
        ("ng", "t1"): {**_v2_brief(), "trend_score": 0.80, "headline": "NG1", "market": "ng"},
        ("ng", "t2"): {**_v2_brief(), "trend_score": 0.70, "headline": "NG2", "market": "ng"},
        ("ke", "t1"): {**_v2_brief(), "trend_score": 0.60, "headline": "KE1", "market": "ke"},
        ("ke", "t2"): {**_v2_brief(), "trend_score": 0.50, "headline": "KE2", "market": "ke"},
    }
    calls: list[list[dict[str, Any]]] = []

    def fake_render(
        briefs,
        displays,
        brand,
        run_date,
        daily_summary=None,
        pan_african_stories=None,
        micro_briefs_by_market=None,
    ):
        calls.append(briefs)
        # Size scales with brief count so the trim has to step down to fit.
        return "x" * (len(briefs) * 20_000)

    monkeypatch.setattr(er, "render_pulse_html", fake_render)
    monkeypatch.setattr("src.alerts.email_render.brand.load_brand", lambda name: {})

    oversize = "y" * (INLINE_BODY_CEILING_BYTES + 50_000)
    out = _guard_inline_body_size(oversize, briefs_by_topic, date(2026, 6, 28), None)

    final = calls[-1]
    markets = {b["market"] for b in final}
    assert markets == {"za", "ng", "ke"}, f"a market was dropped by the trim: {markets}"
    assert len(out.encode("utf-8")) <= INLINE_BODY_CEILING_BYTES


# GCS full-read archive (hosted "Open the full read" target).


def test_archive_html_to_gcs_inert_when_bucket_unset(monkeypatch):
    """No bucket configured -> returns "" and never touches the storage SDK."""
    import src.alerts.email_digest as ed

    monkeypatch.setattr(ed, "MAILER_ARCHIVE_BUCKET", "")
    assert ed.archive_html_to_gcs("<html>x</html>", date(2026, 5, 30)) == ""


def test_archive_html_to_gcs_uploads_and_returns_signed_url(monkeypatch):
    """With a bucket set, it uploads pulse/<date>-<token>.html and returns a V4
    SIGNED url, never the plain object URL. The storage client is mocked so the
    test does no network I/O.

    The bucket is private. An unguessable object name is not a control, because
    roles/storage.objectViewer on allUsers carries storage.objects.list, so one
    leaked link exposes every archived digest. Access has to be carried by a
    signature with an expiry, not by the secrecy of the path.
    """
    from unittest.mock import MagicMock

    import src.alerts.email_digest as ed

    monkeypatch.setattr(ed, "MAILER_ARCHIVE_BUCKET", "ogilvy-pulse-archive")
    # Module logger to a no-op: the success path logs the archived URL and
    # pytest log capture on Windows + Py3.13 segfaults on that record.
    monkeypatch.setattr(ed, "logger", MagicMock())
    blob = MagicMock()
    blob.generate_signed_url.return_value = (
        "https://storage.googleapis.com/ogilvy-pulse-archive/pulse/x.html?X-Goog-Signature=deadbeef"
    )
    bucket = MagicMock()
    bucket.blob.return_value = blob
    client = MagicMock()
    client.bucket.return_value = bucket

    signer = MagicMock()
    signer.token = "ya29.cloud-platform"
    signer.service_account_email = "runtime-sa@example.iam.gserviceaccount.com"
    monkeypatch.setattr(ed, "_archive_signer_credentials", lambda: signer)

    url = ed.archive_html_to_gcs("<html>pulse</html>", date(2026, 5, 30), _client=client)

    import re

    blob_name = bucket.blob.call_args.args[0]
    assert re.fullmatch(r"pulse/2026-05-30-[A-Za-z0-9_-]{8,}\.html", blob_name), blob_name
    client.bucket.assert_called_with("ogilvy-pulse-archive")
    args, kwargs = blob.upload_from_string.call_args
    assert args[0] == "<html>pulse</html>"
    assert "text/html" in kwargs["content_type"]

    # The returned URL is the signed one, and the plain object URL never ships.
    assert url == blob.generate_signed_url.return_value
    assert "X-Goog-Signature" in url

    # V4, read-only, and inside the seven day ceiling the signer enforces.
    signed_kwargs = blob.generate_signed_url.call_args.kwargs
    assert signed_kwargs["version"] == "v4"
    assert signed_kwargs["method"] == "GET"
    assert signed_kwargs["expiration"] == ed.ARCHIVE_URL_TTL
    assert ed.ARCHIVE_URL_TTL.total_seconds() <= 604800

    # A private object must not advertise itself as publicly cacheable, or a
    # shared intermediary cache can serve it to someone with no signature.
    assert blob.cache_control.startswith("private")


def test_archive_html_to_gcs_returns_empty_when_signing_fails(monkeypatch):
    """Signing needs iam.serviceAccounts.signBlob on the runtime SA. If that is
    missing the archive must degrade to no hosted link, never to a plain
    unsigned URL that only works while the bucket is public."""
    from unittest.mock import MagicMock

    import src.alerts.email_digest as ed

    monkeypatch.setattr(ed, "MAILER_ARCHIVE_BUCKET", "ogilvy-pulse-archive")
    monkeypatch.setattr(ed, "logger", MagicMock())
    blob = MagicMock()
    blob.generate_signed_url.side_effect = RuntimeError("no signBlob permission")
    bucket = MagicMock()
    bucket.blob.return_value = blob
    client = MagicMock()
    client.bucket.return_value = bucket

    assert ed.archive_html_to_gcs("<html>x</html>", date(2026, 5, 30), _client=client) == ""


def test_archive_url_ttl_is_within_the_v4_ceiling():
    """google-cloud-storage raises above seven days, so a longer TTL would fail
    every send at signing time rather than at review time."""
    import src.alerts.email_digest as ed
    from google.cloud.storage import _signing

    assert ed.ARCHIVE_URL_TTL.total_seconds() <= _signing.SEVEN_DAYS


# Default run_date is the UTC date, not the host's local date.today().


def test_send_email_digest_default_run_date_is_utc(monkeypatch):
    """With run_date=None, the digest date is the UTC date.

    The BQ trend_date is UTC. On a host whose local clock has rolled to the next
    day while it is still the prior UTC day (e.g. UTC+02 just after midnight),
    date.today() would stamp tomorrow's date on the subject, footer, and GCS
    blob while the data is for the UTC trend_date.
    """
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    import src.alerts.email_digest as ed

    captured: dict[str, Any] = {}

    def _capture_subject(*args, **kwargs):
        # build_subject(market_to_alerts, run_date, ...): run_date is args[1].
        captured["run_date"] = args[1]
        return "stub subject"

    # Pin "now" to a known UTC instant. The host local date is irrelevant; the
    # resolved run_date must be the UTC date of this instant.
    fixed = datetime(2026, 5, 30, 1, 15, tzinfo=UTC)

    class _FixedDatetime:
        @staticmethod
        def now(tz=None):
            return fixed.astimezone(tz) if tz else fixed

    monkeypatch.setattr(ed, "datetime", _FixedDatetime)
    monkeypatch.setattr(ed, "build_subject", _capture_subject)
    monkeypatch.setattr(ed, "logger", MagicMock())
    monkeypatch.delenv("EMAIL_ALERTS_ENABLED", raising=False)

    result = ed.send_email_digest({}, run_date=None, recipients=["a@example.com"])

    assert result == "dry_run"  # alerts disabled
    assert captured["run_date"] == date(2026, 5, 30)


def test_archive_html_to_gcs_swallows_upload_error(monkeypatch):
    """An upload failure is non-fatal: returns "" so a hosting blip never
    blocks the email send."""
    from unittest.mock import MagicMock

    import src.alerts.email_digest as ed

    monkeypatch.setattr(ed, "MAILER_ARCHIVE_BUCKET", "ogilvy-pulse-archive")
    # Patch the module logger to a no-op: the except path logs a warning, and
    # pytest's log capture on Windows + Py3.13 segfaults on that record. The
    # repo's standing workaround for the same crash (per the Phase 2 tests).
    monkeypatch.setattr(ed, "logger", MagicMock())
    client = MagicMock()
    client.bucket.side_effect = RuntimeError("boom")

    assert ed.archive_html_to_gcs("<html>x</html>", date(2026, 5, 30), _client=client) == ""


def test_dry_run_does_not_archive_to_gcs(monkeypatch):
    """A dry run (EMAIL_ALERTS_ENABLED != true) must not call archive_html_to_gcs.
    The archive overwrites pulse/<date>.html, the public object the already-sent
    email links to; a dry/test run must have zero side effects."""
    from unittest.mock import MagicMock

    import src.alerts.email_digest as ed

    monkeypatch.delenv("EMAIL_ALERTS_ENABLED", raising=False)
    monkeypatch.setenv("MAILER_V2_ENABLED", "true")
    monkeypatch.setattr(ed, "logger", MagicMock())
    monkeypatch.setattr(ed, "render_text", lambda *a, **k: "text")
    monkeypatch.setattr(ed, "render_html", lambda *a, **k: "<html>full</html>")
    called = {"n": 0}

    def _spy(*a, **k):
        called["n"] += 1
        return "https://example.com/pulse.html"

    monkeypatch.setattr(ed, "archive_html_to_gcs", _spy)

    out = ed.send_email_digest({}, date(2026, 6, 16))

    assert out == "dry_run"  # dry run does not send
    assert called["n"] == 0  # and does not touch GCS


# Archive-fail fallback: when the bucket is set but the upload fails, the rich
# PULSE must ship inline, NOT link to the stale static MAILER_FULL_URL.


def _smtp_ok():
    """A MagicMock SMTP context that records sendmail without a real socket."""
    from unittest.mock import MagicMock

    fake_server = MagicMock()
    fake_ctx = MagicMock()
    fake_ctx.__enter__.return_value = fake_server
    return fake_ctx, fake_server


def test_send_email_digest_inlines_rich_body_when_archive_upload_fails(monkeypatch):
    """Bucket set + upload returns "" (a blip) -> full_url must be "" so the rich
    body ships inline. Falling back to the static MAILER_FULL_URL would link the
    teaser to a page that does not host today's read and drop the rich body."""
    import src.alerts.email_digest as ed

    monkeypatch.setenv("EMAIL_ALERTS_ENABLED", "true")
    monkeypatch.setenv("GMAIL_USER", "a@b.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pw")
    monkeypatch.setenv("EMAIL_RECIPIENTS", "x@y.com")
    monkeypatch.setenv("MAILER_V2_ENABLED", "true")
    # A real static full-read URL is configured AND a bucket is configured.
    monkeypatch.setattr(ed, "MAILER_ARCHIVE_BUCKET", "ogilvy-pulse-archive")
    monkeypatch.setattr(ed, "MAILER_FULL_URL", "https://example.com/static-stale.html")
    monkeypatch.setattr(ed, "logger", MagicMock())
    monkeypatch.setattr(ed, "render_text", lambda *a, **k: "text")
    monkeypatch.setattr(ed, "render_html", lambda *a, **k: "<html>RICH FULL BODY</html>")
    # Upload fails: archive returns "" (its documented non-fatal failure value).
    monkeypatch.setattr(ed, "archive_html_to_gcs", lambda *a, **k: "")

    captured: dict[str, Any] = {}

    def _spy_inbox(rich_html, briefs, run_date, daily_summary, full_url):
        captured["full_url"] = full_url
        captured["rich_html"] = rich_html
        # Mirror the real function: empty url -> rich body inline.
        return rich_html if not full_url else "<html>LEAN TEASER</html>"

    monkeypatch.setattr(ed, "_pulse_inbox_body", _spy_inbox)

    fake_ctx, fake_server = _smtp_ok()
    with patch("src.alerts.email_digest.smtplib.SMTP", return_value=fake_ctx):
        status = ed.send_email_digest({}, date(2026, 6, 16))

    assert status == "sent"
    # The stale static URL must NOT be used when the bucket-backed upload failed.
    assert captured["full_url"] == ""
    # The rich body is what got sent (no lean teaser linking to a stale page).
    # The MIME body base64-encodes each part, so decode before asserting.
    import base64 as _b64

    payload = fake_server.sendmail.call_args.args[2]
    decoded = _b64.b64decode(payload.split("base64\n\n")[-1].split("\n")[0]).decode("utf-8")
    assert "RICH FULL BODY" in decoded
    assert "static-stale.html" not in payload
    assert "LEGACY V1 PLAIN" not in payload


def test_send_email_digest_uses_static_full_url_when_no_bucket(monkeypatch):
    """No bucket configured but a real MAILER_FULL_URL set -> the static URL is
    the intended full-read target, so the lean teaser links to it. This is the
    pre-hosting configuration the bucket path must not regress."""
    import src.alerts.email_digest as ed

    monkeypatch.setenv("EMAIL_ALERTS_ENABLED", "true")
    monkeypatch.setenv("GMAIL_USER", "a@b.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pw")
    monkeypatch.setenv("EMAIL_RECIPIENTS", "x@y.com")
    monkeypatch.setenv("MAILER_V2_ENABLED", "true")
    monkeypatch.setattr(ed, "MAILER_ARCHIVE_BUCKET", "")
    monkeypatch.setattr(ed, "MAILER_FULL_URL", "https://example.com/hosted-read.html")
    monkeypatch.setattr(ed, "logger", MagicMock())
    monkeypatch.setattr(ed, "render_text", lambda *a, **k: "text")
    monkeypatch.setattr(ed, "render_html", lambda *a, **k: "<html>RICH FULL BODY</html>")
    archive_calls = {"n": 0}

    def _spy_archive(*a, **k):
        archive_calls["n"] += 1
        return "https://should-not-be-used.example/x.html"

    monkeypatch.setattr(ed, "archive_html_to_gcs", _spy_archive)

    captured: dict[str, Any] = {}

    def _spy_inbox(rich_html, briefs, run_date, daily_summary, full_url):
        captured["full_url"] = full_url
        return "<html>LEAN TEASER</html>"

    monkeypatch.setattr(ed, "_pulse_inbox_body", _spy_inbox)

    fake_ctx, _ = _smtp_ok()
    with patch("src.alerts.email_digest.smtplib.SMTP", return_value=fake_ctx):
        status = ed.send_email_digest({}, date(2026, 6, 16))

    assert status == "sent"
    assert captured["full_url"] == "https://example.com/hosted-read.html"
    # With no bucket, the GCS archive must not be attempted at all.
    assert archive_calls["n"] == 0


# Inbox body selection (Path A: host the rich PULSE, send the lean inbox teaser).


def test_pulse_inbox_body_uses_summary_when_url_present():
    """With a real hosted full-read URL, the inbox body is the lean
    render_inbox_summary linking to it, NOT the rich body."""
    import src.alerts.email_digest as ed

    briefs_by_topic = {("za", "music_amapiano"): {**_v2_brief(), "trend_score": 0.9}}
    rich = "<div>RICH FULL EDITORIAL BODY</div>"
    out = ed._pulse_inbox_body(
        rich,
        briefs_by_topic,
        date(2026, 5, 31),
        None,
        "https://storage.googleapis.com/b/pulse/2026-05-31.html",
    )
    assert "RICH FULL EDITORIAL BODY" not in out
    assert "https://storage.googleapis.com/b/pulse/2026-05-31.html" in out
    assert "Open the full read" in out
    assert "Amapiano goes mainstream" in out  # headline from the brief


def test_pulse_inbox_body_keeps_rich_when_no_url():
    """No hosted URL -> the rich body ships inline, exactly as today. This is
    what keeps the cron unchanged until the GCS bucket is provisioned."""
    import src.alerts.email_digest as ed

    rich = "<div>RICH FULL EDITORIAL BODY</div>"
    out = ed._pulse_inbox_body(rich, {}, date(2026, 5, 31), None, "")
    assert out == rich


def test_archive_signs_via_iam_when_credentials_have_no_private_key(monkeypatch):
    """On Cloud Run the ADC principal is compute_engine.Credentials, which holds
    a token and no private key. generate_signed_url must therefore be handed
    service_account_email and access_token so the storage library takes the IAM
    signBlob path.

    Regression: the first version of this call passed neither, and every send on
    2026-08-20 logged "you need a private key to sign credentials". The object
    still uploaded, so the archive looked healthy while the digest shipped with
    no route to the full read.
    """
    from unittest.mock import MagicMock

    import src.alerts.email_digest as ed

    monkeypatch.setattr(ed, "MAILER_ARCHIVE_BUCKET", "ogilvy-pulse-archive")
    monkeypatch.setattr(ed, "logger", MagicMock())

    creds = MagicMock()
    creds.service_account_email = "runtime-sa@example.iam.gserviceaccount.com"
    creds.token = "ya29.fake-access-token"
    monkeypatch.setattr(ed, "_archive_signer_credentials", lambda: creds)

    blob = MagicMock()
    blob.generate_signed_url.return_value = "https://x/y?X-Goog-Signature=ab"
    bucket = MagicMock()
    bucket.blob.return_value = blob
    client = MagicMock()
    client.bucket.return_value = bucket

    url = ed.archive_html_to_gcs("<html>p</html>", date(2026, 8, 20), _client=client)

    kwargs = blob.generate_signed_url.call_args.kwargs
    assert kwargs["service_account_email"] == "runtime-sa@example.iam.gserviceaccount.com"
    assert kwargs["access_token"] == "ya29.fake-access-token"
    assert kwargs["version"] == "v4"
    assert url == "https://x/y?X-Goog-Signature=ab"


def test_archive_signer_refreshes_when_sa_email_is_the_metadata_placeholder():
    """compute_engine.Credentials reports service_account_email == "default"
    until a refresh resolves it against the metadata server. Signing with that
    placeholder would aim signBlob at a service account named "default"."""
    from unittest.mock import MagicMock

    import src.alerts.email_digest as ed

    creds = MagicMock()
    creds.token = "already-has-a-token"
    creds.service_account_email = "default"

    def _resolve(_req):
        creds.service_account_email = "real-sa@example.iam.gserviceaccount.com"

    creds.refresh.side_effect = _resolve

    from unittest.mock import patch

    with patch("google.auth.default", return_value=(creds, "ogilvy-trends-v2")):  # gitleaks:allow, public fixture project identifier
        out = ed._archive_signer_credentials()

    assert creds.refresh.called
    assert out.service_account_email == "real-sa@example.iam.gserviceaccount.com"


def test_archive_signer_asks_for_the_cloud_platform_scope():
    """The signer token has to carry cloud-platform, or signBlob rejects it.

    Regression, 2026-08-21: the signer reused ``storage.Client._credentials``.
    google-cloud-storage builds those with ``Client.SCOPE``, which is the three
    devstorage scopes and nothing else. iamcredentials.signBlob will not accept
    a devstorage token, so every send logged
    "403 ACCESS_TOKEN_SCOPE_INSUFFICIENT" and the digest shipped with no link to
    the full read. The permission was already granted; the scope was the fault.
    """
    from unittest.mock import MagicMock, patch

    import src.alerts.email_digest as ed

    creds = MagicMock()
    creds.token = "synthetic-cloud-platform-token"
    creds.service_account_email = "runtime-sa@example.iam.gserviceaccount.com"

    with patch("google.auth.default", return_value=(creds, "ogilvy-trends-v2")) as adc:  # gitleaks:allow, public fixture project identifier
        out = ed._archive_signer_credentials()

    scopes = adc.call_args.kwargs.get("scopes") or adc.call_args.args[0]
    assert list(scopes) == ["https://www.googleapis.com/auth/cloud-platform"]
    assert out is creds


def test_archive_signer_never_signs_with_the_storage_client_credentials():
    """The storage client's credentials are devstorage-scoped by construction,
    so they must not be the ones handed to signBlob even when present."""
    from unittest.mock import MagicMock, patch

    import src.alerts.email_digest as ed

    devstorage = MagicMock(name="devstorage_scoped")
    devstorage.token = "ya29.devstorage-only"
    devstorage.service_account_email = "runtime-sa@example.iam.gserviceaccount.com"
    client = MagicMock()
    client._credentials = devstorage

    signer = MagicMock(name="cloud_platform_scoped")
    signer.token = "ya29.cloud-platform"
    signer.service_account_email = "runtime-sa@example.iam.gserviceaccount.com"

    with patch("google.auth.default", return_value=(signer, "ogilvy-trends-v2")):
        out = ed._archive_signer_credentials()

    assert out is signer
    assert out is not devstorage
