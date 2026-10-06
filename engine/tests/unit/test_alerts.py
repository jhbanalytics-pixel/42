"""Unit tests for src/alerts/email_digest.py."""

from datetime import date
from unittest.mock import MagicMock, patch

from src.alerts.email_digest import (
    IssueFlag,
    TrendAlert,
    TrendMover,
    build_alert,
    build_subject,
    render_html,
    render_text,
    score_to_tier,
    send_email_digest,
    tier_upgraded,
)

THRESHOLDS = {"trending": 0.45, "emerging": 0.30, "monitoring": 0.18}
WEIGHTS = {
    "velocity": 0.17,
    "diversity": 0.13,
    "engagement": 0.15,
    "creator_spread": 0.10,
    "regional_score": 0.10,
    "genz_score": 0.10,
    "watchlist_score": 0.08,
    "search_velocity_score": 0.07,
    "slang_score": 0.05,
    "tone_score": 0.05,
}


def _alert(
    market: str,
    qg: str,
    score: float,
    prior: str | None = "Monitoring",
    tone: float | None = None,
) -> TrendAlert:
    return TrendAlert(
        market=market,
        query_group=qg,
        trend_score=score,
        tier="Trending" if score >= 0.45 else "Emerging",
        prior_tier=prior,
        item_count=30,
        source_diversity=4,
        signals={
            "velocity": (0.9, 0.17, 0.153),
            "diversity": (0.7, 0.13, 0.091),
        },
        samples=[
            {
                "title": f"{qg} headline",
                "url": f"https://example.com/{qg}",
                "text": "",
                "source": "Example Outlet",
            }
        ],
        top_persons=["Jane Doe"],
        top_orgs=["ACME"],
        tone_avg=tone,
    )


def test_score_to_tier_bands():
    # Anchored on the 28 May 2026 recalibration: Trending 0.45,
    # Emerging 0.30, Monitoring 0.18. One probe per band exercising
    # the >= boundary plus the below-threshold sub-floor.
    assert score_to_tier(0.50, THRESHOLDS) == "Trending"
    assert score_to_tier(0.45, THRESHOLDS) == "Trending"
    assert score_to_tier(0.35, THRESHOLDS) == "Emerging"
    assert score_to_tier(0.30, THRESHOLDS) == "Emerging"
    assert score_to_tier(0.22, THRESHOLDS) == "Monitoring"
    assert score_to_tier(0.18, THRESHOLDS) == "Monitoring"
    assert score_to_tier(0.10, THRESHOLDS) == "Below threshold"


def test_score_to_tier_default_thresholds_match_recalibrated_yaml():
    """Defaults baked into score_to_tier match scoring.yaml post-28 May 2026.

    Guards against drift between the in-code fallback dict and the
    configs/scoring.yaml floors. If yaml moves, this test fails and
    flags the fallback for a paired update.
    """
    # An empty dict forces the .get(..., default) branches.
    assert score_to_tier(0.50, {}) == "Trending"
    assert score_to_tier(0.45, {}) == "Trending"
    assert score_to_tier(0.30, {}) == "Emerging"
    assert score_to_tier(0.18, {}) == "Monitoring"
    assert score_to_tier(0.17, {}) == "Below threshold"


def test_tier_upgraded_from_nothing_only_fires_at_emerging():
    assert tier_upgraded("Emerging", None) is True
    assert tier_upgraded("Trending", None) is True
    assert tier_upgraded("Monitoring", None) is False
    assert tier_upgraded("Below threshold", None) is False


def test_tier_upgraded_strict_greater():
    assert tier_upgraded("Trending", "Emerging") is True
    assert tier_upgraded("Emerging", "Monitoring") is True
    assert tier_upgraded("Emerging", "Emerging") is False
    assert tier_upgraded("Monitoring", "Emerging") is False


def test_build_alert_populates_signals_and_tone():
    today = {
        "market": "za",
        "query_group": "sa_identity",
        "trend_score": 0.65,
        "velocity_score": 1.0,
        "engagement_score": 0.8,
        "diversity_score": 0.4,
        "tone_score": 0.5,
        "tone_rows": 30,
        "item_count": 120,
        "source_diversity": 5,
    }
    alert = build_alert(today, None, WEIGHTS, THRESHOLDS)
    assert alert.tier == "Trending"
    assert alert.prior_tier is None
    assert alert.signals["velocity"] == (1.0, 0.17, 0.17)
    assert alert.tone_avg == 0.5


