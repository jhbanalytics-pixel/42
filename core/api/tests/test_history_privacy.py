"""Privacy comments review 2, finding 4: History findings judge every saved post through evidence_gone, the comment rule
included, so a comment under a hidden creator's post, and a renamed hidden creator's own post, are withheld. The saved
envelope holds no creator id and no link, so the judgement is the lookup and the address rule, as for a stored Ask."""
import json

from core.api import history, privacy
from core.api.tests.test_history_saved_envelope import FindingStore, _saved_row

OLD_HANDLE_URL = "https://www.tiktok.com/@hid_old_handle/video/1"
COMMENT_WORDS = "words said under the renamed creators post"
RENAMED_POST = "obs1_" + "c" * 32
RENAMED_WORDS = "the renamed creator's own words"


def comment():
    return {"id": "tiktok_comment_77", "platform": "tiktok", "handle": "fan_one", "url": OLD_HANDLE_URL,
            "posted_at": "2026-09-23T11:00:00+02:00", "market": "ZA", "text": COMMENT_WORDS, "engagement": {},
            "flags": ["market_assumed"]}


def renamed_post():
    return {"id": RENAMED_POST, "platform": "tiktok", "handle": "hid_old_handle", "url": OLD_HANDLE_URL,
            "posted_at": "2026-09-23T10:00:00+02:00", "market": "ZA", "text": RENAMED_WORDS, "engagement": {}, "flags": []}


def saved(*extra):
    row, envelope = _saved_row()
    envelope["evidence"].extend(extra)
    row["claims"][1]["evidence_post_ids"] = ["p02", *[e["id"] for e in extra]]
    row["answer"] = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return row


class Store(FindingStore):
    """People hidden: c_ren, now called hid_new_handle. The posts lookup places the renamed creator's own post."""

    def __init__(self, row, hidden=True, fail=False):
        super().__init__(row)
        self.hidden, self.fail = hidden, fail

    def hidden_people_rows(self):
        if self.fail:
            raise RuntimeError("view unreadable")
        return {"ids": {"c_ren"} if self.hidden else set(), "people": [("tiktok", "hid_new_handle")] if self.hidden else []}

    def suppressed_creators(self):
        return {"c_ren"} if self.hidden else set()

    def creators_by_id(self, ids):
        return [{"creator_id": "c_ren", "platform": "tiktok", "handle": "hid_new_handle"}]

    def map_items(self, ids):
        return []

    def post_creators(self, post_ids, since, until):
        return [{"post_id": p, "creator_id": "c_ren"} for p in post_ids if p == RENAMED_POST]


def read(store):
    finding = history.build_history_findings(store)["findings"][0]
    return finding, json.dumps(finding, ensure_ascii=False)


def test_a_comment_under_a_renamed_hidden_creators_post_is_withheld_with_its_claim_i4():
    finding, text = read(Store(saved(comment())))
    assert "tiktok_comment_77" not in [e["id"] for e in finding["evidence"]]
    assert COMMENT_WORDS not in text and "hid_old_handle" not in text
    assert finding["claims"][1]["text"] == privacy.CLAIM_WITHHELD and finding["claims"][1]["evidence_post_ids"] == []
    assert finding["claims"][0]["text"] == "The first checked claim."  # a claim that rests on visible posts stays
    assert finding["answer"] == privacy.SUMMARY_WITHHELD


def test_a_renamed_hidden_creators_own_post_is_withheld_by_the_lookup_i4():
    finding, text = read(Store(saved(renamed_post())))
    assert RENAMED_POST not in text and RENAMED_WORDS not in text and "hid_old_handle" not in text
    assert finding["claims"][1]["text"] == privacy.CLAIM_WITHHELD


def test_nobody_hidden_leaves_the_finding_as_saved_i4():
    finding, text = read(Store(saved(comment(), renamed_post()), hidden=False))
    assert {"tiktok_comment_77", RENAMED_POST} <= {e["id"] for e in finding["evidence"]}
    assert COMMENT_WORDS in text and privacy.CLAIM_WITHHELD not in text
    assert finding["answer"] == "The first checked claim.\nThe second checked claim."


def test_an_unreadable_list_keeps_the_posts_but_names_no_author_i4():
    finding, text = read(Store(saved(comment()), fail=True))
    assert finding["evidence"] and all("handle" not in e and "url" not in e for e in finding["evidence"])


def test_a_post_lookup_that_fails_leaves_out_every_saved_post_and_claim_i4():
    class Failing(Store):
        def post_creators(self, post_ids, since, until):
            raise RuntimeError("posts unreadable")

    finding, text = read(Failing(saved(renamed_post())))
    assert finding["evidence"] == [] and "First source text." not in text
    assert [c["text"] for c in finding["claims"]] == [privacy.CLAIM_WITHHELD] * 2


def test_the_stored_envelope_is_not_changed_by_a_read_i4():
    row = saved(comment())
    store = Store(row)
    read(store)
    assert store.row == row
