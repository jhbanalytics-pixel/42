"""Byte identical rows of one lane count once at the Ask admission match count check.

The producer can write one row twice, and the evidence query then returns both copies
with a match count of two. Copies that are byte identical on the wire are one row and
admit as one fact. Rows that share a market and id but differ in any byte still refuse
the whole admission at the same check.
"""

import copy
import json

import pytest
from src.analysis.open_intelligence import general_question_context_admission as subject
from src.analysis.open_intelligence.brain_contract import canonical_digest

from tests.unit import test_general_question_context_queries as wires

_POST = {
    "market": "za",
    "source": "reddit",
    "platform": "reddit",
    "author_handle": None,
    "url": "https://example.test/one-post",
    "text": "Night commute observations from this source.",
    "title": None,
    "published_at": "2026-09-07T00:00:00+00:00",
}


def _admit(evidence_rows, *, lane="enriched_content", full=None):
    """Admit evidence rows of one lane given as (id, payload, match_count) triples.

    full is the matching count the evidence query reports; above the rows returned it
    marks the selection as truncated.
    """
    other = "raw_content" if lane == "enriched_content" else "enriched_content"
    builder = wires.subject()
    args = wires.inputs()
    request, plan, intake, binding = args
    binding["source_binding_digest"] = canonical_digest(binding)
    candidates = wires.candidate_rows(binding)
    partitions = [
        {"lane": "__metadata__", "partition_date": None, "partition_count": 1},
        {"lane": lane, "partition_date": "2026-09-07", "partition_count": 1},
    ]
    identifiers = sorted({identifier for identifier, _payload, _count in evidence_rows})
    indexes = [wires.metadata(binding, len(identifiers), len(identifiers), evidence=True)]
    for identifier in identifiers:
        indexes.append(
            {
                "lane": lane,
                "market": "za",
                "id": identifier,
                "match_count": 1,
                "payload": json.dumps(
                    {"market": "za", "id": identifier, "collected_date": "2026-09-07"}
                ),
            }
        )
    evidence = [
        wires.metadata(binding, len(evidence_rows), full or len(evidence_rows), evidence=True)
    ]
    for identifier, payload, match_count in evidence_rows:
        evidence.append(
            {
                "lane": lane,
                "market": "za",
                "id": identifier,
                "match_count": match_count,
                # A payload given as a string is the wire spelling itself.
                "payload": payload
                if isinstance(payload, str)
                else json.dumps({"id": identifier, **payload}),
            }
        )
    empty = [wires.metadata(binding, 0, 0, evidence=True)]
    queries = {
        "candidate": builder.build_context_candidate_query(*args, candidate_limit=100),
        "partitions": builder.build_context_partition_query(*args),
    }
    rows = {"candidate": candidates, "partitions": partitions}
    lanes = {lane: (indexes, evidence), other: (empty, empty)}
    for key, table in (("enriched", "enriched_content"), ("raw", "raw_content")):
        common = {
            "candidate_rows": candidates,
            "partition_rows": partitions,
            "lane": table,
            "candidate_limit": 200,
            "geo_policy": True,
            **({"enriched_rows": lanes["enriched_content"][1]} if key == "raw" else {}),
        }
        rows[key + "_index"], rows[key] = lanes[table]
        queries[key + "_index"] = builder.build_context_index_query(*args, **common)
        queries[key] = builder.build_context_evidence_query(
            *args, index_rows=rows[key + "_index"], **common
        )
    # The evidence query counts rows per (market, id), so a wire row's match_count is what
    # the SQL returns for that id; this pins the fixture to the query admission reads.
    assert (
        "COUNT(*) OVER (PARTITION BY evidence.market, evidence.id) AS match_count"
        in queries["enriched" if lane == "enriched_content" else "raw"].sql
    )
    captures, records = {}, {}
    for key, query in queries.items():
        receipt = {
            "template_id": query.template_id,
            "sql_digest": query.sql_digest,
            "parameters_digest": query.parameters_digest,
            "query_state": "succeeded",
            "result_digest": canonical_digest(rows[key]),
            "job_id": "job_" + key,
        }
        captures[key] = {"rows": rows[key], "receipt": receipt}
        records[key] = {"receipt": copy.deepcopy(receipt), "reservation": copy.deepcopy(receipt)}
    result = subject.admit_protected_context(
        request,
        plan,
        intake,
        source=subject.LoadedProtectedContext(binding["profile_id"], {}, binding),
        queries=queries,
        captures=captures,
        query_records=records,
    )
    # A stored admission revalidates through the same decode and projection.
    assert (
        subject.validate_stored_context_admission(
            result,
            request=request,
            plan=plan,
            intake=intake,
            queries=queries,
            captures=captures,
            query_records=records,
        )
        == result
    )
    return result