def test_build_alert_tone_hidden_when_no_gdelt_rows():
    row = {
        "market": "za",
        "query_group": "tiktok_hashtag",
        "trend_score": 0.5,
        "tone_score": 0.0,
        "tone_rows": 0,
        "item_count": 200,
        "source_diversity": 3,
    }
    alert = build_alert(row, None, WEIGHTS, THRESHOLDS)
    assert alert.tone_avg is None


def test_build_subject_lists_markets_and_total():
    subject = build_subject(
        {"za": [_alert("za", "a", 0.7)], "ng": [_alert("ng", "b", 0.5)]},
        date(2026, 4, 23),
    )
    assert "2026-04-23" in subject
    assert "2 upgrades" in subject
    assert "NG 1" in subject
    assert "ZA 1" in subject


def test_build_subject_v2_leads_with_cultural_verdict(monkeypatch):
    """With the v2 mailer on and a daily_summary, the subject leads with the
    through_line (the cultural verdict), not the ops upgrade/mover count."""
    monkeypatch.setenv("MAILER_V2_ENABLED", "true")
    subject = build_subject(
        {"za": [_alert("za", "a", 0.7)]},
        date(2026, 5, 31),
        daily_summary={"through_line": "Youth reframe survival as a communal hustle."},
    )
    assert subject == "Trends Digest, 2026-05-31 | Youth reframe survival as a communal hustle"
    assert "upgrade" not in subject  # ops framing replaced by the verdict


def test_build_subject_v2_falls_back_when_no_summary(monkeypatch):
    """v2 on but no daily_summary -> the ops subject still renders, no crash."""
    monkeypatch.setenv("MAILER_V2_ENABLED", "true")
    subject = build_subject({"za": [_alert("za", "a", 0.7)]}, date(2026, 5, 31))
    assert "2026-05-31" in subject
    assert "1 upgrade" in subject


def test_render_text_sorts_items_by_score_within_market():
    by_market = {
        "za": [
            _alert("za", "lower", 0.45),
            _alert("za", "higher", 0.72),
        ]
    }
    out = render_text(by_market, date(2026, 4, 23))
    higher_idx = out.index("higher")
    lower_idx = out.index("lower")
    assert higher_idx < lower_idx


def test_render_text_has_per_market_header_and_stats():
    by_market = {
        "ng": [_alert("ng", "naija_youth", 0.55)],
        "za": [_alert("za", "load_shedding", 0.72)],
    }
    out = render_text(by_market, date(2026, 4, 23))
    assert "Trends Digest, 2026-04-23" in out
    assert "ZA: 1 upgrade" in out
    assert "NG: 1 upgrade" in out
    assert "Why we flagged it:" in out
    assert "Spiking fast" in out
    assert "https://example.com/naija_youth" in out


def test_render_html_escapes_entities_and_embeds_links():
    alert = TrendAlert(
        market="za",
        query_group="safe<script>",
        trend_score=0.72,
        tier="Trending",
        prior_tier="Emerging",
        item_count=10,
        source_diversity=2,
        signals={"velocity": (0.9, 0.17, 0.153)},
        samples=[
            {
                "title": "Dangerous <script>",
                "url": "https://example.com/x",
                "text": "",
                "source": "Src & Co",
            }
        ],
        top_persons=["A & B"],
        top_orgs=[],
        tone_avg=None,
    )
    out = render_html({"za": [alert]}, date(2026, 4, 23))
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "A &amp; B" in out
    assert "https://example.com/x" in out


def test_render_html_tone_bullet_surfaces_on_strong_signal():
    alert = _alert("ng", "naija_identity", 0.55, tone=0.30)
    out = render_html({"ng": [alert]}, date(2026, 4, 23))
    assert "Strong negative tone" in out


def test_send_email_digest_dry_run_when_disabled(monkeypatch):
    monkeypatch.setenv("EMAIL_ALERTS_ENABLED", "false")
    monkeypatch.setenv("GMAIL_USER", "a@b.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pw")
    monkeypatch.setenv("EMAIL_RECIPIENTS", "x@y.com")
    by_market = {"za": [_alert("za", "a", 0.7)]}
    with patch("src.alerts.email_digest.smtplib.SMTP") as smtp:
        status = send_email_digest(by_market, date(2026, 4, 23))
    # A deliberate dry run is its own status, not a send failure.
    assert status == "dry_run"
    smtp.assert_not_called()


