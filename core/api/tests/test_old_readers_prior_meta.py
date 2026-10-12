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

# Release C's one stored shape, evidence_links (plan 3.4d, review of Release C finding I1). The fixture agent never cites a comment, so a
# corpus of its rows never put the key in front of Release A's readers. The rows below are made with the candidate's own producer
# function; the expected values are pinned here, not read back from the rows.

LINKED = {"v": 1, "parents": {"x_comment_7001": "x_fixture_3"}}
NAMES = ("linked", "partial", "hidden_post", "post_pruned", "link_dropped")


def ids_of(row):
    return [e["id"] for e in (row["record"].get("answer") or {}).get("evidence") or []]


@pytest.fixture(scope="module")
def candidate():
    written = h.written_rows(h.ROOT)
    return written, h.linked_rows(h.ROOT, written)


@pytest.fixture(scope="module")
def named(candidate):
    return dict(zip(NAMES, candidate[1]))


def test_the_scenarios_are_the_five_the_corpus_must_hold():
    assert h.LINK_SCENARIOS == NAMES


# the rows: the links come from the candidate's producer, and the expected values are pinned here, not read back from the rows

def test_a_comment_whose_post_is_shown_stores_the_link_under_the_key(named):
    row = named["linked"]
    assert row["record"]["evidence_links"] == LINKED
    assert {"x_comment_7001", "x_fixture_3"} <= set(ids_of(row))


def test_a_second_comment_with_no_stored_post_gets_no_link_and_the_first_keeps_its_own(named):
    row = named["partial"]
    assert row["record"]["evidence_links"] == LINKED
    assert "x_comment_7003" in ids_of(row) and "x_comment_7003" not in row["record"]["evidence_links"]["parents"]


def test_a_comment_shown_without_its_hidden_post_stores_no_key(named):
    row = named["hidden_post"]
    assert "evidence_links" not in row["record"]
    assert "x_comment_7001" in ids_of(row) and "x_fixture_3" not in ids_of(row)


def test_a_link_that_names_a_post_no_longer_shown_is_kept_as_stored(named):
    row = named["post_pruned"]
    assert row["record"]["evidence_links"] == LINKED
    assert "x_comment_7001" in ids_of(row) and "x_fixture_3" not in ids_of(row)


def test_a_record_whose_reader_dropped_the_link_holds_the_comment_and_its_post_and_no_key(named):
    row = named["link_dropped"]
    assert "evidence_links" not in row["record"]
    assert {"x_comment_7001", "x_fixture_3"} <= set(ids_of(row))


def test_every_link_row_is_a_complete_written_row_with_only_the_evidence_the_key_and_the_ids_changed(candidate):
    written, linked = candidate
    base = next(row for row in written if h.kind_of(row) == "complete")
    ask_ids = [h.ask_id_of(row) for row in linked]
    assert len(set(ask_ids)) == len(ask_ids) == len(NAMES) and not set(ask_ids) & {h.ask_id_of(row) for row in written}
    for row in linked:
        assert h.kind_of(row) == "complete" and row["run_id"] == h.ask_id_of(row)
        kept = {k: v for k, v in row["record"].items() if k not in ("ask_id", "answer", "answer_meta", "evidence_links")}
        assert kept == {k: v for k, v in base["record"].items() if k not in ("ask_id", "answer", "answer_meta")}
        assert row["answer"] == row["record"]["answer"]
        assert row["record"]["answer"]["evidence"][:len(base["record"]["answer"]["evidence"])] == base["record"]["answer"]["evidence"]
        meta = row["record"]["answer_meta"]
        again = h.make_meta(row["record"], meta["execution"]["state"], meta["execution"]["stop_reason"], meta["summary"]["state"],
                            meta["summary"]["removals"], meta["summary"]["rewrite"])
        assert meta == again and meta["ask_id"] == h.ask_id_of(row)


def test_the_comment_and_post_records_have_the_shape_of_the_evidence_the_answer_already_holds(candidate):
    written, linked = candidate
    base = next(row for row in written if h.kind_of(row) == "complete")
    shapes = {frozenset(e) for e in base["record"]["answer"]["evidence"]}
    for row in linked:
        assert {frozenset(e) for e in row["record"]["answer"]["evidence"]} <= shapes


def test_a_tree_with_no_producer_gives_no_link_rows(candidate, tmp_path):
    written, _ = candidate
    assert h.linked_rows(ch.archive(A, tmp_path / "a"), written) == []


# the corpus and layer 1

