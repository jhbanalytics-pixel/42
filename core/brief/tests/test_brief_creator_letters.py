"""Each pack post carries a code letter for its author, never its creator id (18 Oct 2026).

The pack row has the post's creator_id; the record keeps it as a model-only field and explain.py writes a letter per
author (creator A, creator B) the way it writes sound letters, so the writer, the support check and the critic see
which posts one author made even when a handle is missing, and the counted pattern line counts such a creator. The
raw id reaches no prompt and no stored card.
"""

import copy

from core.brief import explain
from core.brief.payload import MODEL_ONLY_FIELDS, reader_evidence
from core.brief.tests.test_brief_explain import CANDIDATE, W_END, W_START, heads, make_pack, outside_fences
from core.brief.tests.test_brief_explain_local import writer_heads

LEDGER = [{"id": "c1", "text": "Two creators post.", "label": "observed", "kind": "observation",
           "evidence_ids": ["tt_1", "ig_2", "tt_3"]}]
RAW = "7301234567890123"


def id_pack():
    pack = make_pack()
    pack["evidence"][0]["creator_id"] = RAW  # the handle names the author; the id is the same author's
    pack["evidence"][1]["creator_id"] = "ig_author_1"
    pack["evidence"][2].update({"handle": None, "creator_id": "no_handle_1"})  # the id alone names the author
    return pack


def test_letters_follow_pack_order_and_the_same_author_shares_one_letter():
    pack = id_pack()
    pack["evidence"].append({**copy.deepcopy(pack["evidence"][2]), "id": "tt_4"})  # the unnamed author again
    pack["evidence"].append({**copy.deepcopy(pack["evidence"][0]), "id": "tt_5", "handle": None})  # tt_1's author by id
    pack["evidence"].append({**copy.deepcopy(pack["evidence"][0]), "id": "tt_6", "handle": None, "creator_id": None})
    letters = explain._creator_letters(pack)
    marks = [letters.get(explain._author(r)) for r in pack["evidence"]]
    assert marks == ["creator A", "creator B", "creator C", "creator C", "creator A", None]
    # A record with no id (an older card, a fixture) is keyed by its handle, so two such posts by one handle share a
    # letter; the pack's own rows always carry the id the creator cap used.
    pack["evidence"][5]["handle"] = "@someone_else"
    pack["evidence"].append({**copy.deepcopy(pack["evidence"][5]), "id": "tt_7"})
    marks = [letters.get(explain._author(r)) for r in pack["evidence"] for letters in [explain._creator_letters(pack)]]
    assert marks[5:] == ["creator D", "creator D"]


def test_the_writer_the_support_check_and_the_critic_see_the_letter_and_never_the_id():
    pack = id_pack()
    writer = explain._writer_user(CANDIDATE, pack, "ZA", W_START, W_END)
    assert [h["creator"] for h in writer_heads(writer)] == ["creator A", "creator B", "creator C"]
    support = explain._support_user(LEDGER[0], pack["evidence"], pack, "ZA")
    assert [h["creator"] for h in heads(support)] == ["creator A", "creator B", "creator C"]
    critic = explain._critic_user(CANDIDATE, "ZA", "A sentence.", LEDGER, pack, ("c1",))
    assert [h["creator"] for h in heads(critic)] == ["creator A", "creator B", "creator C"]
    for prompt in (writer, support, critic):
        assert RAW not in prompt and "no_handle_1" not in prompt and "ig_author_1" not in prompt
    assert "name an author by handle, never by a creator letter" in explain.WRITER_SYSTEM
    assert "name an author by handle, never by a creator letter" in outside_fences(writer)
    assert "creator is a code letter in the same way" in explain.SUPPORT_SYSTEM
    assert "creator is a\ncode letter in the same way" in explain.CRITIC_SYSTEM


def test_a_post_with_no_id_and_no_handle_carries_null():
    pack = make_pack()
    pack["evidence"][2]["handle"] = None
    assert [h["creator"] for h in writer_heads(explain._writer_user(CANDIDATE, pack, "ZA", W_START, W_END))] == [
        "creator A", "creator B", None]


def test_the_pattern_line_counts_an_unnamed_creator_by_its_id():
    pack = id_pack()
    for r in pack["evidence"]:
        r["formats"] = ["dance"]
    pack["evidence"][1]["handle"] = None  # two unnamed authors with different ids, one named
    [shared] = explain._shared_patterns(pack, explain._sound_letters(pack))
    assert (shared["pattern"], shared["post_ids"], shared["creators"]) == ("format dance", ["tt_1", "ig_2", "tt_3"], 3)
    # The same id twice on one platform is one creator; with no id and no handle the post adds none.
    pack["evidence"][1].update({"creator_id": "no_handle_1", "platform": "tiktok"})
    assert explain._shared_patterns(pack, explain._sound_letters(pack))[0]["creators"] == 2
    pack["evidence"][1]["creator_id"] = None
    assert explain._shared_patterns(pack, explain._sound_letters(pack))[0]["creators"] == 2
    pack["evidence"][2]["creator_id"] = None
    assert explain._shared_patterns(pack, explain._sound_letters(pack)) == []


def test_the_stored_card_never_carries_the_id():
    assert "creator_id" in MODEL_ONLY_FIELDS
    assert not any("creator_id" in e for e in reader_evidence(id_pack()["evidence"]))
