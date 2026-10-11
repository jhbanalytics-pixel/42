"""Release C review 2 findings I3 and m2.

I3: History asks who wrote the stored posts of all its findings in one lookup, not one per finding.
m2: History and dossiers mask the handle a hidden creator had when the run read them, as the Ask read does, in the
words a finding or a dossier keeps beside the posts."""
import json

from core.api import history, privacy
from core.api.tests import test_history_privacy as base
from core.api.tests import test_privacy_ask_routes as routes
from core.api.tests.test_privacy_projection import P_REN1, P_VIS1, ev

OLD = "hid_old_handle"
COUNT = 25


def post_id(n):
    return "obs1_" + f"{n:032x}"


def stored_post(n, handle="vis_handle"):
    return {"id": post_id(n), "platform": "tiktok", "handle": handle, "url": f"https://www.tiktok.com/@{handle}/video/{n}",
            "posted_at": "2026-09-23T10:00:00+02:00", "market": "ZA", "text": f"words of post {n}", "engagement": {},
            "flags": []}


class ManyStore(base.Store):
    """COUNT findings, each with a stored post of its own. Even posts are placed with a visible creator; odd ones are
    not found by the lookup at all. Finding 0 also holds the renamed hidden creator's own post."""

    def __init__(self, count=COUNT, question=None, fail_lookup=False):
        super().__init__(base.saved(), hidden=True)
        self.count, self.question, self.fail_lookup = count, question, fail_lookup
        self.lookups = []

    def findings(self, item_id=None):
        rows = []
        for n in range(self.count):
            extra = [stored_post(n)] + ([base.renamed_post()] if n == 0 else [])
            row = base.saved(*extra)
            row["finding_id"] = f"f_{n}"
            if n == 0 and self.question:
                row["question"] = self.question
            rows.append(row)
        return rows

    def post_creators(self, post_ids, since, until):
        self.lookups.append(list(post_ids))
        if self.fail_lookup:
            raise RuntimeError("posts unreadable")
        out = [{"post_id": p, "creator_id": "c_ren"} for p in post_ids if p == base.RENAMED_POST]
        out += [{"post_id": p, "creator_id": "c_vis"} for p in post_ids
                if p != base.RENAMED_POST and int(p[5:], 16) % 2 == 0]
        return out


def test_history_asks_who_wrote_the_posts_of_twenty_five_findings_in_one_lookup_i3():
    store = ManyStore()
    out = history.build_history_findings(store)["findings"]
    assert len(out) == COUNT
    assert len(store.lookups) == 1
    assert sorted(store.lookups[0]) == sorted({post_id(n) for n in range(COUNT)} | {base.RENAMED_POST})


def test_history_still_judges_each_finding_by_its_own_posts_after_the_shared_lookup_i3():
    out = history.build_history_findings(ManyStore())["findings"]
    shown = {n: post_id(n) in [e["id"] for e in out[n]["evidence"]] for n in range(COUNT)}
    assert {n for n, here in shown.items() if here} == {n for n in range(COUNT) if n % 2 == 0}  # a miss is gone
    assert base.RENAMED_POST not in json.dumps(out)  # the hidden creator's post is gone from finding 0


def test_a_lookup_that_fails_is_one_failure_not_one_per_finding_i3():
    store = ManyStore(fail_lookup=True)
    out = history.build_history_findings(store)["findings"]
    assert len(store.lookups) == 1
    assert all(post_id(n) not in json.dumps(out[n]) for n in range(COUNT))  # no post shows when none can be placed


def test_nobody_hidden_costs_no_lookup_i3():
    store = ManyStore()
    store.hidden = False
    history.build_history_findings(store)
    assert store.lookups == []


def test_a_finding_question_loses_the_old_handle_of_a_creator_whose_post_is_withheld_m2():
    store = ManyStore(question=f"Why is @{OLD} trending in Durban")
    first = history.build_history_findings(store)["findings"][0]
    assert OLD not in json.dumps(first)
    assert "Durban" in first["question"]


def test_a_dossier_masks_the_old_handle_in_its_question_and_a_gaps_searched_text_m2():
    store = routes.RouteStore(hide={"c_ren"})
    view = {"dossier_id": "d_1", "version": 1, "title": "Durban amapiano", "question": f"Why is @{OLD} trending in Durban",
            "summary": "Posts rose.", "content_hash": "sha256:" + "0" * 64,
            "evidence": [ev(P_REN1, OLD, "renamed creator words", "2026-10-05T01:00:00+02:00"),
                         ev(P_VIS1, "vis_handle", "visible fixture words")],
            "claims": [{"claim_id": "c1", "text": "Posts rose", "label": "observed", "kind": "observation",
                        "evidence_ids": [P_VIS1], "quotes": [], "kept": True}],
            "gaps": [{"what": "No posts", "searched": f"posts by @{OLD} in Durban", "why": "empty"}]}
    out = privacy.project_dossier(view, store)
    assert OLD not in json.dumps(out)
    assert out["content_hash"] == view["content_hash"]
    assert [e["id"] for e in out["evidence"]] == [P_VIS1]


def test_a_dossier_with_nobody_hidden_keeps_its_words_m2():
    store = routes.RouteStore(hide=set())
    view = {"dossier_id": "d_1", "version": 1, "question": f"Why is @{OLD} trending", "evidence": [], "claims": []}
    assert privacy.project_dossier(view, store)["question"] == view["question"]
