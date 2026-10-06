import json
import logging
import urllib.error
import urllib.request

import pytest

from core.api import channel
from core.api.store import FixtureStore

BASE = "https://42.example.invalid"
D30 = "2026-09-30"
HOOK = "https://hooks.slack.com/services/T0SECRET/B0SECRET/sEcReTtOkEn42"
TEAMS_HOOK = ("https://prod-12.westeurope.logic.azure.com:443/workflows/abc/triggers/manual/paths/invoke"
              "?api-version=2016-06-01&sig=sEcReTsIg42")
HANDLE = "@za_creator_one"
BARE_HANDLE = "za_creator_one"
EXCERPT = "caption words that belong to the author only"
SPAN = "transcript words that belong to the author only"
QUESTION = "What is za_creator_one posting about this week?"
CLAIM = "The creator said the quoted post line again and again"
QUOTE = "quoted post line from the author"
SHORT_ANSWER = "The short answer text stays in the app"
CREATOR_LABEL = "Watching the one creator I follow"
QUERY_VALUE = "@za_creator_one mentions"
BRAND_VALUE = "Secret Brand Launch"
HEADLINE_CREATOR = "za_creator_one is the biggest thing in South Africa"
LEAKS = ["@", BARE_HANDLE, EXCERPT, SPAN, QUESTION, CLAIM, QUOTE, SHORT_ANSWER, CREATOR_LABEL, QUERY_VALUE,
         BRAND_VALUE, HEADLINE_CREATOR, "Hidden fourth card", "Brand item title", "Query item title",
         "My amapiano watch", "hooks.slack.com", "sEcReT"]


def evidence(n):
    return {"id": f"tt_{n}", "platform": "tiktok", "handle": HANDLE, "url": f"https://example.invalid/p/{n}",
            "text": EXCERPT, "transcript_span": {"start_s": 1.0, "end_s": 3.0, "text": SPAN}}


def card(item_id, title, kind="hashtag", state_word="Rising", count_line="31 creators, 3 days", market="ZA"):
    return {"item_id": item_id, "market": market, "date": D30, "kind": kind, "title": title,
            "state_word": state_word, "count_line": count_line, "explained": True,
            "explanation": f"{HANDLE} started it. {EXCERPT}",
            "claims": [{"id": "c1", "text": CLAIM, "quotes": [QUOTE], "evidence_ids": ["tt_1"]}],
            "evidence": [evidence(1), evidence(2)], "ask": f"What is behind {title}?"}


def market(code, status="published", cards=(), more=()):
    return {"market": code, "label": channel.LABELS[code], "status": status, "banners": [],
            "cards": list(cards), "more": list(more)}


def today(headline_item="i_creator", headline_text=HEADLINE_CREATOR, ke_status="data_issue"):
    za = market("ZA", cards=[card("i_creator", HANDLE, kind="creator"), card("i_amapiano", "#amapiano"),
                             card("i_sound", "fixture sound", kind="sound", state_word="Emerging"),
                             card("i_fourth", "Hidden fourth card")],
                more=[card("i_more", "Hidden more card")])
    ng = market("NG", cards=[card("i_owambe", "#owambe", market="NG")])
    ke = market("KE", status=ke_status)
    headline = {"text": headline_text, "market": "ZA", "item_id": headline_item, "claim_ids": ["c1"]}
    return {"date": D30, "status": "partial", "headline": headline, "markets": [za, ng, ke]}


def alert(watch_id, kind, item_card, label, because="Entered Rising", mkt="ZA"):
    return {"watch_id": watch_id, "label": label, "item_id": item_card["item_id"], "market": mkt,
            "fired_because": because, "since": D30, "card": item_card, "watch_kind": kind}


def alerts():
    return [alert("w_item", "item", card("i_amapiano", "#amapiano"), "My amapiano watch"),
            alert("w_item_creator", "item", card("i_creator", HANDLE, kind="creator"), "My amapiano watch"),
            alert("w_creator", "creator", card("i_creator", HANDLE, kind="creator"), CREATOR_LABEL),
            alert("w_query", "query", card("i_q", "Query item title", market="NG"), QUERY_VALUE, mkt="NG"),
            alert("w_brand", "brand", card("i_b", "Brand item title", market="KE"), BRAND_VALUE, mkt="KE")]