def test_the_corpus_keeps_the_link_rows_and_judges_each_against_the_key_absent_twin_of_a_complete_row(candidate):
    written, linked = candidate
    rows, twin_of = h.corpus(written, linked)
    by_id = {h.ask_id_of(row): row for row in rows}
    assert all(h.ask_id_of(row) in by_id for row in linked)
    for row in linked:
        twin = by_id[twin_of[h.ask_id_of(row)]]
        assert h.kind_of(twin) == "complete" and "answer_meta" not in twin["record"] and "evidence_links" not in twin["record"]
    assert len(h.corpus(written)[0]) + len(linked) == len(rows) and len({h.ask_id_of(row) for row in rows}) == len(rows)


def holds_links(row):
    return bool((row["record"].get("evidence_links") or {}).get("parents"))


def comment_without_key(row):
    return "evidence_links" not in row["record"] and any("_comment_" in i for i in ids_of(row))


def names_unshown_post(row):
    return any(post not in ids_of(row) for post in ((row["record"].get("evidence_links") or {}).get("parents") or {}).values())


def test_layer_1_asks_for_the_three_cases_when_the_candidate_has_the_producer(candidate):
    written, linked = candidate
    full, _ = h.corpus(written, linked)
    assert h.layer1_problems(full, expects_links=True) == []
    bare, _ = h.corpus(written)
    assert h.layer1_problems(bare) == [] and len(h.layer1_problems(bare, expects_links=True)) == 3
    for label, drop, count in (("holds evidence_links", holds_links, 2), ("without evidence_links", comment_without_key, 1),
                               ("names a post", names_unshown_post, 1)):  # a corpus with no link at all also names no unshown post
        problems = h.layer1_problems([row for row in full if not drop(row)], expects_links=True)
        assert len(problems) == count and label in problems[0], (label, problems)


# layer 2 with Release A's real readers (the at_a fixture above)

def link_ids(data):
    return [h.ask_id_of(row) for row in data["layer2_rows"] if str(h.ask_id_of(row)).startswith("a_20261009_link")]


def test_release_a_reads_every_link_row_on_every_layer_and_the_data_holds_them(at_a):
    ids = link_ids(at_a)
    assert len(ids) == len(NAMES) and at_a["candidate_writes_links"] is True
    assert {h.ask_id_of(row) for row in at_a["rows"]} >= set(ids)
    for ask_id in ids:
        assert [at_a["reads"][ask_id][route]["status"] for route in ("ask read", "ask events", "ask export")] == [200, 200, 200], ask_id
        assert at_a["reads"][ask_id]["ask events"]["events"][-1] == "done"


def test_release_a_gives_the_stored_link_back_unchanged_and_nothing_where_none_is_stored(at_a):
    reads = at_a["reads"]
    want = dict(zip(NAMES, [LINKED, LINKED, "absent", LINKED, "absent"]))
    for name, ask_id in zip(NAMES, link_ids(at_a)):
        assert reads[ask_id]["ask read"]["links"] == want[name], name


def test_the_receipt_at_release_a_covers_the_key_and_says_pass(at_a):
    receipt = h.build_receipt(ch.git_head(), at_a, base_commit=A)
    assert receipt["problems"] == [] and receipt["verdict"] == "pass" and receipt["prior_stores_answer_meta"] is True
    assert h.layer_problems(at_a) == {"layer 1": [], "layer 2": [], "layer 3": [], "layer 4": []}


def test_the_key_is_declared_for_a_previous_release_that_stores_the_state_and_every_other_new_key_is_not(at_a):
    ids = link_ids(at_a)
    assert h.layer2_problems(at_a["reads"], at_a["twin_of"], declared=h.C_RECORD_KEYS) == []
    undeclared = h.layer2_problems(at_a["reads"], at_a["twin_of"], declared=())
    assert any(p.startswith(f"{ids[0]} ask read.evidence_links: undeclared key") for p in undeclared)
    assert h.C_RECORD_KEYS == {"evidence_links"}
    mutated = copy.deepcopy(at_a)
    shape = mutated["reads"][ids[0]]["ask read"]["shape"]
    mutated["reads"][ids[0]]["ask read"]["shape"] = {**shape, "surprise": "string"}
    problems = h.layer_problems(mutated)["layer 2"]
    assert any("ask read.surprise: undeclared key" in p for p in problems) and not any("evidence_links: undeclared" in p for p in problems)


