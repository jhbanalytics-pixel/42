"""The export names no hidden person by an old handle (Release C review I3).

The privacy readers decide who is hidden and drop what rests on that person. The export has no hide list of its own, so
the texts it adds must not carry a handle that no reader could have judged: the parent of a comment is named only when
the post itself is among the sources shown, and the stored step timeline (free text the model and the tools wrote,
which can repeat a handle the readers know only by its old name) is not printed at all. Every leak string below is a
literal written here, never read back from the record it judges."""
import pytest

from core.api import export
from core.api.tests.test_privacy_ask_routes import ASK, GOOD, api, remote_agent  # noqa: F401  (fixtures)
from core.api.tests.test_privacy_comments import C_REN, OLD_URL, WORDS, comment, only_comment_claim, stored_record
from core.api.tests.test_privacy_projection import P_REN1, P_VIS1, ask_record

OLD_STEP = "Searching TikTok for posts by @hid_old_handle"
VIS_URL = "https://www.tiktok.com/@vis_handle/video/1"


@pytest.mark.parametrize("linked", [True, False])
def test_a_stored_step_that_names_the_old_handle_of_a_hidden_creator_is_not_in_the_export(api, linked):  # noqa: F811
    record, remark = stored_record(linked)
    record["steps"] = [{"seq": 1, "text": OLD_STEP}, {"seq": 2, "text": "Reading 3 posts"}]
    api.store.hide = {"c_ren"}
    api.store.records[ASK] = record
    page = api.serve(remote_agent({})).get(f"/api/ask/{ASK}/export", headers=GOOD)
    assert page.status_code == 200
    for s in ("hid_old_handle", OLD_STEP, "Searching TikTok for posts by", WORDS, remark["id"]):
        assert s not in page.text


def test_the_export_has_no_step_timeline_whatever_the_stored_steps_say():
    record = ask_record()
    record["steps"] = [{"seq": 1, "text": OLD_STEP}, {"seq": 2, "text": "Reading 3 posts"}]
    page = export.render_answer_html(record)
    assert "How this was researched" not in page and "Reading 3 posts" not in page and OLD_STEP not in page


def unlisted_parent_record():
    """A comment kept in the record whose post is not among the sources, the address still holding the old handle."""
    record = ask_record()
    record["answer"]["evidence"] = [e for e in record["answer"]["evidence"] if e["id"] != P_REN1]
    record["answer"]["claims"] = [c for c in record["answer"]["claims"] if c["id"] != "c4"]
    record["answer"]["watch_next"] = []
    record["answer"]["evidence"].append(comment(C_REN, None, url=OLD_URL))
    record["answer"]["claims"].append(only_comment_claim(C_REN))
    return record


def test_a_comment_whose_post_is_not_among_the_sources_is_named_for_a_post_and_links_nowhere():
    page = export.render_answer_html(unlisted_parent_record())
    assert "hid_old_handle" not in page
    assert "Comment on a post (TikTok)" in page and "On a post" in page
    assert WORDS in page  # the comment is still shown, only its post is not named


def test_a_comment_under_a_post_among_the_sources_is_named_for_that_posts_handle_and_links_to_it():
    record = ask_record()
    record["answer"]["evidence"].append(comment("tiktok_comment_903", P_VIS1, url=VIS_URL))
    record["answer"]["claims"].append(only_comment_claim("tiktok_comment_903"))
    page = export.render_answer_html(record)
    assert "Comment on @vis_handle&#x27;s post (TikTok)" in page
    assert f'<a href="{VIS_URL}">@vis_handle&#x27;s post</a>' in page
    assert f'<a href="{VIS_URL}">{VIS_URL}</a>' in page