def test_send_email_digest_happy_path(monkeypatch):
    monkeypatch.setenv("EMAIL_ALERTS_ENABLED", "true")
    monkeypatch.setenv("GMAIL_USER", "a@b.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setenv("EMAIL_RECIPIENTS", "x@y.com, z@w.com")
    by_market = {"za": [_alert("za", "load_shedding", 0.72)]}

    fake_server = MagicMock()
    fake_ctx = MagicMock()
    fake_ctx.__enter__.return_value = fake_server
    with patch("src.alerts.email_digest.smtplib.SMTP", return_value=fake_ctx) as smtp:
        status = send_email_digest(by_market, date(2026, 4, 23))
    assert status == "sent"
    smtp.assert_called_once()
    fake_server.starttls.assert_called_once()
    # App password should have had its spaces stripped before login.
    fake_server.login.assert_called_once_with("a@b.com", "abcdefghijklmnop")
    args, _ = fake_server.sendmail.call_args
    sender, recipients, payload = args
    assert sender == "a@b.com"
    assert recipients == ["x@y.com", "z@w.com"]
    # MIME body is base64-encoded; assert on visible headers instead.
    assert "Subject: Trends Digest, 2026-04-23" in payload
    assert "multipart/alternative" in payload


def test_send_email_digest_missing_creds(monkeypatch):
    monkeypatch.setenv("EMAIL_ALERTS_ENABLED", "true")
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.setenv("EMAIL_RECIPIENTS", "x@y.com")
    with patch("src.alerts.email_digest.smtplib.SMTP") as smtp:
        status = send_email_digest({"za": [_alert("za", "a", 0.7)]}, date(2026, 4, 23))
    assert status == "no_creds"
    smtp.assert_not_called()


def test_send_email_digest_missing_recipients(monkeypatch):
    monkeypatch.setenv("EMAIL_ALERTS_ENABLED", "true")
    monkeypatch.setenv("GMAIL_USER", "a@b.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pw")
    monkeypatch.setenv("EMAIL_RECIPIENTS", "")
    with patch("src.alerts.email_digest.smtplib.SMTP") as smtp:
        status = send_email_digest({"za": [_alert("za", "a", 0.7)]}, date(2026, 4, 23))
    assert status == "no_recipients"
    smtp.assert_not_called()


def test_send_email_digest_smtp_error_returns_smtp_failed(monkeypatch):
    import smtplib as smtp_module

    monkeypatch.setenv("EMAIL_ALERTS_ENABLED", "true")
    monkeypatch.setenv("GMAIL_USER", "a@b.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "pw")
    monkeypatch.setenv("EMAIL_RECIPIENTS", "x@y.com")

    fake_server = MagicMock()
    fake_server.login.side_effect = smtp_module.SMTPAuthenticationError(535, b"bad")
    fake_ctx = MagicMock()
    fake_ctx.__enter__.return_value = fake_server
    with patch("src.alerts.email_digest.smtplib.SMTP", return_value=fake_ctx):
        status = send_email_digest({"za": [_alert("za", "a", 0.7)]}, date(2026, 4, 23))
    assert status == "smtp_failed"


def _mover(market: str, qg: str, prior: float, current: float) -> TrendMover:
    return TrendMover(
        market=market,
        query_group=qg,
        current_score=current,
        prior_score=prior,
        delta=current - prior,
        tier="Monitoring" if current < 0.4 else "Emerging",
        prior_tier="Monitoring" if prior < 0.4 else "Emerging",
        item_count=50,
        source_diversity=5,
        samples=[
            {
                "title": f"{qg} moved today",
                "url": f"https://example.com/{qg}",
                "text": "",
                "source": "Daily Source",
            }
        ],
        top_persons=["Jane Doe"],
        top_orgs=["ACME"],
        tone_avg=None,
    )


def test_build_subject_lists_movers_and_issues_when_no_upgrades():
    """Zero-upgrade days now anchor the subject with the literal '0
    upgrades' prefix so subject parsing stays consistent across days
    (Thapelo flagged 7 May 2026: subject went from '2 upgrades... |
    9 movers' yesterday to '9 movers' only today, breaking same-shape
    parsing). Quiet days (no movers, no issues, no healed) still read
    'quiet day' so true silence is distinguishable from zero-upgrade-
    with-movers."""
    subject = build_subject(
        {},
        date(2026, 4, 24),
        movers_by_market={"za": [_mover("za", "a", 0.40, 0.48)]},
        issues=[IssueFlag(label="YOUTUBE (ZA)", detail="403 quota exceeded")],
    )
    assert "2026-04-24" in subject
    assert "0 upgrades" in subject
    assert "1 mover" in subject
    assert "1 issue" in subject


def test_build_subject_all_quiet_reads_quiet_day():
    subject = build_subject({}, date(2026, 4, 24))
    assert "quiet day" in subject


def test_build_subject_pluralises_correctly():
    subject = build_subject(
        {},
        date(2026, 4, 24),
        movers_by_market={"za": [_mover("za", "a", 0.40, 0.48), _mover("za", "b", 0.50, 0.45)]},
    )
    assert "2 movers" in subject
    assert "movers(s)" not in subject  # no legacy plural hack


def test_render_text_includes_movers_section():
    movers = {
        "za": [_mover("za", "load_shedding", 0.55, 0.63)],
        "ng": [_mover("ng", "naija_youth", 0.42, 0.50)],
    }
    out = render_text({}, date(2026, 4, 24), movers_by_market=movers)
    assert "What's moving" in out
    assert "load_shedding" in out
    assert "+0.080" in out or "+0.08" in out
    assert "0.550 -> 0.630" in out
    assert "https://example.com/load_shedding" in out


def test_render_html_shows_issues_in_red_block():
    issues = [
        IssueFlag(label="YOUTUBE (ZA)", detail="403 quota exceeded"),
        IssueFlag(label="RSS (KE)", detail="Nation Africa feed 404"),
    ]
    out = render_html({}, date(2026, 4, 24), issues=issues)
    assert "Things that broke" in out
    assert "YOUTUBE (ZA)" in out
    assert "403 quota exceeded" in out
    assert "Nation Africa feed 404" in out


def test_render_html_no_upgrades_no_movers_still_shows_issues():
    issues = [IssueFlag(label="RSS (KE)", detail="feed 404")]
    out = render_html({}, date(2026, 4, 24), issues=issues)
    assert "No tier upgrades today" in out
    assert "Things that broke" in out


def test_render_text_no_double_hyphens_or_em_dashes():
    """Writing-style guard: no `--`, no em dash, no en dash in rendered text."""
    by_market = {"za": [_alert("za", "load_shedding", 0.72)]}
    movers = {"ng": [_mover("ng", "naija_youth", 0.42, 0.50)]}
    out = render_text(by_market, date(2026, 4, 24), movers_by_market=movers)
    assert "--" not in out
    assert "\u2014" not in out  # em dash
    assert "\u2013" not in out  # en dash


def test_render_html_no_em_dash_entity():
    """No &mdash; or &ndash; in HTML either."""
    by_market = {"za": [_alert("za", "a", 0.7)]}
    out = render_html(by_market, date(2026, 4, 24))
    assert "&mdash;" not in out
    assert "&ndash;" not in out


def test_mover_delta_colour_positive_and_negative_differ():
    """Sanity check: positive and negative deltas render with different colours."""
    pos = _mover("za", "a", 0.40, 0.48)
    neg = _mover("za", "b", 0.48, 0.40)
    pos_out = render_html({}, date(2026, 4, 24), movers_by_market={"za": [pos]})
    neg_out = render_html({}, date(2026, 4, 24), movers_by_market={"za": [neg]})
    # Both render but border colours differ by sign.
    assert "#1e8449" in pos_out  # positive >= 0.03
    assert "#c0392b" in neg_out  # negative <= -0.03


def test_email_stats_renders_unclassified_rows_when_present():
    """Run stats footer shows unclassified_rows line when provided."""
    text = render_text(
        {},
        date(2026, 4, 24),
        stats={"scored_rows": 24, "unclassified_rows": 42},
    )
    assert "unclassified rows" in text
    assert "42" in text


# Quiet-day gate vs the v2 PULSE. The v2 PULSE leads on the daily verdict + the
# per-topic briefs, so it has substance even with zero tier upgrades; the gate
# must not suppress it. v1 (the ops digest) still skips a fully quiet day.


def test_quiet_day_v2_pulse_with_briefs_still_sends(monkeypatch):
    from src.alerts.detector import _should_skip_quiet_day

    monkeypatch.setenv("MAILER_V2_ENABLED", "true")
    briefs = {("za", "music_amapiano"): {"headline": "x"}}
    assert _should_skip_quiet_day(0, 0, [], [], briefs) is False


def test_quiet_day_v2_no_briefs_skips(monkeypatch):
    from src.alerts.detector import _should_skip_quiet_day

    monkeypatch.setenv("MAILER_V2_ENABLED", "true")
    assert _should_skip_quiet_day(0, 0, [], [], None) is True


def test_quiet_day_v1_skips_even_with_briefs(monkeypatch):
    from src.alerts.detector import _should_skip_quiet_day

    monkeypatch.delenv("MAILER_V2_ENABLED", raising=False)
    assert _should_skip_quiet_day(0, 0, [], [], {("za", "x"): {}}) is True


def test_quiet_day_with_upgrades_always_sends(monkeypatch):
    from src.alerts.detector import _should_skip_quiet_day

    monkeypatch.delenv("MAILER_V2_ENABLED", raising=False)
    assert _should_skip_quiet_day(1, 0, [], [], None) is False


# --- send_daily_digest status contract -------------------------------------
# These pin the exact 4th-tuple status strings the run_rss_now email-status
# marker row depends on. If the detector renames a status, _log_email_status
# would coerce the unknown value to 'render_error' and over-flag; these tests
# fail first and force both sides to stay in sync.


def test_send_daily_digest_returns_send_failed_when_smtp_fails(monkeypatch):
    """When send_email_digest returns False on a notable day (SMTP/auth blip),
    send_daily_digest's status element must be 'send_failed' so the marker row
    records a real no-email failure, not a quiet day."""
    from src.alerts import detector

    monkeypatch.delenv("MAILER_V2_ENABLED", raising=False)
    with (
        patch.object(detector, "_should_skip_quiet_day", return_value=False),
        patch.object(detector, "send_email_digest", return_value=False),
        patch.object(detector, "logger", MagicMock()),
    ):
        result = detector.send_daily_digest([], issues=[IssueFlag(label="X", detail="boom")])
    assert result[3] == "send_failed"


def test_send_daily_digest_returns_skipped_on_quiet_day(monkeypatch):
    """A legitimately quiet day must short-circuit BEFORE the send and return
    'skipped_nothing_notable', the healthy value the marker treats as ok. This
    is why a naive 'retry when status is not sent' would wrongly spam quiet
    days; the marker relies on this exact string to stay silent."""
    from src.alerts import detector

    monkeypatch.delenv("MAILER_V2_ENABLED", raising=False)
    with (
        patch.object(detector, "_should_skip_quiet_day", return_value=True),
        patch.object(detector, "send_email_digest") as send,
        patch.object(detector, "logger", MagicMock()),
    ):
        result = detector.send_daily_digest([])
    assert result[3] == "skipped_nothing_notable"
    send.assert_not_called()


def test_send_daily_digest_maps_dry_run_to_dry_run(monkeypatch):
    """A deliberate dry run (EMAIL_ALERTS_ENABLED != true) returns 'dry_run' from
    send_email_digest on a notable day; send_daily_digest must pass it through as
    'dry_run', NOT collapse it to 'send_failed'. The marker treats 'dry_run' as
    healthy, so a disabled-send run is not flagged as a delivery failure."""
    from src.alerts import detector

    monkeypatch.delenv("MAILER_V2_ENABLED", raising=False)
    with (
        patch.object(detector, "_should_skip_quiet_day", return_value=False),
        patch.object(detector, "send_email_digest", return_value="dry_run"),
        patch.object(detector, "logger", MagicMock()),
    ):
        result = detector.send_daily_digest([], issues=[IssueFlag(label="X", detail="boom")])
    assert result[3] == "dry_run"


def test_send_daily_digest_maps_no_creds_to_send_failed(monkeypatch):
    """A real no-email outcome (missing creds) is a genuine failure: it must
    collapse to 'send_failed' so the marker flags a no-email day."""
    from src.alerts import detector

    monkeypatch.delenv("MAILER_V2_ENABLED", raising=False)
    with (
        patch.object(detector, "_should_skip_quiet_day", return_value=False),
        patch.object(detector, "send_email_digest", return_value="no_creds"),
        patch.object(detector, "logger", MagicMock()),
    ):
        result = detector.send_daily_digest([], issues=[IssueFlag(label="X", detail="boom")])
    assert result[3] == "send_failed"