_FIRST = {**_POST, "collected_at": "2026-09-07T01:00:00+00:00"}
_LATER = {**_POST, "collected_at": "2026-09-07T05:00:00+00:00"}
_COMPARED = (
    "receipts",
    "readings",
    "fulfilled_requirement_ids",
    "limitations",
    "observation_keys",
    "origin_groups",
    "independent_origin_count",
    "unknown_origin_count",
    "native_id_projection",
    "origin_authority_projection",
    "units",
)


_SNAPSHOT_BOUND = frozenset({"snapshot_id", "reading_id", "reading_ids"})


def _unbound(value):
    """Drop the identifiers derived from the snapshot id, which binds the capture's own
    result digest and so differs whenever the captured rows differ."""
    if isinstance(value, dict):
        return {key: _unbound(item) for key, item in value.items() if key not in _SNAPSHOT_BOUND}
    if isinstance(value, list):
        return [_unbound(item) for item in value]
    return value


def _swept(monkeypatch):
    """Record every identity sweep, so a test can tell which guard refused the read."""
    calls = []
    sweep = subject._identifiable_facts

    def recorded(facts):
        calls.append(len(facts))
        return sweep(facts)

    monkeypatch.setattr(subject, "_identifiable_facts", recorded)
    return calls


def _refused_at_the_match_count_check(monkeypatch, rows):
    swept = _swept(monkeypatch)
    with pytest.raises(ValueError, match="protected_context_invalid") as raised:
        _admit(rows)
    # The match count check refuses with no cause, before the identity sweep runs.
    assert raised.value.__cause__ is None
    assert swept == []


@pytest.mark.parametrize("copies", [2, 3])
def test_a_byte_identical_repeated_write_admits_as_the_single_row(copies):
    single = _admit([("uuid-twice", _FIRST, 1)])
    repeated = _admit([("uuid-twice", _FIRST, copies)] * copies)
    assert len(repeated["receipts"]) == 1
    assert repeated["units"]["collected_records"] == 1
    assert repeated["observation_keys"] == [["za", "reddit", "uuid-twice"]]
    assert repeated["snapshot_id"] != single["snapshot_id"]
    assert [receipt["reading_ids"] for receipt in repeated["receipts"]] == [
        [reading["reading_id"] for reading in repeated["readings"]]
    ]
    for field in _COMPARED:
        assert _unbound(repeated[field]) == _unbound(single[field]), field


def test_a_differing_duplicate_still_refuses_at_the_match_count_check(monkeypatch):
    _refused_at_the_match_count_check(
        monkeypatch, [("uuid-twice", _FIRST, 2), ("uuid-twice", _LATER, 2)]
    )


def test_a_duplicate_differing_only_in_its_counts_still_refuses(monkeypatch):
    counted = {**_FIRST, "likes": 4.0}
    _refused_at_the_match_count_check(
        monkeypatch, [("uuid-twice", {**_FIRST, "likes": 1.0}, 2), ("uuid-twice", counted, 2)]
    )


