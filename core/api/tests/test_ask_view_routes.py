"""Every route that shows a stored Ask answer shows the reader's view of it (core/agent/answer_view.py)."""
import json
import re
from pathlib import Path

from core.api import agent_app
from core.api.export import render_answer_html

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def stored():
    """The fixture with c1 and c7 saying c2 again (same quoted posts and words, same figures and queries). c3, c4 and
    c6 are three creators' own posts and are not repeats."""
    record = json.loads((FIXTURES / "ask_comments_repeat.json").read_text(encoding="utf-8"))
    claims = {c["id"]: c for c in record["answer"]["claims"]}
    claims["c1"]["numbers"] = json.loads(json.dumps(claims["c2"]["numbers"]))
    claims["c7"]["quotes"] = json.loads(json.dumps(claims["c2"]["quotes"]))
    claims["c7"]["numbers"] = json.loads(json.dumps(claims["c2"]["numbers"]))
    return record


def test_the_agent_reads_a_stored_answer_as_the_reader_view():
    shown = agent_app.readable(stored(), (set(), set(), {}))
    assert [c["id"] for c in shown["answer"]["claims"]] == ["c2", "c3", "c4", "c5", "c6"]
    assert "tiktok/post/transcript" not in json.dumps(shown["answer"]["gaps"])
    assert shown["run"]["technical_gaps"]


def test_the_stored_record_is_not_changed_by_a_read():
    record = stored()
    agent_app.readable(record, (set(), set(), {}))
    assert record == stored()


def test_the_export_of_a_raw_stored_answer_is_the_same_view():
    full = render_answer_html(stored())
    page = re.sub(r"<details>.*?</details>", "", full, flags=re.S)  # the technical details may hold the full text
    assert "tiktok/post/transcript" in full
    assert "tiktok/post/transcript" not in page
    assert "tiktok_comment_77020000000000001" not in page
    assert "Why: error" not in page
    assert page.count("Every post tagged with #funnyclip") == 1
    assert "Posts featured the hashtags #funnyclip, #skits, and #laughs together" not in page


# The API stored-record path (GET /api/ask/{id} when the agent no longer holds the Ask).
from core.api.tests.test_privacy_ask_routes import ASK, GOOD, api, remote_agent  # noqa: E402,F401


def repeated():
    record = stored()
    record["ask_id"] = ASK
    return record


def test_the_api_reads_a_stored_answer_the_agent_does_not_hold_as_the_reader_view_i1(api):
    api.store.records[ASK] = repeated()
    body = api.serve(remote_agent({})).get(f"/api/ask/{ASK}", headers=GOOD).json()
    assert [c["id"] for c in body["answer"]["claims"]] == ["c2", "c3", "c4", "c5", "c6"]
    assert "tiktok/post/transcript" not in json.dumps(body["answer"]["gaps"])
    assert any(g["searched"] == "tiktok/post/transcript" for g in body["run"]["technical_gaps"])


def test_the_api_stored_path_and_the_agent_path_show_the_same_answer_i1(api):
    api.store.records[ASK] = repeated()
    from_store = api.serve(remote_agent({})).get(f"/api/ask/{ASK}", headers=GOOD).json()
    held = agent_app.readable(repeated(), (set(), set(), {}))
    from_agent = api.serve(remote_agent({}, held=held)).get(f"/api/ask/{ASK}", headers=GOOD).json()
    assert from_store["answer"] == from_agent["answer"]
    assert from_store["run"]["technical_gaps"] == from_agent["run"]["technical_gaps"]
    assert from_store["run"]["followups"] == from_agent["run"]["followups"]


def test_the_stored_record_is_not_changed_by_the_api_read_i1(api):
    api.store.records[ASK] = repeated()
    api.serve(remote_agent({})).get(f"/api/ask/{ASK}", headers=GOOD)
    assert api.store.records[ASK] == repeated()