def test_a_previous_reader_that_changes_or_drops_the_stored_link_is_found(at_a):
    ids = link_ids(at_a)
    changed = copy.deepcopy(at_a)
    changed["reads"][ids[0]]["ask read"]["links"] = {"v": 1, "parents": {}}
    assert any(p.startswith(f"{ids[0]} ask read.evidence_links:") for p in h.layer_problems(changed)["layer 2"])
    dropped = copy.deepcopy(at_a)
    dropped["reads"][ids[0]]["ask read"]["links"] = "absent"
    assert any(p.startswith(f"{ids[0]} ask read.evidence_links:") for p in h.layer_problems(dropped)["layer 2"])
    invented = copy.deepcopy(at_a)
    invented["reads"][ids[2]]["ask read"]["links"] = LINKED
    assert any(p.startswith(f"{ids[2]} ask read.evidence_links:") for p in h.layer_problems(invented)["layer 2"])


def test_a_read_that_does_not_show_whether_the_key_came_back_is_a_problem_for_a_row_that_stores_it(at_a):
    ids = link_ids(at_a)
    blind = copy.deepcopy(at_a)
    del blind["reads"][ids[0]]["ask read"]["links"]
    assert any(ids[0] in p and "evidence_links" in p for p in h.layer_problems(blind)["layer 2"])


def test_a_corpus_without_the_link_rows_cannot_pass_once_the_candidate_has_the_producer(at_a):
    stripped = copy.deepcopy(at_a)
    ids = set(link_ids(stripped))
    stripped["rows"] = [row for row in stripped["rows"] if h.ask_id_of(row) not in ids]
    stripped["layer2_rows"] = [row for row in stripped["layer2_rows"] if h.ask_id_of(row) not in ids]
    stripped["reads"] = {k: v for k, v in stripped["reads"].items() if k not in ids}
    receipt = h.build_receipt(ch.git_head(), stripped, base_commit=A)
    assert receipt["verdict"] == "fail" and any(p.startswith("layer 1: ") and "evidence_links" in p for p in receipt["problems"])



# Release C as the previous release (the D0 gate). Its writers store evidence_links as well as answer_meta, and its readers act on the key:
# with a creator hidden in the fixture world they withhold a comment whose post they cannot place, the claims that cite it and the so_what
# items of those claims, mark the read with privacy, and prune the link to what is still shown. A candidate link row is therefore judged
# against Release C's own row of the same case, made by Release C's producer from Release C's own complete row and read by the same
# readers, never against the key-absent twin. The keys a candidate may add are the ones the previous writers do not already store.

C = "d0aebe7"


def link_row(ask_id, links="absent"):
    out = row(ask_id)
    if links != "absent":
        out["record"]["evidence_links"] = copy.deepcopy(links)
    return out


def links_read(ask_id, links, body=None):
    got = reads_for([ask_id], body or {"ask_id": "a"})[ask_id]
    got["ask read"]["links"] = copy.deepcopy(links)
    return got


def links_world(candidate_got, prior_got, *, candidate_stored=LINKED, prior_stored=LINKED, keep=True):
    """One candidate link row a_c1 with the previous release's own row of the same case a_p1 as its twin, and (keep) one more own row
    a_p9 that came back as stored, so the rule has a row that shows the previous reader giving a link back."""
    candidate, prior = link_row("a_c1", candidate_stored), [link_row("a_p1", prior_stored)]
    reads = {"a_c1": links_read("a_c1", candidate_got), "a_p1": links_read("a_p1", prior_got)}
    if keep:
        prior.append(link_row("a_p9", LINKED))
        reads["a_p9"] = links_read("a_p9", LINKED)
    return reads, [candidate], {"a_c1": "a_p1"}, prior


def test_the_keys_the_previous_writers_store_are_read_from_what_they_wrote():
    with_meta, without = [row("a_1"), row("a_2", "failed")], [row("a_1", meta=False)]
    assert h.prior_record_keys(without, []) == set()
    assert h.prior_record_keys(with_meta, []) == {"answer_meta"}
    assert h.prior_record_keys(with_meta, [link_row("a_p1", LINKED), link_row("a_p2")]) == {"answer_meta", "evidence_links"}
    assert h.prior_record_keys(with_meta, [link_row("a_p2")]) == {"answer_meta"}, "a producer that stored no key on any row stores none"


def test_a_candidate_may_add_only_the_keys_the_previous_writers_do_not_store():
    assert h.declared_keys(set()) == h.RECORD_KEYS == {"answer_meta", "evidence_links"}
    assert h.declared_keys({"answer_meta"}) == h.C_RECORD_KEYS == {"evidence_links"}
    assert h.declared_keys({"answer_meta", "evidence_links"}) == set()


