import datetime as dt
import json
import re
from html import unescape
from types import SimpleNamespace

import pytest

from core.api import digest
from core.api.store import FixtureStore

STAGING = "https://f42-api-590353929363.us-central1.run.app"
D30 = "2026-09-30"


def card(item_id="i1", market="ZA", title="#amapiano", state_word="Rising", count_line="31 creators, 3 days",
         evidence=None, **more):
    return {"item_id": item_id, "market": market, "title": title, "state_word": state_word,
            "count_line": count_line, "evidence": evidence or [], **more}


def post(n, url, platform="tiktok", handle=None):
    return {"id": f"tt_{n}", "platform": platform, "handle": handle or f"@creator_{n}", "url": url,
            "thumbnail_url": f"https://example.invalid/thumb/{n}.jpg"}


def alert(watch_id="w_1", label="Amapiano in Joburg", item_id="i1", market="ZA", because="Entered Rising",
          **card_fields):
    return {"watch_id": watch_id, "label": label, "item_id": item_id, "market": market,
            "fired_because": because, "since": D30, "card": card(item_id, market, **card_fields)}


def response(alerts=(), waiting=()):
    return {"date": D30, "run_id": "r_detect_20260930_01", "alerts": list(alerts), "waiting": list(waiting)}


def watch(watch_id, target, rule, market="ZA", label="A watch"):
    return {"watch_id": watch_id, "created_at": "2026-09-29T08:00:00+02:00", "who": "passcode",
            "target": target, "market": market, "rule": rule, "label": label, "status": "active"}


def visible(html):
    """The words a reader sees: tags dropped, entities left as written."""
    return re.sub(r"<[^>]+>", " ", html)


# subject

def test_subject_counts_the_watches_that_fired():
    three = response([alert("w_1"), alert("w_2"), alert("w_3")])
    assert digest.subject(three) == "42 alerts, 30 September 2026: 3 watches fired"
    assert digest.subject(response([alert("w_1")])) == "42 alerts, 30 September 2026: 1 watch fired"
    assert digest.subject(response()) == "42 alerts, 30 September 2026: no watches fired"


def test_subject_counts_a_watch_once_when_it_fires_on_two_markets():
    both = response([alert("w_all", market="ZA"), alert("w_all", market="NG")])
    assert digest.subject(both) == "42 alerts, 30 September 2026: 1 watch fired"


# links

def test_topic_link_uses_the_app_route_and_encodes_the_parts():
    assert digest.topic_url(STAGING, "abc123", "ZA") == STAGING + "/#/t/abc123?market=ZA"
    assert digest.topic_url(STAGING + "/", "a b/&", "NG") == STAGING + "/#/t/a%20b%2F%26?market=NG"


def test_app_url_comes_from_the_environment_with_the_staging_default(monkeypatch):
    monkeypatch.delenv("F42_APP_URL", raising=False)
    assert digest.app_url() == STAGING
    monkeypatch.setenv("F42_APP_URL", "https://42.example.invalid/")
    assert digest.app_url() == "https://42.example.invalid"
    monkeypatch.setenv("F42_APP_URL", "javascript:alert(1)")
    with pytest.raises(ValueError):
        digest.app_url()


def test_post_links_keep_only_plain_web_addresses_and_at_most_two():
    evidence = [post(1, "javascript:alert(1)"), post(2, "data:text/html,hi"),
                post(3, "https://user:pw@example.invalid/p"), post(4, "/relative/path"), post(5, None),
                post(6, " https://example.invalid/tiktok/6 "), post(7, "http://example.invalid/ig/7", "instagram"),
                post(8, "https://example.invalid/yt/8", "youtube")]
    links = digest.post_links(card(evidence=evidence))
    assert [u for u, _ in links] == ["https://example.invalid/tiktok/6", "http://example.invalid/ig/7"]
    assert [words for _, words in links] == ["@creator_6 on TikTok", "@creator_7 on Instagram"]


