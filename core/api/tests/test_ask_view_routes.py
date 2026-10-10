"""Every route that shows a stored Ask answer shows the reader's view of it (core/agent/answer_view.py)."""
import json
import re
from pathlib import Path

from core.api import agent_app
from core.api.export import render_answer_html

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def stored():
    return json.loads((FIXTURES / "ask_comments_repeat.json").read_text(encoding="utf-8"))


def test_the_agent_reads_a_stored_answer_as_the_reader_view():
    shown = agent_app.readable(stored(), (set(), set(), {}))
    assert [c["id"] for c in shown["answer"]["claims"]] == ["c2", "c5"]
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