def scheduled():
    answer = {"status": "complete", "short_answer": SHORT_ANSWER,
              "claims": [{"id": "c1", "text": CLAIM, "quotes": [QUOTE], "evidence_ids": ["tt_1"]}],
              "evidence": [evidence(1)]}
    return [{"ask_id": "a_sched_1", "schedule_id": "s_1", "question": QUESTION, "status": "complete",
             "answer": answer},
            {"ask_id": "a_sched_2", "schedule_id": "s_2", "question": QUESTION, "status": "failed", "answer": None}]


def dumped(payload):
    return json.dumps(payload, ensure_ascii=False)


def slack_text(payload):
    return "\n".join(b["text"]["text"] for b in payload["blocks"] if b.get("type") == "section")


def teams_text(payload):
    body = payload["attachments"][0]["content"]["body"]
    return "\n".join(b["text"] for b in body if b.get("type") == "TextBlock")


# what reaches the channel

@pytest.mark.parametrize("flavour", ["slack", "teams"])
def test_no_handle_post_text_question_claim_or_label_reaches_the_channel(flavour):
    payload = channel.render(today(), alerts(), scheduled(), BASE, flavour)
    text = dumped(payload)
    for leak in LEAKS:
        assert leak not in text, leak


@pytest.mark.parametrize("flavour", ["slack", "teams"])
def test_the_closed_field_list_is_there(flavour):
    payload = channel.render(today(), alerts(), scheduled(), BASE, flavour)
    text = slack_text(payload) if flavour == "slack" else teams_text(payload)
    for words in ("South Africa: Published", "Nigeria: Published", "Kenya: Published with a data issue",
                  "Today's brief is ready", "A creator is Rising in South Africa", "#amapiano", "Rising",
                  "31 creators, 3 days", "fixture sound", "Emerging", "#owambe", "Entered Rising",
                  "A watched creator moved in South Africa", "A watched search moved in Nigeria",
                  "A watched search moved in Kenya", "Answered", "Failed"):
        assert words in text, words
    assert f"{BASE}/#/today?date={D30}" in dumped(payload)
    assert f"{BASE}/#/t/i_amapiano?market=ZA" in dumped(payload)
    assert f"{BASE}/#/alerts" in dumped(payload)


def test_a_creator_kind_headline_is_replaced_and_any_other_headline_goes_out():
    creator = slack_text(channel.render(today(), [], [], BASE, "slack"))
    assert "Today's brief is ready" in creator and HEADLINE_CREATOR not in creator
    plain = slack_text(channel.render(today("i_amapiano", "Amapiano steps took over South Africa"), [], [], BASE,
                                      "slack"))
    assert "Amapiano steps took over South Africa" in plain and "Today's brief is ready" not in plain


def test_a_headline_whose_card_cannot_be_found_or_that_carries_a_handle_is_replaced():
    missing = slack_text(channel.render(today("i_gone", "A finding about nothing listed"), [], [], BASE, "slack"))
    assert "Today's brief is ready" in missing and "A finding about nothing listed" not in missing
    handle = slack_text(channel.render(today("i_amapiano", f"{HANDLE} took over"), [], [], BASE, "slack"))
    assert "Today's brief is ready" in handle and "@" not in handle


def test_no_headline_gives_the_brief_is_ready_line():
    resp = today()
    resp["headline"] = None
    assert "Today's brief is ready" in slack_text(channel.render(resp, [], [], BASE, "slack"))


def test_at_most_three_cards_per_market_from_cards_only():
    text = slack_text(channel.render(today(), [], [], BASE, "slack"))
    za = text[text.index("South Africa: Published"):text.index("Nigeria: Published")]
    assert za.count(f"{BASE}/#/t/") == 3
    assert "Hidden fourth card" not in text and "Hidden more card" not in text


