"""Old readers when the previous release already stores answer_meta (Release C on top of Release A).

Layers 2 and 4 of old_reader_harness were written for a previous release that stores no typed state (a80be1d): the clone of a record that
carries answer_meta was judged against its key-absent twin, and a previous writer's record was expected to read as no state. Release A
stores answer_meta itself, so for such a previous release the previous readers are run on the candidate's real records as the candidate
writes them, with answer_meta present, and judged against the previous release's own records of the same kind read by the same readers;
and a previous writer's record must read as the state it stored. Whether the previous release stores the key is derived from its writers.
The a80 rules are unchanged and are tested in test_old_readers.py."""
import copy

import pytest

from core.api.tests import compat_harness as ch
from core.api.tests import old_reader_harness as h

A = "3710c5e"


def row(ask_id, kind="complete", meta=True, execution="completed", summary="shown"):
    status = {"complete": "complete", "partial": "complete", "failed": "failed", "stopped": "stopped"}[kind]
    record = {"ask_id": ask_id, "status": status, "answer": {"status": "partial" if kind == "partial" else "ok", "short_answer": "x", "claims": []},
              "run": {"run_id": ask_id}}
    if meta:
        record["answer_meta"] = {"v": 1, "ask_id": ask_id, "execution": {"state": execution, "stop_reason": None},
                                 "summary": {"state": summary, "removals": [], "rewrite": "not_attempted"}}
    return {"run_id": ask_id, "record": record}


def read(shape, status=200):
    return {"label": "x", "method": "GET", "path": "/x", "status": status, "content_type": "application/json", "kind": "json", "shape": ch.shape(shape)}


def reads_for(ids, body, status=200):
    out = {}
    for ask_id in ids:
        out[ask_id] = {"ask read": read(body, status), "ask events": {"status": 200, "kind": "sse", "events": ["done"]},
                       "ask export": {"status": 200, "kind": "text", "content_type": "text/html"}}
    out["today"] = {"today": read({"markets": []})}
    return out


# whether the previous release stores the typed state is derived from what its writers wrote

def test_a_previous_release_stores_answer_meta_when_every_record_its_writers_wrote_carries_it():
    assert h.stores_answer_meta([row("a_1"), row("a_2", "failed")]) is True


@pytest.mark.parametrize("rows", [[], [row("a_1", meta=False)], [row("a_1"), row("a_2", meta=False)]])
def test_a_previous_release_that_writes_no_records_or_some_without_the_key_does_not_store_it(rows):
    assert h.stores_answer_meta(rows) is False


def test_it_is_read_from_the_writers_of_the_previous_commit_a_80_does_not_and_a_does(tmp_path):
    for commit, expected in (("a80be1d", False), (A, True)):
        assert h.stores_answer_meta(h.written_rows(ch.archive(commit, tmp_path / commit))) is expected, commit


# layer 2 for such a release: its readers on the candidate's real records, against its own records of the same kind

def meta_data(twin_body, candidate_body, *, candidate_status=200, ids=("a_c1",), legacy=("a_l1",)):
    reads = reads_for(legacy, twin_body)
    reads.update({k: v for k, v in reads_for(ids, candidate_body, candidate_status).items() if k != "today"})
    return reads, {i: legacy[0] for i in ids}


def test_a_candidate_record_that_reads_like_the_previous_release_own_record_has_no_problem():
    body = {"answer_meta": {"v": 1, "execution": {"state": "completed"}}, "ask_id": "a"}
    reads, twin_of = meta_data(body, body)
    assert h.layer2_problems(reads, twin_of, declared=()) == []


def test_a_key_the_previous_release_own_record_does_not_read_with_is_undeclared_even_if_it_is_answer_meta_itself():
    reads, twin_of = meta_data({"ask_id": "a"}, {"ask_id": "a", "answer_meta": {"v": 1}})
    problems = h.layer2_problems(reads, twin_of, declared=())
    assert problems and all("undeclared key" in p for p in problems)


def test_a_key_the_previous_reader_stops_giving_and_a_changed_status_are_problems():
    reads, twin_of = meta_data({"ask_id": "a", "answer_meta": {"v": 1}}, {"ask_id": "a"})
    assert any("answer_meta" in p for p in h.layer2_problems(reads, twin_of, declared=()))
    reads, twin_of = meta_data({"ask_id": "a"}, {"ask_id": "a"}, candidate_status=500)
    assert any("status" in p for p in h.layer2_problems(reads, twin_of, declared=()))


def test_the_layers_use_the_record_rules_only_when_the_previous_release_stores_the_key():
    twin, legacy, real = row("a_t1", meta=False), row("a_t1"), row("a_c1")
    reads = {**reads_for(["a_t1"], {"ask_id": "a"}), "a_c1": reads_for(["a_c1"], {"ask_id": "a", "answer_meta": {"v": 1}})["a_c1"]}
    stored = {"rows": [real, twin], "legacy": [twin], "twin_of": {"a_c1": "a_t1"}, "paths": set(), "reads": reads, "reverse": {}}
    assert h.layer_problems(stored)["layer 2"] == [], "a80: answer_meta is the one declared new key"
    flagged = h.layer_problems({**stored, "prior_stores_answer_meta": True, "layer2_rows": [real, legacy], "legacy": [legacy]})["layer 2"]
    assert flagged and all("undeclared key" in p for p in flagged)


# layer 4 for such a release: a previous writer's record reads as the state it stored

def stored_meta(**over):
    meta = {"v": 1, "ask_id": "a_l1", "execution": {"state": "completed", "stop_reason": None},
            "summary": {"state": "shown", "removals": [], "rewrite": "not_attempted"}}
    meta.update(over)
    return meta


