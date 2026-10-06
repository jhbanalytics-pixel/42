"""What admission does with one post the producer collected more than once.

The producer assigns every row a fresh random id when it writes raw_content, and the
enriched row it writes next reuses that id (build_raw_row in scripts/run_rss_now.py). A
post collected again, on another day or under another query term, therefore arrives as a
second row with a second id. One id occurs twice in one lane only when the same producer
row was written twice.

These tests drive admit_protected_context through the real query builders and decoders,
with the wire rows the evidence query returns for each case, and state what admission
does today.
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


def _admit(evidence_rows):
    """Admit enriched evidence rows given as (id, payload, match_count) triples."""
    builder = wires.subject()
    args = wires.inputs()
    request, plan, intake, binding = args
    binding["source_binding_digest"] = canonical_digest(binding)
    candidates = wires.candidate_rows(binding)
    partitions = [
        {"lane": "__metadata__", "partition_date": None, "partition_count": 1},
        {"lane": "enriched_content", "partition_date": "2026-09-07", "partition_count": 1},
    ]
    identifiers = sorted({identifier for identifier, _payload, _count in evidence_rows})
    indexes = [wires.metadata(binding, len(identifiers), len(identifiers), evidence=True)]
    for identifier in identifiers:
        indexes.append(
            {
                "lane": "enriched_content",
                "market": "za",
                "id": identifier,
                "match_count": 1,
                "payload": json.dumps(
                    {"market": "za", "id": identifier, "collected_date": "2026-09-07"}
                ),
            }
        )
    evidence = [wires.metadata(binding, len(evidence_rows), len(evidence_rows), evidence=True)]
    for identifier, payload, match_count in evidence_rows:
        evidence.append(
            {
                "lane": "enriched_content",
                "market": "za",
                "id": identifier,
                "match_count": match_count,
                "payload": json.dumps({"id": identifier, **payload}),
            }
        )
    empty = [wires.metadata(binding, 0, 0, evidence=True)]
    queries = {
        "candidate": builder.build_context_candidate_query(*args, candidate_limit=100),
        "partitions": builder.build_context_partition_query(*args),
    }
    rows = {"candidate": candidates, "partitions": partitions}
    for key, lane in (("enriched", "enriched_content"), ("raw", "raw_content")):
        common = {
            "candidate_rows": candidates,
            "partition_rows": partitions,
            "lane": lane,
            "candidate_limit": 200,
            "geo_policy": True,
            **({"enriched_rows": evidence} if key == "raw" else {}),
        }
        rows[key + "_index"] = indexes if key == "enriched" else empty
        rows[key] = evidence if key == "enriched" else empty
        queries[key + "_index"] = builder.build_context_index_query(*args, **common)
        queries[key] = builder.build_context_evidence_query(
            *args, index_rows=rows[key + "_index"], **common
        )
    # The evidence query counts rows per (market, id), so a wire row's match_count is what
    # the SQL returns for that id; this pins the fixture to the query admission reads.
    assert (
        "COUNT(*) OVER (PARTITION BY evidence.market, evidence.id) AS match_count"
        in queries["enriched"].sql
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
    return subject.admit_protected_context(
        request,
        plan,
        intake,
        source=subject.LoadedProtectedContext(binding["profile_id"], {}, binding),
        queries=queries,
        captures=captures,
        query_records=records,
    )


def test_a_recollected_post_is_admitted_as_two_unknown_observations_not_refused():
    first = {**_POST, "collected_at": "2026-09-07T01:00:00+00:00"}
    second = {**_POST, "collected_at": "2026-09-07T05:00:00+00:00"}
    result = _admit([("uuid-first", first, 1), ("uuid-second", second, 1)])

    assert len(result["receipts"]) == 2
    assert not any("claimed two ways" in line for line in result["limitations"])
    assert result["observation_keys"] == [
        ["za", "reddit", "uuid-first"],
        ["za", "reddit", "uuid-second"],
    ]
    assert result["units"]["collected_records"] == 2
    assert result["units"]["unique_observations"] == 2
    assert result["independent_origin_count"] == 0
    assert result["unknown_origin_count"] == 2
    assert result["origin_authority_projection"]["state"] == "not_projected"
    assert result["native_id_projection"]["identity_states"] == {"not_projected": 2}


def _swept(monkeypatch):
    """Record every identity sweep, so a test can tell which guard refused the read."""
    calls = []
    sweep = subject._identifiable_facts

    def recorded(facts):
        calls.append(len(facts))
        return sweep(facts)

    monkeypatch.setattr(subject, "_identifiable_facts", recorded)
    return calls


def test_one_producer_row_written_twice_byte_identical_admits_as_the_single_row(monkeypatch):
    swept = _swept(monkeypatch)
    payload = {**_POST, "collected_at": "2026-09-07T01:00:00+00:00"}
    result = _admit([("uuid-twice", payload, 2), ("uuid-twice", payload, 2)])
    # Byte identical copies of one row in one lane count once at the match count check,
    # so the identity sweep and the selector see the single row, never the pair.
    assert swept == [1]
    assert len(result["receipts"]) == 1
    assert result["observation_keys"] == [["za", "reddit", "uuid-twice"]]
    assert result["units"]["collected_records"] == 1


def test_one_producer_row_written_twice_with_other_counts_refuses_the_whole_admission(
    monkeypatch,
):
    swept = _swept(monkeypatch)
    first = {**_POST, "collected_at": "2026-09-07T01:00:00+00:00"}
    second = {**_POST, "collected_at": "2026-09-07T05:00:00+00:00"}
    with pytest.raises(ValueError, match="protected_context_invalid") as raised:
        _admit([("uuid-twice", first, 2), ("uuid-twice", second, 2)])
    # Without the match count guard the sweep would refuse both rows as claimed two
    # ways and the read would then refuse for want of coverage; the sweep never runs.
    assert raised.value.__cause__ is None
    assert swept == []


def test_a_recollected_post_passes_through_the_identity_sweep(monkeypatch):
    swept = _swept(monkeypatch)
    first = {**_POST, "collected_at": "2026-09-07T01:00:00+00:00"}
    second = {**_POST, "collected_at": "2026-09-07T05:00:00+00:00"}
    _admit([("uuid-first", first, 1), ("uuid-second", second, 1)])
    assert swept == [2]
