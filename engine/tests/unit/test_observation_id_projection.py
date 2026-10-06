"""The obs1 projection: a read time identity over snapshot or raw rows, added beside the key.

Ask admission and the snapshot identify an observation by (market, platform, id), and the
producer gives every row a fresh uuid4, so one post collected in two runs reads as two
unknown observations. The projection derives obs1 at read time from the columns
raw_content already holds and reports it beside that key. It is an added identity, not a
collapse rule: it never merges, drops or refuses a row, and admission's refusal of a
byte-different double write stays exactly where it was.
"""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from src.analysis.open_intelligence import observation_id_projection as projection
from src.ingestion.observation_id import observation_id

from tests.unit import test_general_question_context_recollection as recollection

ENGINE = Path(__file__).resolve().parents[2]

_POST = {
    "source": "reddit",
    "platform": "reddit",
    "market": "za",
    "author_handle": None,
    "author_name": None,
    "title": None,
    "text": "Night commute observations from this source.",
    "url": "https://example.test/one-post",
    "published_at": "2026-09-07T00:00:00+00:00",
}


def _run(identifier, run, **changes):
    return {
        **_POST,
        "id": identifier,
        "collected_at": f"2026-09-0{run}T01:00:00+00:00",
        "pipeline_run_id": f"run-{run}",
        "query_term": f"term {run}",
        "likes": run,
        **changes,
    }


def test_the_same_item_recollected_in_two_runs_gets_one_obs1():
    rows = [_run("uuid-first", 7), _run("uuid-second", 8)]
    value = projection.observation_id_projection(rows)
    assert value["version"] == projection.OBSERVATION_ID_PROJECTION_VERSION
    assert value["scheme"] == "obs1"
    assert value["state"] == "projected"
    [first, second] = value["rows"]
    assert first["observation_id"] == second["observation_id"] == observation_id(_POST)
    assert value["observations"] == [
        {
            "observation_id": observation_id(_POST),
            "members": [["za", "uuid-first"], ["za", "uuid-second"]],
        }
    ]
    assert value["units"] == {
        "collected_records": 2,
        "derived_records": 2,
        "underived_records": 0,
        "distinct_row_ids": 2,
        "distinct_observation_ids": 1,
        "recollected_observation_ids": 1,
    }


def test_different_items_get_different_obs1():
    rows = [
        _run("uuid-first", 7),
        _run("uuid-other", 7, url="https://example.test/other-post"),
        _run("uuid-market", 7, market="ng"),
    ]
    value = projection.observation_id_projection(rows)
    assert len({row["observation_id"] for row in value["rows"]}) == 3
    assert value["units"]["distinct_observation_ids"] == 3
    assert value["units"]["recollected_observation_ids"] == 0


def test_the_projection_names_the_key_each_row_was_derived_from():
    rows = [
        _run("a", 7, native_id="t3_abc"),
        _run("b", 7),
        _run("c", 7, url=""),
        _run("d", 7, url="", text="", title=None),
    ]
    value = projection.observation_id_projection(rows)
    assert [(row["id"], row["key_kind"]) for row in value["rows"]] == [
        ("a", "native"),
        ("b", "url"),
        ("c", "text"),
        ("d", None),
    ]
    assert value["rows"][3]["observation_id"] is None
    assert value["units"]["underived_records"] == 1
    assert value["key_columns"] == [
        column for column in projection.KEY_COLUMNS if column in rows[0]
    ]


def test_a_projection_with_no_derivable_row_says_so():
    value = projection.observation_id_projection([_run("d", 7, url="", text="")])
    assert value["state"] == "not_projected"
    assert value["observations"] == []


def test_the_projection_is_pure_and_order_free():
    rows = [_run("uuid-second", 8), _run("uuid-first", 7), _run("x", 7, market="ke")]
    before = copy.deepcopy(rows)
    value = projection.observation_id_projection(rows)
    assert rows == before
    assert value == projection.observation_id_projection(list(reversed(rows)))
    assert projection.validate_observation_id_projection(value, rows=rows) == value


def test_a_byte_different_double_write_is_listed_row_by_row_never_collapsed():
    rows = [_run("uuid-twice", 7), _run("uuid-twice", 7, likes=40)]
    value = projection.observation_id_projection(rows)
    assert len(value["rows"]) == 2
    assert value["observations"][0]["members"] == [["za", "uuid-twice"]]
    assert value["units"]["collected_records"] == 2
    assert value["units"]["distinct_row_ids"] == 1
    assert value["units"]["recollected_observation_ids"] == 0


def test_admission_still_refuses_a_byte_different_double_write(monkeypatch):
    """Sharing one obs1 does not make a byte-different double write admissible."""
    first = {**recollection._POST, "collected_at": "2026-09-07T01:00:00+00:00"}
    second = {**recollection._POST, "collected_at": "2026-09-07T05:00:00+00:00"}
    value = projection.observation_id_projection(
        [{"id": "uuid-twice", **first}, {"id": "uuid-twice", **second}]
    )
    assert value["units"]["distinct_observation_ids"] == 1
    with pytest.raises(ValueError, match="protected_context_invalid"):
        recollection._admit([("uuid-twice", first, 2), ("uuid-twice", second, 2)])
    recollection.test_one_producer_row_written_twice_with_other_counts_refuses_the_whole_admission(
        monkeypatch
    )


@pytest.mark.parametrize(
    "rows",
    [
        "not a list",
        [["za", "id"]],
        [{**_POST, "id": ""}],
        [{**_POST, "id": 7}],
        [{key: value for key, value in _POST.items() if key != "market"} | {"id": "a"}],
    ],
)
def test_rows_without_a_market_and_id_are_refused_by_name(rows):
    with pytest.raises(ValueError, match="observation_id_projection_invalid"):
        projection.observation_id_projection(rows)


