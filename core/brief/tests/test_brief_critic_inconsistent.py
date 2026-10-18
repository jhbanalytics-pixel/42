"""A critic answer that contradicts itself (18 Oct 2026).

An answer core/brief/quality.py critic_is_valid rejects, such as ruled_out true with a stray news_driven flag, was
stored as "not ruled out": the card was held for a simpler explanation the critic never stood by. It now has its own
standing, "critic answer inconsistent", its own wording and its own retained reason code, and it is still a cut.
Nothing about what passes changes.
"""

import pytest

from core.api import today
from core.brief import explain, job
from core.brief.quality import critic_is_valid
from core.brief.tests.test_brief_explain import RULED_OUT, good
from core.brief.tests.test_brief_second_draft import Critics, critic_rows, run
from core.trust import retained

STRAY_NEWS = dict(RULED_OUT, news_driven=True)
STRAY_REACTION = dict(RULED_OUT, local_reaction=True)
NEWS_AND_EVENT = dict(RULED_OUT, ruled_out=False, news_driven=True, scheduled_event=True)
MISSING_FLAG = {k: v for k, v in RULED_OUT.items() if k != "scheduled_event"}
INVALID = [STRAY_NEWS, STRAY_REACTION, NEWS_AND_EVENT, MISSING_FLAG]
NOT_RULED_OUT = dict(RULED_OUT, ruled_out=False, local_why_now=False)


@pytest.mark.parametrize("out", INVALID)
def test_an_invalid_answer_has_its_own_standing_and_is_a_cut(out):
    assert not critic_is_valid(out)
    row = explain._critic_row(out, 2)
    assert row["standing"] == explain.INCONSISTENT == "critic answer inconsistent"
    assert row["verdict"] == "cut"
    assert "; critic answer inconsistent: " in row["detail"] and "not ruled out" not in row["detail"]
    assert job.check_reason(row) == job.CRITIC_INCONSISTENT
    assert job.retained_columns(row)["reason_code"] == "critic_answer_inconsistent"


def test_a_valid_answer_keeps_its_standing():
    assert explain._critic_row(NOT_RULED_OUT)["standing"] == "not ruled out"
    assert explain._critic_row(RULED_OUT)["standing"] == "ruled out"
    assert explain._critic_row(RULED_OUT)["verdict"] == "pass"


def test_the_standing_is_read_back_from_a_row_without_keys():
    row = {k: v for k, v in explain._critic_row(STRAY_NEWS).items() if k not in ("standing", "local_why_now")}
    assert job._critic_parts(row) == (explain.INCONSISTENT, True)
    assert job.check_reason(row) == job.CRITIC_INCONSISTENT


def test_the_code_is_in_the_retained_enum_and_the_wording_is_the_one_today_reads():
    assert "critic_answer_inconsistent" in retained.REASON_CODES
    assert today.CRITIC_INCONSISTENT == job.CRITIC_INCONSISTENT
    assert not today._is_critic_hold({"rule": "critic", "reason": job.CRITIC_INCONSISTENT})


def test_today_names_the_inconsistent_answer_as_the_hold():
    rows = [{"claim_id": None, "rule": "critic", "verdict": "cut", "reason": job.CRITIC_INCONSISTENT}]
    assert today._held_detail(rows) == "The critic's answer was inconsistent."


def test_an_inconsistent_answer_cuts_the_explanation_without_a_second_draft():
    model = Critics([good(), good()], [STRAY_NEWS, RULED_OUT])
    result = run(model)
    assert result["reason"] == "failed_checks" and result["numbers_only"] is True and result["explanation"] is None
    assert len(model.writer_calls()) == 1 and len(model.critic_calls()) == 1
    [row] = critic_rows(result)
    assert row["verdict"] == "cut" and row["standing"] == explain.INCONSISTENT
    assert job.failed_reason(result) == job.CRITIC_INCONSISTENT