def test_a_title_with_a_handle_is_worded_as_a_creator_even_when_kind_is_not_creator():
    resp = today("i_amapiano")
    resp["markets"][1]["cards"] = [card("i_odd", "@odd_handle", kind="topic", market="NG")]
    text = slack_text(channel.render(resp, [], [], BASE, "slack"))
    assert "A creator is Rising in Nigeria" in text and "odd_handle" not in text


def test_a_market_without_a_brief_says_no_brief_today():
    resp = today()
    resp["markets"][2]["status"] = None
    text = slack_text(channel.render(resp, [], [], BASE, "slack"))
    assert "Kenya: No brief today" in text


def test_no_brief_at_all_gives_three_no_brief_lines_and_no_headline():
    text = slack_text(channel.render({"date": D30, "headline": None, "markets": []}, [], [], BASE, "slack"))
    for name in ("South Africa", "Nigeria", "Kenya"):
        assert f"{name}: No brief today" in text
    assert "Today's brief is ready" not in text


def test_alerts_and_scheduled_answers_are_capped_with_a_pointer_to_the_rest():
    many = [alert(f"w_{n}", "item", card(f"i_{n}", f"#tag{n}"), "x") for n in range(15)]
    text = slack_text(channel.render(today(), many, [], BASE, "slack"))
    assert "#tag9" in text and "#tag10" not in text
    assert "5 more on the Alerts screen" in text


def test_an_alert_without_a_known_watch_kind_is_worded_as_a_search():
    odd = alert("w_odd", None, card("i_x", "Typed words maybe"), "x")
    text = slack_text(channel.render(today(), [odd], [], BASE, "slack"))
    assert "A watched search moved in South Africa" in text and "Typed words maybe" not in text


def test_slack_payload_is_mrkdwn_sections_with_escaped_text():
    resp = today("i_amapiano", "Fish <&> chips")
    payload = channel.render(resp, [], [], BASE, "slack")
    assert set(payload) == {"text", "blocks"} and payload["text"]
    sections = [b for b in payload["blocks"] if b["type"] == "section"]
    assert sections and all(b["text"]["type"] == "mrkdwn" for b in sections)
    assert all(len(b["text"]["text"]) <= 3000 for b in sections)
    assert "Fish &lt;&amp;&gt; chips" in slack_text(payload)
    assert f"<{BASE}/#/t/i_amapiano?market=ZA|" in slack_text(payload)


def test_teams_payload_is_an_adaptive_card_message():
    payload = channel.render(today(), [], [], BASE, "teams")
    assert payload["type"] == "message"
    attachment = payload["attachments"][0]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert attachment["content"]["type"] == "AdaptiveCard"
    assert f"]({BASE}/#/t/i_amapiano?market=ZA)" in teams_text(payload)


def test_an_unknown_flavour_is_refused():
    with pytest.raises(ValueError):
        channel.render(today(), [], [], BASE, "email")


# the webhook address

@pytest.mark.parametrize("url", [HOOK, "https://hooks.slack.com/workflows/T1/A1/1/abc"])
def test_slack_addresses_are_slack(url):
    assert channel.flavour_of(url) == "slack"


@pytest.mark.parametrize("url", [TEAMS_HOOK,
                                 "https://ogilvy.webhook.office.com/webhookb2/abc@def/IncomingWebhook/x/y",
                                 "https://outlook.office.com/webhook/abc/IncomingWebhook/x/y",
                                 "https://outlook.office365.com/webhook/abc",
                                 "https://default0a1b.2c.environment.api.powerplatform.com:443/powerautomate/"
                                 "automations/direct/workflows/abc/triggers/manual/paths/invoke?sig=x",
                                 "https://make.powerautomate.com/hook/abc"])
def test_teams_addresses_are_teams(url):
    assert channel.flavour_of(url) == "teams"


@pytest.mark.parametrize("url", ["https://example.invalid/hooks.slack.com/sEcReTpath",
                                 "http://hooks.slack.com/services/T/B/sEcReTpath",
                                 "https://hooks.slack.com.evil.invalid/sEcReTpath",
                                 "https://notoffice.com/sEcReTpath", "sEcReTpath", ""])
def test_anything_else_is_refused_without_repeating_the_address(url):
    with pytest.raises(ValueError) as caught:
        channel.flavour_of(url)
    assert "sEcReT" not in str(caught.value) and (not url or url not in str(caught.value))