def test_post_links_name_a_post_collected_as_twitter_on_x():
    links = digest.post_links(card(evidence=[post(1, "https://example.invalid/x/1", "twitter"),
                                             post(2, "https://example.invalid/x/2", "x")]))
    assert [words for _, words in links] == ["@creator_1 on X", "@creator_2 on X"]


# the HTML body

def test_html_carries_every_part_of_each_alert():
    html = digest.render_html(response([alert(evidence=[post(1, "https://example.invalid/tiktok/1")])]), STAGING)
    for words in ("Amapiano in Joburg", "South Africa", "Entered Rising", "#amapiano", "Rising",
                  "31 creators, 3 days", "@creator_1 on TikTok"):
        assert words in visible(html)
    assert f'href="{STAGING}/#/t/i1?market=ZA"' in html
    assert 'href="https://example.invalid/tiktok/1"' in html
    assert "42 alerts, 30 September 2026: 1 watch fired" in html


def test_html_escapes_every_value():
    evil = '<script>alert("x")</script>&'
    html = digest.render_html(response(
        [alert(label=evil, title=evil, because=evil, state_word=evil, count_line=evil, item_id='"><b>',
               evidence=[post(1, 'https://example.invalid/p?a=1&b="2"', handle=evil)])],
        [{"watch_id": "w_9", "label": evil, "waiting": evil}]), STAGING)
    assert "<script>" not in html and "<b>" not in html
    assert '"2"' not in html
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;&amp;" in html
    assert 'href="https://example.invalid/p?a=1&amp;b=&quot;2&quot;"' in html


def test_html_shows_the_held_back_reason_and_flag_on_a_card():
    html = digest.render_html(response([alert(flag_word="Likely coordinated",
                                              held_back={"rule": "G4", "reason": "likely_coordinated",
                                                         "reason_text": "Likely coordinated"})]), STAGING)
    assert "Held back: Likely coordinated" in visible(html)


def test_html_lists_waiting_entries_under_waiting_for_data():
    html = digest.render_html(response([], [{"watch_id": "w_w", "label": "Step breakout",
                                             "waiting": "Waiting for breakout detection"}]), STAGING)
    text = visible(html)
    assert "Waiting for data" in text
    assert text.index("Waiting for data") < text.index("Step breakout") < text.index("Waiting for breakout detection")
    assert "No watch fired this morning." in text


def test_html_leaves_the_waiting_section_out_when_nothing_waits():
    assert "Waiting for data" not in digest.render_html(response([alert()]), STAGING)


def test_html_has_the_footer_no_images_no_tracking_and_no_banned_words():
    html = digest.render_html(response([alert(evidence=[post(1, "https://example.invalid/tiktok/1")])],
                                       [{"watch_id": "w_w", "label": "x", "waiting": "Waiting for tone detection"}]),
                              STAGING)
    assert digest.FOOTER in unescape(visible(html))
    assert "published detection" in digest.FOOTER and "never feed 42's trust numbers" in digest.FOOTER
    assert "<img" not in html.lower() and "<script" not in html.lower() and "@import" not in html
    assert "thumb/" not in html
    lowered = html.lower()
    for banned in ("gen z", "genz", "nano banana", "lyria", "gemini", "powered by", "google"):
        assert banned not in lowered
    assert chr(0x2014) not in html and chr(0x2013) not in html and "--" not in visible(html)


# the plain-text body

def test_text_carries_the_same_list_with_safe_links_only():
    text = digest.render_text(response(
        [alert(evidence=[post(1, "javascript:alert(1)"), post(2, "https://example.invalid/tiktok/2")],
               held_back={"rule": "G4", "reason": "likely_coordinated", "reason_text": "Likely coordinated"})],
        [{"watch_id": "w_w", "label": "Step breakout", "waiting": "Waiting for breakout detection"}]), STAGING)
    assert text.splitlines()[0] == "42 alerts, 30 September 2026: 1 watch fired"
    for words in ("Amapiano in Joburg", "South Africa", "Entered Rising", "#amapiano", "Rising",
                  "31 creators, 3 days", "Held back: Likely coordinated", f"{STAGING}/#/t/i1?market=ZA",
                  "@creator_2 on TikTok: https://example.invalid/tiktok/2", "Waiting for data",
                  "Step breakout: Waiting for breakout detection", digest.FOOTER):
        assert words in text
    assert "javascript:" not in text and chr(0x2014) not in text and "--" not in text