def test_a_tree_whose_producer_writes_no_link_gives_no_rows_of_its_own_and_the_suffix_names_them(candidate, tmp_path):
    written, linked = candidate
    assert h.linked_rows(ch.archive(A, tmp_path / "a"), written, suffix="_prior") == []
    own = h.linked_rows(h.ROOT, written, suffix="_prior")
    assert [h.ask_id_of(r) for r in own] == [h.ask_id_of(r) + "_prior" for r in linked]
    assert all(r["run_id"] == h.ask_id_of(r) and r["record"]["answer_meta"]["ask_id"] == h.ask_id_of(r) for r in own)


def test_a_candidate_row_read_as_the_previous_release_reads_its_own_row_of_the_case_is_no_problem():
    assert h.links_problems(*links_world("absent", "absent")) == []
    assert h.links_problems(*links_world(LINKED, LINKED)) == []


def test_a_previous_reader_that_withholds_its_own_link_must_withhold_the_candidate_one_too():
    problems = h.links_problems(*links_world(LINKED, "absent"))
    assert len(problems) == 1 and problems[0].startswith("a_c1 ask read.evidence_links:") and "a_p1" in problems[0]


def test_where_the_previous_reader_gives_its_own_link_back_as_stored_the_candidate_link_must_come_back_as_the_candidate_stored_it():
    other = {"v": 2, "parents": {"x_comment_7001": "x_fixture_3"}}
    problems = h.links_problems(*links_world("absent", LINKED, candidate_stored=other))
    assert len(problems) == 1 and problems[0].startswith("a_c1 ask read.evidence_links: the previous reader gave absent where the record stores")


def test_a_read_of_the_previous_release_own_row_that_does_not_show_the_key_is_a_problem():
    reads, rows, twin_of, prior = links_world(LINKED, LINKED)
    del reads["a_p1"]["ask read"]["links"]
    assert any(p.startswith("a_p1 ask read.evidence_links:") and "does not show" in p for p in h.links_problems(reads, rows, twin_of, prior))


def test_own_rows_none_of_which_came_back_as_stored_prove_nothing_and_are_a_problem():
    problems = h.links_problems(*links_world("absent", "absent", keep=False))
    assert problems == ["ask read.evidence_links: no link row of the previous release came back as stored, so its rows show nothing"]


def c_data(candidate_body, prior_body):
    """layer_problems data for a previous release that stores both keys: one candidate link row against its own row of the case."""
    reads, rows, twin_of, prior = links_world("absent", "absent")
    reads["a_c1"]["ask read"] = {**read(candidate_body), "links": "absent"}
    reads["a_p1"]["ask read"] = {**read(prior_body), "links": "absent"}
    return {"rows": rows, "legacy": [], "twin_of": twin_of, "paths": set(), "reads": reads, "reverse": {}, "prior_stores_answer_meta": True,
            "layer2_rows": rows, "prior_linked": prior, "prior_record_keys": ["answer_meta", "evidence_links"]}


MARKER = {"v": 1, "state": "applied", "withheld": {"posts": 1, "claims": 1, "summary": False}}


def test_a_withholding_the_previous_release_gives_its_own_row_too_is_not_an_undeclared_key():
    body = {"ask_id": "a", "answer": {"so_what": []}, "privacy": MARKER}
    assert h.layer_problems(c_data(body, body))["layer 2"] == []


def test_a_withholding_marker_on_the_candidate_row_alone_is_undeclared_and_a_lost_so_what_is_missing():
    plain = {"ask_id": "a", "answer": {"so_what": [{"text": "x", "claim_ids": ["c1"]}]}}
    withheld = {"ask_id": "a", "answer": {"so_what": []}, "privacy": MARKER}
    problems = h.layer_problems(c_data(withheld, plain))["layer 2"]
    assert "a_c1 ask read.privacy: undeclared key" in problems
    assert any(p.startswith("a_c1 ask read.answer.so_what[0]") for p in problems)


def test_a_key_the_previous_release_already_stores_is_no_longer_declared_against_it():
    problems = h.layer_problems(c_data({"ask_id": "a", "evidence_links": LINKED}, {"ask_id": "a"}))["layer 2"]
    assert "a_c1 ask read.evidence_links: undeclared key" in problems


def test_a_previous_release_own_link_row_nobody_read_is_a_problem_not_a_pass():
    data = c_data({"ask_id": "a"}, {"ask_id": "a"})
    del data["reads"]["a_p1"]
    assert any(p.startswith("a_p1: no read of") for p in h.layer_problems(data)["layer 2"])