def test_a_stored_projection_is_validated_and_rebuilt_from_its_rows():
    rows = [_run("uuid-first", 7), _run("uuid-second", 8)]
    value = projection.observation_id_projection(rows)
    assert projection.validate_observation_id_projection(value) == value
    tampered = copy.deepcopy(value)
    tampered["rows"][1]["observation_id"] = "obs1_" + "0" * 32
    with pytest.raises(ValueError, match="observation_id_projection_invalid"):
        projection.validate_observation_id_projection(tampered, rows=rows)
    for field in projection.PROJECTION_FIELDS:
        missing = {key: item for key, item in value.items() if key != field}
        with pytest.raises(ValueError, match="observation_id_projection_invalid"):
            projection.validate_observation_id_projection(missing)
    with pytest.raises(ValueError, match="observation_id_projection_invalid"):
        projection.validate_observation_id_projection({**value, "version": "obs2_projection_v1"})
    with pytest.raises(ValueError, match="observation_id_projection_invalid"):
        projection.validate_observation_id_projection({**value, "extra": 1})


def test_a_projection_stored_as_canonical_json_still_validates():
    from src.analysis.open_intelligence.brain_contract import canonical_bytes

    rows = [_run("uuid-first", 7), _run("uuid-second", 8, native_id=None)]
    value = projection.observation_id_projection(rows)
    stored = json.loads(canonical_bytes(value))
    assert list(stored) != list(value)
    assert projection.validate_observation_id_projection(stored, rows=rows) == value


def test_an_old_projection_record_reads_as_not_recorded():
    assert projection.stored_observation_id_projection({"contract_version": "any"}) == (
        projection.OBSERVATION_ID_PROJECTION_NOT_RECORDED
    )
    value = projection.observation_id_projection([_run("uuid-first", 7)])
    stored = {"observation_id_projection": value}
    assert projection.stored_observation_id_projection(stored) == value
    with pytest.raises(ValueError, match="observation_id_projection_invalid"):
        projection.stored_observation_id_projection({"observation_id_projection": {}})


def test_old_projection_snapshots_still_read_and_validate(monkeypatch):
    """Retained v1 and current v3 snapshots carry no obs1 and validate exactly as before."""
    from src.analysis.open_intelligence import general_question_context_admission as subject
    from src.analysis.open_intelligence import general_question_snapshot as snapshot

    from tests.unit import test_general_question_snapshot_sidecar as sidecar_tests

    request, plan, intake = sidecar_tests.inputs()
    loaded, queries, captures, records = sidecar_tests.query_inputs(monkeypatch)
    current = sidecar_tests.admit(request, plan, intake, loaded, queries, captures, records)
    retained = sidecar_tests.to_v1(current)
    common = {
        "request": request,
        "plan": plan,
        "intake": intake,
        "queries": queries,
        "captures": captures,
        "query_records": records,
    }
    assert subject.validate_stored_context_admission(retained, **common) == retained
    assert subject.validate_stored_context_admission(current, **common) == current
    provenance = {
        "profile": "protected_context_v1",
        "source_binding": loaded.source_binding,
        "authority_captures": [],
        "captures": captures,
        "admission": retained,
        "limits": {},
    }
    old = snapshot._project_context_snapshot(
        sidecar_tests.context_for(request, intake), plan, retained, provenance
    )
    assert old["snapshot_digest"] == sidecar_tests.RETAINED_V1_SNAPSHOT_DIGEST
    sidecar = snapshot._identity_sidecar(current, loaded.source_binding)
    stored = snapshot._project_context_snapshot(
        sidecar_tests.context_for(request, intake),
        plan,
        current,
        {
            **provenance,
            "profile": "protected_context_v3",
            "admission": current,
            "identity_sidecar": sidecar,
        },
    )
    support = snapshot.read_context_evidence_support(
        stored, [row["receipt_id"] for row in stored["receipts"]]
    )
    assert support["units"]["unique_observations"] == 1
    for record in (old, stored, old["provenance"]["admission"], current, sidecar):
        assert projection.stored_observation_id_projection(record) == (
            projection.OBSERVATION_ID_PROJECTION_NOT_RECORDED
        )


def test_the_projection_moves_no_frozen_digest():
    """The frozen registry, bridge manifest and contract bytes are untouched by this slice."""
    pinned = {
        "configs/open_intelligence/execution_origins_bridge_v3.json": (
            "f23001ed7c5ae2d108dda69ab412e79d2c6b951ce147e92b3b53559cfc67b9e2"
        ),
        "configs/open_intelligence/resource_manifest_bridge_v3.json": (
            "592ed45ffdc1a0a10371d197bc4cc2057372b12f845b7ea9c0280bfd68b37efe"
        ),
        "configs/open_intelligence/candidate_contracts/source-bridge-capture-v3.json": (
            "b007a53067e2e4e841421c146eb96bedc3f269b413b253db0d21c0ebd16ce226"
        ),
    }
    for path, digest in pinned.items():
        assert hashlib.sha256((ENGINE / path).read_bytes()).hexdigest() == digest, path
        assert "observation_id" not in (ENGINE / path).read_text(encoding="utf-8")


def test_comments_on_one_post_located_by_fragment_stay_separate_observations():
    post = "https://www.youtube.com/watch?v=abc"
    rows = [_run(f"uuid-{n}", 7, url=f"{post}#comment-{n}") for n in (1, 2)]
    value = projection.observation_id_projection(rows)
    assert value["rows"][0]["observation_id"] != value["rows"][1]["observation_id"]
    assert [item["members"] for item in value["observations"]] in (
        [[["za", "uuid-1"]], [["za", "uuid-2"]]],
        [[["za", "uuid-2"]], [["za", "uuid-1"]]],
    )