# from the store, and the command

def test_build_renders_the_alerts_route_list_from_fixtures():
    watches = [watch("w_step", {"kind": "hashtag", "value": "#fixture_za_step"}, {"state_in": ["emerging"]},
                     label="Step watch"),
               watch("w_bot", {"kind": "hashtag", "value": "#fixture_za_botnet"}, {"state_in": ["rising"]}),
               watch("w_wait", {"kind": "hashtag", "value": "#fixture_za_step"}, {"creator_surge": True},
                     label="Step surge")]
    out = digest.build(FixtureStore(), watches, D30, STAGING)
    assert out["subject"] == "42 alerts, 30 September 2026: 2 watches fired"
    assert out["alerts"] == 2 and out["waiting"] == 1
    assert "Step watch" in out["html"] and "Entered Emerging" in out["text"]
    assert "Held back: Likely coordinated" in out["text"]
    assert "Waiting for creator views detection" in out["html"]


def test_dry_run_writes_one_html_and_one_text_file(tmp_path, monkeypatch):
    monkeypatch.setenv("F42_DATA", "fixtures")
    monkeypatch.delenv("F42_APP_URL", raising=False)
    watches = tmp_path / "watches.json"
    watches.write_text(json.dumps([watch("w_step", {"kind": "hashtag", "value": "#fixture_za_step"},
                                         {"state_in": ["emerging"]})]), encoding="utf-8")
    out = tmp_path / "out"
    assert digest.main(["--dry-run", "--date", D30, "--out", str(out), "--watches", str(watches)]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["alerts-2026-09-30.html", "alerts-2026-09-30.txt"]
    html = (out / "alerts-2026-09-30.html").read_text(encoding="utf-8")
    text = (out / "alerts-2026-09-30.txt").read_text(encoding="utf-8")
    assert "#fixture_za_step" in html and text.startswith("42 alerts, 30 September 2026: 1 watch fired")


def test_without_watches_the_dry_run_reads_the_current_watch_list(tmp_path, monkeypatch):
    monkeypatch.setenv("F42_DATA", "fixtures")
    monkeypatch.setattr(digest, "current_watches", lambda: [])
    out = tmp_path / "out"
    assert digest.main(["--dry-run", "--date", D30, "--out", str(out)]) == 0
    assert (out / "alerts-2026-09-30.txt").read_text(encoding="utf-8").startswith(
        "42 alerts, 30 September 2026: no watches fired")


def test_sending_is_not_built(tmp_path, capsys):
    out = tmp_path / "out"
    assert digest.main(["--date", D30, "--out", str(out)]) == 2
    assert not out.exists()
    assert "mail route" in capsys.readouterr().err


# sending (--send): Gmail SMTP, the way engine/src/alerts/email_digest.py sent

SENDER = "jhb.analytics@gmail.com"
PASSWORD = "abcd efgh ijkl mnop"
STRIPPED = PASSWORD.replace(" ", "")
TO = ["one@example.invalid", "two@example.invalid"]
TODAY = "2026-10-01"


class FakeSMTP:
    """Stands in for smtplib.SMTP: records each connection, never opens a socket."""
    made = []
    fail = None
    refuse = {}

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.tls = self.login_as = None
        self.sent = []
        FakeSMTP.made.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.tls = context

    def login(self, user, password):
        if FakeSMTP.fail:
            raise FakeSMTP.fail
        self.login_as = (user, password)

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.sent.append((msg, from_addr, list(to_addrs)))
        # smtplib returns the recipients the server refused when it accepted at least one.
        return {r: v for r, v in FakeSMTP.refuse.items() if r in to_addrs}


@pytest.fixture
def mail(tmp_path, monkeypatch):
    """A --send run on fixtures: one watch that fires on 30 September, sending switched on, a fake SMTP."""
    FakeSMTP.made, FakeSMTP.fail, FakeSMTP.refuse = [], None, {}
    monkeypatch.setattr(digest.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("F42_DATA", "fixtures")
    monkeypatch.setenv("RUN_DATE", TODAY)
    monkeypatch.delenv("F42_APP_URL", raising=False)
    monkeypatch.setenv("GMAIL_USER", SENDER)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("DIGEST_RECIPIENTS", " " + ", ".join(TO) + ", ")
    monkeypatch.setenv("DIGEST_SEND_ENABLED", "true")
    runs = digest.chain.MemoryRunsStore()
    monkeypatch.setattr(digest, "runs_store", lambda: runs)
    watches = tmp_path / "watches.json"
    watches.write_text(json.dumps([watch("w_step", {"kind": "hashtag", "value": "#fixture_za_step"},
                                         {"state_in": ["emerging"]})]), encoding="utf-8")
    return SimpleNamespace(runs=runs, argv=["--send", "--date", D30, "--watches", str(watches)], watches=watches)


def sent_messages():
    return [m for s in FakeSMTP.made for m in s.sent]


def no_secret(*texts):
    for text in texts:
        assert PASSWORD not in text and STRIPPED not in text


def test_send_mails_one_multipart_message_to_every_recipient_hidden_from_each_other(mail, capsys):
    assert digest.main(mail.argv) == 0
    [smtp] = FakeSMTP.made
    assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.gmail.com", 587, 30)
    assert smtp.tls is not None and smtp.login_as == (SENDER, STRIPPED)
    [(msg, from_addr, to_addrs)] = smtp.sent
    assert from_addr == SENDER and to_addrs == TO
    assert msg["From"] == SENDER and msg["To"] == SENDER and msg["Bcc"] is None and msg["Cc"] is None
    assert msg["Subject"] == "42 alerts, 30 September 2026: 1 watch fired"
    raw = msg.as_string()
    assert not any(r in raw for r in TO)
    assert msg.get_content_type() == "multipart/alternative"
    parts = {p.get_content_type(): p.get_content() for p in msg.iter_parts()}
    assert sorted(parts) == ["text/html", "text/plain"]
    assert parts["text/plain"].startswith("42 alerts, 30 September 2026: 1 watch fired")
    assert "#fixture_za_step" in parts["text/html"]
    out = capsys.readouterr()
    assert "Sent" in out.out and "2 recipients" in out.out
    no_secret(out.out, out.err, raw, json.dumps(mail.runs.rows))


def test_send_records_a_digest_runs_row_and_a_rerun_the_same_day_sends_nothing(mail, capsys):
    assert digest.main(mail.argv) == 0
    rows = mail.runs.rows
    assert [(r["stage"], r["run_date"], r["status"]) for r in rows] == [("digest", TODAY, "running"),
                                                                         ("digest", TODAY, "ok")]
    assert rows[0]["run_id"] == rows[1]["run_id"] and rows[1]["finished_at"]
    assert rows[1]["counts"] == {"sent": True, "recipients": 2, "alerts": 1, "waiting": 0, "digest_date": D30,
                                "refused": 0}
    capsys.readouterr()
    assert digest.main(mail.argv) == 0
    assert len(sent_messages()) == 1
    assert "Already sent" in capsys.readouterr().out
    assert len(mail.runs.rows) == 2  # the rerun only reads


def test_a_run_still_sending_blocks_a_second_send(mail, capsys):
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    mail.runs.append({"run_id": "digest-x", "stage": "digest", "run_date": TODAY, "status": "running",
                      "started_at": started, "finished_at": None, "counts": None, "error": None})
    assert digest.main(mail.argv) == 0
    assert sent_messages() == []
    assert "still sending" in capsys.readouterr().out


def test_a_failed_send_earlier_the_same_day_is_retried(mail):
    FakeSMTP.fail = digest.smtplib.SMTPServerDisconnected("gone")
    assert digest.main(mail.argv) == 1
    FakeSMTP.fail = None
    assert digest.main(mail.argv) == 0
    assert len(sent_messages()) == 1
    assert [r["status"] for r in mail.runs.rows] == ["running", "failed", "running", "ok"]


def test_an_smtp_failure_exits_nonzero_with_the_exception_type_only(mail, capsys, caplog):
    FakeSMTP.fail = digest.smtplib.SMTPAuthenticationError(535, f"bad login for {SENDER} with {STRIPPED} / {PASSWORD}")
    assert digest.main(mail.argv) == 1
    out = capsys.readouterr()
    assert "SMTPAuthenticationError" in out.err
    assert "bad login" not in out.err + out.out
    assert mail.runs.rows[-1]["status"] == "failed" and mail.runs.rows[-1]["error"] == "SMTPAuthenticationError"
    no_secret(out.out, out.err, caplog.text, json.dumps(mail.runs.rows))


def test_a_refused_recipient_is_recorded_and_exits_nonzero_without_a_second_send(mail, capsys):
    FakeSMTP.refuse = {TO[1]: (550, b"no such user")}
    assert digest.main(mail.argv) == 1
    out = capsys.readouterr()
    assert "1 of 2 recipients refused" in out.err
    assert TO[1] not in out.out + out.err
    rows = mail.runs.rows
    assert rows[-1]["status"] == "ok"
    assert rows[-1]["counts"]["recipients"] == 2 and rows[-1]["counts"]["refused"] == 1
    # The accepted recipient already has it, so a rerun the same day must not mail them again.
    assert digest.main(mail.argv) == 0
    assert len(sent_messages()) == 1


def test_sending_switched_off_sends_nothing_and_exits_cleanly(mail, monkeypatch, capsys):
    for value in (None, "", "false", "1", "yes"):
        if value is None:
            monkeypatch.delenv("DIGEST_SEND_ENABLED")
        else:
            monkeypatch.setenv("DIGEST_SEND_ENABLED", value)
        assert digest.main(mail.argv) == 0
        assert "DIGEST_SEND_ENABLED" in capsys.readouterr().out
    monkeypatch.setenv("DIGEST_SEND_ENABLED", " TRUE ")
    assert digest.main(mail.argv) == 0
    assert len(sent_messages()) == 1


@pytest.mark.parametrize("name, value, words", [
    ("GMAIL_USER", None, "GMAIL_USER"),
    ("GMAIL_USER", "not an address", "GMAIL_USER"),
    ("GMAIL_APP_PASSWORD", None, "GMAIL_APP_PASSWORD"),
    ("GMAIL_APP_PASSWORD", "   ", "GMAIL_APP_PASSWORD"),
    ("DIGEST_RECIPIENTS", None, "DIGEST_RECIPIENTS"),
    ("DIGEST_RECIPIENTS", " , ", "DIGEST_RECIPIENTS"),
    ("DIGEST_RECIPIENTS", "one@example.invalid, Two <two@example.invalid>", "DIGEST_RECIPIENTS"),
    ("DIGEST_RECIPIENTS", "one@example.invalid,bad\r\nBcc: x@example.invalid", "DIGEST_RECIPIENTS"),
    ("DIGEST_RECIPIENTS", "one@example.invalid;two@example.invalid", "DIGEST_RECIPIENTS"),
])
def test_missing_or_bad_settings_send_nothing_and_exit_nonzero(mail, monkeypatch, capsys, name, value, words):
    if value is None:
        monkeypatch.delenv(name)
    else:
        monkeypatch.setenv(name, value)
    assert digest.main(mail.argv) == 2
    assert FakeSMTP.made == [] and mail.runs.rows == []
    out = capsys.readouterr()
    assert words in out.err
    no_secret(out.out, out.err)


def test_no_alert_and_nothing_waiting_means_no_email(mail, monkeypatch, capsys):
    mail.watches.write_text("[]", encoding="utf-8")
    assert digest.main(mail.argv) == 0
    assert FakeSMTP.made == [] and mail.runs.rows == []
    assert "No email" in capsys.readouterr().out


def test_a_waiting_watch_alone_still_sends(mail, capsys):
    # tone_flip is judged from v_item_tone_daily now; creator_surge still waits while no watch_matches can be read.
    mail.watches.write_text(json.dumps([watch("w_wait", {"kind": "creator", "value": "@fixture_za_1"},
                                              {"creator_surge": True}, label="Creator surge")]), encoding="utf-8")
    assert digest.main(mail.argv) == 0
    [(msg, _, _)] = sent_messages()
    assert msg["Subject"] == "42 alerts, 30 September 2026: no watches fired"


def test_an_unreadable_runs_table_sends_nothing(mail, monkeypatch, capsys):
    class Broken:
        def latest(self, stage, day):
            raise RuntimeError("bigquery is down")

        def append(self, row):
            raise AssertionError("must not write")

    monkeypatch.setattr(digest, "runs_store", lambda: Broken())
    assert digest.main(mail.argv) == 1
    assert FakeSMTP.made == []
    assert "RuntimeError" in capsys.readouterr().err


def test_send_and_dry_run_together_are_refused(mail, tmp_path):
    with pytest.raises(SystemExit):
        digest.main(mail.argv + ["--dry-run", "--out", str(tmp_path / "out")])
    assert FakeSMTP.made == []


def test_dry_run_never_connects_or_records_even_with_sending_on(mail, tmp_path):
    out = tmp_path / "out"
    assert digest.main(["--dry-run", "--date", D30, "--out", str(out), "--watches", str(mail.watches)]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["alerts-2026-09-30.html", "alerts-2026-09-30.txt"]
    assert FakeSMTP.made == [] and mail.runs.rows == []


def test_without_a_date_a_detect_run_older_than_today_sends_nothing(mail, monkeypatch, capsys):
    # Today's detect has not landed: the latest good run is 30 September, already mailed on its own morning.
    argv = [a for a in mail.argv if a not in ("--date", D30)]
    assert digest.main(argv) == 0
    assert FakeSMTP.made == [] and mail.runs.rows == []
    assert "No email" in capsys.readouterr().out
    monkeypatch.setenv("RUN_DATE", D30)
    assert digest.main(argv) == 0
    assert len(sent_messages()) == 1


# the jobs image: core.api.digest must import without the web stack

def test_importing_the_digest_pulls_in_no_fastapi_starlette_or_agent_app():
    import subprocess
    import sys
    from pathlib import Path
    root = Path(digest.__file__).resolve().parents[2]
    code = "import sys, core.api.digest; print('\\n'.join(sorted(sys.modules)))"
    done = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, check=True)
    loaded = set(done.stdout.split())
    assert "core.api.digest" in loaded
    assert not {m for m in loaded if m.split(".")[0] in ("fastapi", "starlette")}
    assert "core.api.agent_app" not in loaded


def test_the_digest_and_the_app_share_one_watch_list(monkeypatch):
    from core.api import agent_app, watchlist
    monkeypatch.delenv("F42_DATA", raising=False)
    assert agent_app.WATCHES is watchlist.WATCHES and agent_app._lock is watchlist._lock
    row = agent_app.watch_row(watch("w_shared", {"kind": "hashtag", "value": "#x"}, {"state_in": ["rising"]}))
    monkeypatch.setattr(watchlist, "WATCHES", [row])
    monkeypatch.setattr(agent_app, "WATCHES", watchlist.WATCHES)
    assert [w["watch_id"] for w in watchlist.current_watches()] == ["w_shared"]
    assert [w["watch_id"] for w in agent_app.current_watches()] == ["w_shared"]
    assert digest.current_watches is watchlist.current_watches


def test_the_app_reads_watches_from_bigquery_through_its_own_client(monkeypatch):
    from core.api import agent_app

    class Client:
        def query(self, sql, job_config=None):
            assert "test-project.intelligence_42_agent.watches" in sql
            return SimpleNamespace(result=lambda: [])

    monkeypatch.setenv("F42_DATA", "bigquery")
    monkeypatch.setenv("F42_PROJECT", "test-project")
    monkeypatch.setattr(agent_app, "bigquery_client", lambda: Client())
    assert agent_app.current_watches() == []