# the evidence: Release C's own writers and readers judging this tree

@pytest.fixture(scope="module")
def at_c(tmp_path_factory):
    return h.collect(ch.ROOT, tmp_path_factory.mktemp("old-readers-at-c"), base_commit=C)


def c_link_ids(suffix=""):
    return [f"a_20261009_link{number:02d}{suffix}" for number in range(1, len(NAMES) + 1)]


def test_the_harness_derives_that_release_c_stores_both_keys_and_reads_its_own_link_rows(at_c):
    assert at_c["prior_stores_answer_meta"] is True and at_c["prior_record_keys"] == ["answer_meta", "evidence_links"]
    own = c_link_ids("_prior")
    assert [h.ask_id_of(r) for r in at_c["prior_linked"]] == own
    assert [at_c["twin_of"][c] for c in c_link_ids()] == own
    assert all(at_c["reads"][i]["ask read"]["status"] == 200 for i in own)


def test_release_c_withholds_what_it_cannot_place_on_its_own_rows_as_stated_here(at_c):
    """The stated withholding of Release C's readers in the fixture world, pinned here and not read from the rows: the link comes back
    on the linked and the partial case and on no other, and every case but the linked one carries the privacy marker."""
    reads = at_c["reads"]
    want = dict(zip(NAMES, [LINKED, LINKED, "absent", "absent", "absent"]))
    for suffix in ("_prior", ""):
        for name, ask_id in zip(NAMES, c_link_ids(suffix)):
            assert reads[ask_id]["ask read"]["links"] == want[name], (name, suffix)
            assert ("privacy" in reads[ask_id]["ask read"]["shape"]) is (name != "linked"), (name, suffix)


def test_the_receipt_at_release_c_says_pass_with_no_problem(at_c):
    receipt = h.build_receipt(ch.git_head(), at_c, base_commit=C)
    assert receipt["problems"] == [] and receipt["verdict"] == "pass", receipt["problems"]
    assert receipt["legacy_rows"] == 4 and receipt["json_paths"] > 0 and receipt["corpus_rows"] > 4


def test_against_release_c_a_candidate_read_that_differs_from_its_own_row_is_found(at_c):
    pruned, first, second = c_link_ids()[NAMES.index("post_pruned")], c_link_ids()[0], c_link_ids()[1]
    through = copy.deepcopy(at_c)
    through["reads"][pruned]["ask read"]["links"] = LINKED
    assert any(p.startswith(f"{pruned} ask read.evidence_links:") for p in h.layer_problems(through)["layer 2"])
    lost = copy.deepcopy(at_c)
    lost["reads"][first]["ask read"]["links"] = "absent"
    assert any(p.startswith(f"{first} ask read.evidence_links:") for p in h.layer_problems(lost)["layer 2"])
    unmarked = copy.deepcopy(at_c)
    shape = unmarked["reads"][second]["ask read"]["shape"]
    unmarked["reads"][second]["ask read"]["shape"] = {k: v for k, v in shape.items() if k != "privacy"}
    assert f"{second} ask read.privacy: missing from the candidate" in h.layer_problems(unmarked)["layer 2"]


def test_a_candidate_tree_that_writes_a_link_release_c_cannot_read_fails_the_receipt(tmp_path):
    """A planted incompatibility: the candidate producer writes the link under version 2, which Release C's reader takes for no link.
    The tree is this one with that one line changed; the harness must fail it against Release C and still pass it against Release A,
    whose readers do not act on the key."""
    tree = h.copy_tree(ch.ROOT, tmp_path / "mutant")
    checks = tree / "core/agent/checks.py"
    text = checks.read_text(encoding="utf-8")
    planted = text.replace('return {"v": 1, "parents": parents} if parents else None', 'return {"v": 2, "parents": parents} if parents else None')
    assert planted != text, "the producer line moved; the mutation must be planted again"
    checks.write_text(planted, encoding="utf-8")
    receipt = h.build_receipt("c" * 40, h.collect(tree, tmp_path / "c", base_commit=C), base_commit=C)
    assert receipt["verdict"] == "fail"
    assert any(p.startswith("layer 2: a_20261009_link01 ask read.evidence_links:") for p in receipt["problems"]), receipt["problems"]
    assert h.build_receipt("c" * 40, h.collect(tree, tmp_path / "a", base_commit=A), base_commit=A)["verdict"] == "pass"