def test_an_identical_pair_with_a_differing_third_row_refuses(monkeypatch):
    _refused_at_the_match_count_check(
        monkeypatch,
        [("uuid-twice", _FIRST, 3), ("uuid-twice", _FIRST, 3), ("uuid-twice", _LATER, 3)],
    )


def test_identical_copies_whose_match_count_disagrees_with_the_copy_count_refuse(monkeypatch):
    _refused_at_the_match_count_check(
        monkeypatch, [("uuid-twice", _FIRST, 3), ("uuid-twice", _FIRST, 3)]
    )


@pytest.mark.parametrize("copies", [2, 3])
def test_identical_copies_that_each_claim_a_single_match_refuse(monkeypatch, copies):
    """Each copy saying match count one while several arrive is a real double write: the
    source itself says there is one row, so the copies are not collapsed into it."""
    _refused_at_the_match_count_check(monkeypatch, [("uuid-twice", _FIRST, 1)] * copies)


def test_identical_copies_claiming_fewer_matches_than_arrive_refuse(monkeypatch):
    _refused_at_the_match_count_check(monkeypatch, [("uuid-twice", _FIRST, 2)] * 3)


def test_copies_that_differ_only_in_their_wire_spelling_still_refuse(monkeypatch):
    spelled = json.dumps({"id": "uuid-twice", **_FIRST})
    respelled = json.dumps({"id": "uuid-twice", **_FIRST}, separators=(",", ":"))
    assert json.loads(spelled) == json.loads(respelled)
    _refused_at_the_match_count_check(
        monkeypatch, [("uuid-twice", spelled, 2), ("uuid-twice", respelled, 2)]
    )


def test_a_raw_lane_identical_double_write_admits_as_the_single_row():
    single = _admit([("uuid-twice", _FIRST, 1)], lane="raw_content")
    repeated = _admit([("uuid-twice", _FIRST, 2)] * 2, lane="raw_content")
    assert [receipt["source_row_id"] for receipt in repeated["receipts"]] == ["uuid-twice"]
    assert repeated["units"]["collected_records"] == 1
    for field in _COMPARED:
        assert _unbound(repeated[field]) == _unbound(single[field]), field


def test_a_raw_lane_differing_duplicate_still_refuses(monkeypatch):
    swept = _swept(monkeypatch)
    with pytest.raises(ValueError, match="protected_context_invalid") as raised:
        _admit([("uuid-twice", _FIRST, 2), ("uuid-twice", _LATER, 2)], lane="raw_content")
    assert raised.value.__cause__ is None
    assert swept == []


def test_the_truncation_line_counts_unique_facts_not_copies():
    truncated = _admit([("uuid-twice", _FIRST, 2)] * 2, full=5)
    assert (
        "Enriched selection was truncated to 1 of 4 matching records." in truncated["limitations"]
    )
    assert not any("truncated to 2 of" in line for line in truncated["limitations"])
    single = _admit([("uuid-twice", _FIRST, 1)], full=4)
    assert "Enriched selection was truncated to 1 of 4 matching records." in single["limitations"]


def test_json_spelling_variants_never_merge_when_the_decoder_does_not_line_up(monkeypatch):
    """A decoder whose records do not line up with the captured wire rows refuses a
    repeated identity rather than merging copies on their decoded form."""
    spelled = json.dumps({"id": "uuid-twice", **_FIRST})
    respelled = json.dumps({"id": "uuid-twice", **_FIRST}, separators=(",", ":"))
    decode = subject._decode_context_rows

    def respelling(rows, query):
        material = copy.deepcopy(decode(rows, query))
        if material["records"] and material["records"][0]["lane"] == "enriched_content":
            extra = {**material["records"][0], "payload": json.loads(respelled)}
            material["records"].append(extra)
        return material

    monkeypatch.setattr(subject, "_decode_context_rows", respelling)
    assert json.loads(spelled) == json.loads(respelled)
    with pytest.raises(ValueError, match="protected_context_invalid"):
        _admit([("uuid-twice", spelled, 2)])