def as_read(meta, check="verified"):
    return {"check": check, **{k: copy.deepcopy(meta[k]) for k in ("v", "execution", "summary")}}


def reverse_of(got):
    return {"a_l1": {"ask read": {"status": 200, "kind": "json", "shape": {}, "meta": got}}}


def test_a_stored_state_read_back_as_stored_is_no_problem():
    legacy = [row("a_l1")]
    assert h.reverse_meta_problems(reverse_of(as_read(legacy[0]["record"]["answer_meta"])), legacy) == []


@pytest.mark.parametrize("label,got", [
    ("a different execution state", lambda m: {**as_read(m), "execution": {"state": "failed", "stop_reason": None}}),
    ("a different summary state", lambda m: {**as_read(m), "summary": {"state": "removed", "removals": [], "rewrite": "not_attempted"}}),
    ("a stop reason invented", lambda m: {**as_read(m), "execution": {"state": "completed", "stop_reason": "budget_full"}}),
    ("a record not checked as verified", lambda m: as_read(m, check="legacy_unknown")),
    ("no state", lambda m: "absent"),
    ("a null state", lambda m: "null"),
])
def test_a_stored_state_read_back_as_anything_else_is_a_problem(label, got):
    legacy = [row("a_l1")]
    problems = h.reverse_meta_problems(reverse_of(got(legacy[0]["record"]["answer_meta"])), legacy)
    assert problems and "a_l1" in problems[0], label


def test_a_previous_record_nobody_read_is_a_problem_not_a_pass():
    legacy = row("a_l1")
    data = {"rows": [legacy], "legacy": [legacy], "twin_of": {}, "paths": {"answer.status"}, "reads": reads_for(["a_l1"], {"ask_id": "a"}),
            "reverse": {}, "prior_stores_answer_meta": True}
    assert any("no read of the candidate readers" in p for p in h.layer_problems(data)["layer 4"])


def layer4_data(got):
    legacy = row("a_l1")
    return {"rows": [legacy], "legacy": [legacy], "twin_of": {}, "paths": {"answer.status"}, "reads": reads_for(["a_l1"], {"ask_id": "a"}),
            "reverse": reverse_of(got), "prior_stores_answer_meta": True}


def test_layer_4_of_a_release_that_stores_the_state_passes_a_faithful_read_and_flags_another_state():
    stored = row("a_l1")["record"]["answer_meta"]
    assert h.layer_problems(layer4_data(as_read(stored)))["layer 4"] == []
    other = {**as_read(stored), "execution": {"state": "failed", "stop_reason": None}}
    assert any("a_l1" in p for p in h.layer_problems(layer4_data(other))["layer 4"])
    assert any("a_l1" in p for p in h.layer_problems(layer4_data({"check": "legacy_unknown"}))["layer 4"])


# the receipt records what the harness derived, and the verdict follows the rules above

def test_the_receipt_names_whether_the_previous_release_stores_the_key(monkeypatch):
    monkeypatch.setattr(h, "layer_problems", lambda data: {})
    base = {"rows": [], "legacy": [], "paths": []}
    assert h.build_receipt("c" * 40, base)["prior_stores_answer_meta"] is False
    assert h.build_receipt("c" * 40, {**base, "prior_stores_answer_meta": True})["prior_stores_answer_meta"] is True


# the evidence: Release C's tree against the real readers and writers of Release A

@pytest.fixture(scope="module")
def at_a(tmp_path_factory):
    return h.collect(ch.ROOT, tmp_path_factory.mktemp("old-readers-at-a"), base_commit=A)


def test_the_harness_derives_that_release_a_stores_the_key_and_reads_its_real_records_with_it_present(at_a):
    assert at_a["prior_stores_answer_meta"] is True
    real = [r for r in at_a["layer2_rows"] if "answer_meta" in r["record"]]
    assert len(real) == len(at_a["layer2_rows"]) > 4
    assert all(h.ask_id_of(r) in at_a["reads"] for r in at_a["layer2_rows"])


def test_the_receipt_at_release_a_says_pass_with_no_problem(at_a):
    receipt = h.build_receipt(ch.git_head(), at_a, base_commit=A)
    assert receipt["problems"] == [] and receipt["verdict"] == "pass"
    assert receipt["base_commit"] == A and receipt["prior_stores_answer_meta"] is True
    assert receipt["legacy_rows"] == 4 and receipt["json_paths"] > 0 and receipt["corpus_rows"] > 4


def test_a_candidate_record_that_changes_the_shape_release_a_reads_is_found(at_a):
    mutated = copy.deepcopy(at_a)
    victim = next(ask_id for ask_id, kind in mutated["twin_of"].items())
    for route, body in mutated["reads"][victim].items():
        if body.get("kind") == "json":
            body["shape"] = {**body["shape"], "surprise": "string"} if isinstance(body["shape"], dict) else body["shape"]
    problems = h.layer_problems(mutated)["layer 2"]
    assert any("surprise" in p and "undeclared key" in p for p in problems)


def test_a_previous_record_read_by_the_candidate_as_another_state_is_found(at_a):
    mutated = copy.deepcopy(at_a)
    ask_id = h.ask_id_of(mutated["legacy"][0])
    mutated["reverse"][ask_id]["ask read"]["meta"] = {"check": "verified", "v": 1, "execution": {"state": "failed", "stop_reason": None},
                                                       "summary": {"state": "no_answer", "removals": [], "rewrite": "not_attempted"}}
    assert any(ask_id in p for p in h.layer_problems(mutated)["layer 4"])