# posting

class FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeHttp:
    Request = urllib.request.Request

    def __init__(self, raises=None, status=200):
        self.raises, self.status, self.seen = raises, status, []

    def urlopen(self, req, timeout=None):
        self.seen.append(req)
        if self.raises is not None:
            raise self.raises
        return FakeResponse(self.status)


def test_post_sends_the_payload_as_json():
    http = FakeHttp()
    channel.post({"text": "hello"}, HOOK, http)
    (req,) = http.seen
    assert req.get_method() == "POST" and req.full_url == HOOK
    assert json.loads(req.data.decode("utf-8")) == {"text": "hello"}
    assert req.get_header("Content-type") == "application/json"


def test_an_http_error_is_reported_as_its_status_only(caplog):
    caplog.set_level(logging.DEBUG)
    err = urllib.error.HTTPError(HOOK, 403, f"forbidden at {HOOK}", None, None)
    with pytest.raises(channel.ChannelError) as caught:
        channel.post({"text": "x"}, HOOK, FakeHttp(raises=err))
    assert str(caught.value) == "Channel post failed: HTTP 403"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert "sEcReT" not in repr(caught.value) and "sEcReT" not in caplog.text


def test_any_other_failure_is_reported_as_its_class_name_only(caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(channel.ChannelError) as caught:
        channel.post({"text": "x"}, HOOK, FakeHttp(raises=urllib.error.URLError(f"cannot reach {HOOK}")))
    assert str(caught.value) == "Channel post failed: URLError"
    assert caught.value.__context__ is None and "sEcReT" not in caplog.text


def test_a_bad_address_inside_post_does_not_echo_it():
    with pytest.raises(channel.ChannelError) as caught:
        channel.post({"text": "x"}, "sEcReTpath", FakeHttp())
    assert str(caught.value) == "Channel post failed: ValueError"
    assert caught.value.__context__ is None


def test_a_non_success_status_without_an_exception_is_a_failure():
    with pytest.raises(channel.ChannelError) as caught:
        channel.post({"text": "x"}, HOOK, FakeHttp(status=500))
    assert str(caught.value) == "Channel post failed: HTTP 500"


# from the store, and the command

def watch(watch_id, target, rule, label="A watch", mkt="ZA"):
    return {"watch_id": watch_id, "created_at": "2026-09-29T08:00:00+02:00", "who": "passcode",
            "target": target, "market": mkt, "rule": rule, "label": label, "status": "active"}


WATCHES = [watch("w_step", json.dumps({"kind": "hashtag", "value": "#fixture_za_step"}), {"state_in": ["emerging"]},
                 label="Step watch typed by a user")]


def test_gather_reads_today_and_the_days_alerts_from_fixtures():
    resp, fired = channel.gather(FixtureStore(), WATCHES, D30)
    assert resp["date"] == D30
    assert {m["market"]: m["status"] for m in resp["markets"]} == {"ZA": "published", "NG": "published",
                                                                   "KE": "data_issue"}
    assert [(a["watch_id"], a["watch_kind"]) for a in fired] == [("w_step", "hashtag")]
    text = slack_text(channel.render(resp, fired, [], BASE, "slack"))
    assert "Kenya: Published with a data issue" in text and "Entered Emerging" in text
    assert "Step watch typed by a user" not in text


def test_gather_marks_a_market_with_no_brief_row():
    resp, _ = channel.gather(FixtureStore(), [], "2026-09-29")
    assert {m["market"]: m["status"] for m in resp["markets"]}["KE"] is None
    assert "Kenya: No brief today" in slack_text(channel.render(resp, [], [], BASE, "slack"))


def test_gather_on_a_day_without_any_brief_or_detect_run():
    resp, fired = channel.gather(FixtureStore(), WATCHES, "2026-01-01")
    assert fired == [] and resp["headline"] is None
    assert "South Africa: No brief today" in slack_text(channel.render(resp, fired, [], BASE, "slack"))


@pytest.fixture
def cli(monkeypatch, tmp_path):
    monkeypatch.setenv("F42_DATA", "fixtures")
    monkeypatch.setenv("F42_APP_URL", BASE)
    monkeypatch.delenv("CHANNEL_WEBHOOK_URL", raising=False)
    posted = []
    monkeypatch.setattr(channel, "post", lambda payload, url, http=None: posted.append((payload, url)))
    monkeypatch.setattr(channel, "current_watches", lambda: [])
    path = tmp_path / "watches.json"
    path.write_text(json.dumps(WATCHES), encoding="utf-8")
    return posted, str(path)


def test_the_default_is_a_dry_run_even_with_the_secret_set(cli, monkeypatch, capsys, caplog):
    posted, watches = cli
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("CHANNEL_WEBHOOK_URL", HOOK)
    assert channel.main(["--date", D30, "--watches", watches]) == 0
    out = capsys.readouterr()
    assert posted == []
    assert "South Africa: Published" in out.out and "Entered Emerging" in out.out
    for leak in ("sEcReT", "hooks.slack.com"):
        assert leak not in out.out + out.err + caplog.text


def test_post_without_the_secret_is_a_dry_run(cli, capsys):
    posted, watches = cli
    assert channel.main(["--post", "--date", D30, "--watches", watches]) == 0
    out = capsys.readouterr()
    assert posted == [] and "South Africa: Published" in out.out
    assert "CHANNEL_WEBHOOK_URL" in out.err


def test_post_with_the_secret_posts_the_detected_flavour(cli, monkeypatch, capsys, caplog):
    posted, watches = cli
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("CHANNEL_WEBHOOK_URL", TEAMS_HOOK)
    assert channel.main(["--post", "--date", D30, "--watches", watches]) == 0
    (payload, url), = posted
    assert url == TEAMS_HOOK and payload["type"] == "message"
    out = capsys.readouterr()
    for leak in ("sEcReT", "logic.azure.com"):
        assert leak not in out.out + out.err + caplog.text


def test_post_to_an_unknown_address_stops_without_naming_it(cli, monkeypatch, capsys, caplog):
    posted, watches = cli
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("CHANNEL_WEBHOOK_URL", "https://example.invalid/sEcReTpath")
    assert channel.main(["--post", "--date", D30, "--watches", watches]) == 2
    out = capsys.readouterr()
    assert posted == [] and "sEcReT" not in out.out + out.err + caplog.text


def test_a_failed_post_reports_the_status_only(cli, monkeypatch, capsys, caplog):
    _, watches = cli
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("CHANNEL_WEBHOOK_URL", HOOK)

    def fail(payload, url, http=None):
        raise channel.ChannelError("Channel post failed: HTTP 404")
    monkeypatch.setattr(channel, "post", fail)
    assert channel.main(["--post", "--date", D30, "--watches", watches]) == 1
    out = capsys.readouterr()
    assert "Channel post failed: HTTP 404" in out.err
    assert "sEcReT" not in out.out + out.err + caplog.text


def test_scheduled_answers_can_be_passed_in_as_a_file(cli, tmp_path, capsys):
    _, watches = cli
    path = tmp_path / "scheduled.json"
    path.write_text(json.dumps(scheduled()), encoding="utf-8")
    assert channel.main(["--date", D30, "--watches", watches, "--scheduled", str(path)]) == 0
    out = capsys.readouterr().out
    assert "Answered" in out and QUESTION not in out and SHORT_ANSWER not in out


def test_without_watches_the_current_watch_list_is_read(cli, capsys):
    assert channel.main(["--date", D30]) == 0
    assert "South Africa: Published" in capsys.readouterr().out


def test_post_refuses_any_at_sign_anywhere_in_the_message():
    sent = []

    class Http:
        Request = staticmethod(lambda *a, **k: sent.append(a))

        @staticmethod
        def urlopen(*a, **k):
            raise AssertionError("must not post")

    payload = {"text": "42", "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "3 creators, @x"}}]}
    with pytest.raises(channel.ChannelError) as exc:
        channel.post(payload, "https://hooks.slack.com/services/T/B/secret", http=Http)
    assert "secret" not in str(exc.value) and "@" in str(exc.value) and sent == []
