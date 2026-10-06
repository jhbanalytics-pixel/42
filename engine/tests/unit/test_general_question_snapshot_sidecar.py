"""Identity sidecar: the admission consumes the kernel, the snapshot carries the groups.

The retained v1 admission and snapshot bytes were pinned from the tree before this
change (commit c38aeb2) and must keep validating through the old path unchanged.
"""

import copy
import json

import pytest
from src.analysis.open_intelligence import general_question_context_admission as subject
from src.analysis.open_intelligence import general_question_context_identity as identity
from src.analysis.open_intelligence import general_question_snapshot as snapshot
from src.analysis.open_intelligence import production_snapshot_rows as rows_module
from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.general_question_control import QuestionStoreError

from tests.unit import test_production_snapshot_rows as rows_fixture
from tests.unit.test_general_question_context_admission import inputs, query_inputs

# Pinned from the tree at c38aeb2 for the query_inputs fixture: the v1 admission digest,
# its snapshot id, the v1 context snapshot digest over the synthetic provenance below and
# the legacy row material digest over the retained v3 fixture rows.
RETAINED_V1_ADMISSION_DIGEST = "5247d95d67ff5e2f4456caf85b97f3ddfb22982d51419709eebd56712c6694d4"
RETAINED_SNAPSHOT_ID = "gqs_d972092e3f064ad7d03afc1b76c397edf219227b322027340970aad4f61b6959"
RETAINED_V1_SNAPSHOT_DIGEST = "ff5079750f3a2aa7031b45a9cfcf928a66cb98d3655f17306fcaa6d18ac11355"
RETAINED_LEGACY_MATERIAL_DIGEST = "c90cb8cbb5f25b2039d1b0b49a200064ff65e9f204c7de1e2d46603e15d86010"
IDENTITY_FIELDS = (
    "observation_keys",
    "origin_groups",
    "independent_origin_count",
    "unknown_origin_count",
    "identity_policy_digest",
    "native_id_projection",
    "origin_authority_projection",
    "units",
)
# A row id the identity kernel cannot establish: NFD, so not the NFC form the kernel takes.
MALFORMED_ROW_ID = "row_e\u0301"


def admit(request, plan, intake, loaded, queries, captures, records):
    return subject.admit_protected_context(
        request,
        plan,
        intake,
        source=loaded,
        queries=queries,
        captures=captures,
        query_records=records,
    )


def with_second_record(monkeypatch, *, first=None, second=None, second_id="row_2"):
    """Add a second enriched record to the query_inputs material, both matching the plan term."""
    loaded, queries, captures, records = query_inputs(monkeypatch)
    decode = subject._decode_context_rows

    def extended(rows, query):
        material = copy.deepcopy(decode(rows, query))
        if query.key == "candidate":
            material["records"][0]["sample_row_ids"] = ["row_1", second_id]
        if query.key == "enriched":
            original = material["records"][0]
            original["payload"].update(first or {})
            copied = copy.deepcopy(original)
            copied["id"] = second_id
            copied["payload"].update({"id": second_id, **(second or {})})
            material["records"].append(copied)
            material["candidate_count"] = material["full_matching_count"] = 2
        return material

    monkeypatch.setattr(subject, "_decode_context_rows", extended)
    return loaded, queries, captures, records


def to_v1(admission):
    """The retained v1 shape: no identity fields, no unit line, v1 contract, digest recomputed."""
    value = {
        key: copy.deepcopy(item)
        for key, item in admission.items()
        if key not in (*IDENTITY_FIELDS, "admission_digest")
    }
    value["contract_version"] = "protected_context_admission_v1"
    value["limitations"] = [
        line for line in value["limitations"] if not line.startswith("Coverage counts name")
    ]
    value["admission_digest"] = canonical_digest(value)
    return value


def test_admission_reports_kernel_groups_policy_digest_and_units(monkeypatch):
    request, plan, intake = inputs()
    loaded, queries, captures, records = query_inputs(monkeypatch)
    result = admit(request, plan, intake, loaded, queries, captures, records)
    assert result["contract_version"] == "protected_context_admission_v3"
    assert set(result) == subject._OUTPUT_FIELDS | set(IDENTITY_FIELDS)
    assert result["observation_keys"] == [["ng", "web", "row_1"]]
    assert result["origin_groups"] == {}
    assert result["independent_origin_count"] == 0
    assert result["unknown_origin_count"] == 1
    assert result["identity_policy_digest"] == subject.identity_policy_digest()
    assert result["native_id_projection"] == {
        "version": rows_module.NATIVE_ID_PROJECTION_LEGACY,
        "identity_states": {"not_projected": 1},
    }
    assert result["units"] == {
        "collected_records": 1,
        "unique_observations": 1,
        "source_families": 0,
        "verified_independent_origins": 0,
        "unknown_origins": 1,
    }
    unit_lines = [line for line in result["limitations"] if line.startswith("Coverage counts name")]
    assert unit_lines == [
        "Coverage counts name their units: 1 collected records, 1 unique observations, "
        "0 source families, 0 verified independent origins; 1 observations carry no verified "
        "origin. None of these counts is population prevalence."
    ]
    assert result["readings"][0]["unit"] == "records"
    assert result["readings"][0]["limitations"] == [
        "This selected-record count is not population prevalence."
    ]
    assert result["snapshot_id"] == RETAINED_SNAPSHOT_ID
    assert result["admission_digest"] == canonical_digest(
        {key: value for key, value in result.items() if key != "admission_digest"}
    )
    assert json.loads(json.dumps(result)) == result


def test_origin_counts_say_whether_origin_authority_was_projected_at_all(monkeypatch):
    """The replayed capture carries no origin column, so its zero is an absence.

    A zero independent origin count over rows that never carried the field is not a
    measured zero, and the admission publishes which of the two it is beside the count, the
    way the native id projection publishes an absent column.
    """
    request, plan, intake = inputs()
    loaded, queries, captures, records = query_inputs(monkeypatch)
    result = admit(request, plan, intake, loaded, queries, captures, records)
    assert result["independent_origin_count"] == 0
    assert result["origin_authority_projection"] == {
        "state": "not_projected",
        "projected_records": 0,
        "unprojected_records": 1,
    }
    loaded, queries, captures, records = with_second_record(
        monkeypatch,
        first={"origin_id": "publisher-a", "origin_authority_digest": "b" * 64},
        second={"origin_id": None, "origin_authority_digest": None},
    )
    measured = admit(request, plan, intake, loaded, queries, captures, records)
    assert measured["independent_origin_count"] == 1
    assert measured["origin_authority_projection"] == {
        "state": "projected",
        "projected_records": 2,
        "unprojected_records": 0,
    }


def test_one_malformed_row_is_refused_by_name_and_the_other_rows_proceed(monkeypatch):
    """One row the kernel cannot identify is that row's defect, not the whole answer's.

    Over refusal that loses good data is a defect too: the malformed row is named and
    dropped, and every other admitted row keeps its receipt, its coverage and its slot.
    """
    request, plan, intake = inputs()
    loaded, queries, captures, records = with_second_record(monkeypatch, second_id=MALFORMED_ROW_ID)
    result = admit(request, plan, intake, loaded, queries, captures, records)
    assert result["observation_keys"] == [["ng", "web", "row_1"]]
    assert result["units"]["collected_records"] == 1
    assert {row["source_row_id"] for row in result["receipts"]} == {"row_1"}
    refusals = [line for line in result["limitations"] if line.startswith("Refused admitted row")]
    assert len(refusals) == 1
    assert repr(MALFORMED_ROW_ID) in refusals[0]
    assert "source_row_id" in refusals[0]


def admitted_fact(row_id, digest, **extra):
    return {
        "market": "za",
        "platform": "web",
        "row_id": row_id,
        "content_digest": digest,
        "origin_id": None,
        "origin_authority_digest": None,
        "origin_projected": False,
        **extra,
    }


def test_rows_contradicting_each_other_over_one_observation_are_refused_together():
    """Neither contradicting row is the one to keep, so both go and the other rows stay."""
    facts = [
        admitted_fact("row_1", "a"),
        admitted_fact("row_2", "b"),
        admitted_fact("row_2", "c"),
        admitted_fact("row_3", "d"),
    ]
    kept, refusals = subject._identifiable_facts(facts)
    assert [fact["row_id"] for fact in kept] == ["row_1", "row_3"]
    assert len(refusals) == 2
    assert all("content_digest" in line and "'row_2'" in line for line in refusals)
    origins = [
        admitted_fact("row_1", "a"),
        admitted_fact(
            "row_1",
            "a",
            origin_id="publisher-a",
            origin_authority_digest="b" * 64,
            origin_projected=True,
        ),
        admitted_fact(
            "row_1",
            "a",
            origin_id="publisher-b",
            origin_authority_digest="c" * 64,
            origin_projected=True,
        ),
        admitted_fact("row_9", "z"),
    ]
    kept, refusals = subject._identifiable_facts(origins)
    assert [fact["row_id"] for fact in kept] == ["row_9"]
    assert len(refusals) == 3
    assert all("origin_id" in line for line in refusals)


def test_a_malformed_row_never_reaches_the_derived_groups():
    kept, refusals = subject._identifiable_facts(
        [admitted_fact(MALFORMED_ROW_ID, "a"), admitted_fact("row_2", "b")]
    )
    assert [fact["row_id"] for fact in kept] == ["row_2"]
    assert len(refusals) == 1
    assert "source_row_id" in refusals[0]


def test_a_row_claiming_an_origin_it_cannot_prove_is_refused_without_the_rest(monkeypatch):
    request, plan, intake = inputs()
    loaded, queries, captures, records = with_second_record(
        monkeypatch, second={"origin_id": "wire-story-7 ", "origin_authority_digest": "a" * 64}
    )
    result = admit(request, plan, intake, loaded, queries, captures, records)
    assert result["observation_keys"] == [["ng", "web", "row_1"]]
    refusals = [line for line in result["limitations"] if line.startswith("Refused admitted row")]
    assert len(refusals) == 1
    assert "origin_id" in refusals[0]
    assert "'row_2'" in refusals[0]


def test_identity_policy_digest_pins_the_kernel_rules():
    expected = canonical_digest(
        {
            "policy_version": "general_question_identity_policy_v1",
            "kernel": "general_question_context_identity",
            "markets": sorted(identity.MARKETS),
            "collector_names": sorted(identity.COLLECTOR_NAMES),
            "result_fields": list(identity.RESULT_FIELDS),
            "origin_authority_digest_pattern": identity._HEX64.pattern,
        }
    )
    assert subject.identity_policy_digest() == expected
    # Pinned: a change to any kernel rule constant or the collector list moves this value.
    assert (
        subject.identity_policy_digest()
        == "6bd6fd7a7ada45354211623f4268f8c97ecbe1b7d1f05e7f3aa2cacbbcb44135"
    )


def test_syndicated_copies_share_one_verified_origin(monkeypatch):
    request, plan, intake = inputs()
    origin = {"origin_id": "wire-story-7", "origin_authority_digest": "a" * 64}
    loaded, queries, captures, records = with_second_record(
        monkeypatch, first=origin, second={**origin, "platform": "facebook"}
    )
    result = admit(request, plan, intake, loaded, queries, captures, records)
    assert result["observation_keys"] == [["ng", "facebook", "row_2"], ["ng", "web", "row_1"]]
    assert result["origin_groups"] == {
        "wire-story-7": [["ng", "facebook", "row_2"], ["ng", "web", "row_1"]]
    }
    assert result["independent_origin_count"] == 1
    assert result["unknown_origin_count"] == 0
    assert result["units"]["collected_records"] == 2
    assert result["units"]["unique_observations"] == 2
    assert result["units"]["verified_independent_origins"] == 1
    assert {row["source_row_id"] for row in result["receipts"]} == {"row_1", "row_2"}


def test_distinct_origins_count_separately_and_missing_proof_stays_unknown(monkeypatch):
    request, plan, intake = inputs()
    loaded, queries, captures, records = with_second_record(
        monkeypatch,
        first={"origin_id": "publisher-a", "origin_authority_digest": "b" * 64},
        second={"origin_id": "publisher-b", "origin_authority_digest": "c" * 64},
    )
    result = admit(request, plan, intake, loaded, queries, captures, records)
    assert result["independent_origin_count"] == 2
    assert result["unknown_origin_count"] == 0
    loaded, queries, captures, records = with_second_record(
        monkeypatch,
        first={"origin_id": "publisher-a", "origin_authority_digest": "b" * 64},
        second={"origin_id": None, "origin_authority_digest": None},
    )
    result = admit(request, plan, intake, loaded, queries, captures, records)
    assert result["independent_origin_count"] == 1
    assert result["unknown_origin_count"] == 1
    assert result["units"]["unknown_origins"] == 1


@pytest.mark.parametrize(
    "origin",
    [
        {"origin_id": "wire-story-7"},
        {"origin_id": "wire-story-7", "origin_authority_digest": "A" * 64},
        {"origin_id": "rss", "origin_authority_digest": "a" * 64},
        {"origin_authority_digest": "a" * 64},
    ],
)
def test_unverified_or_collector_origin_refuses_admission(monkeypatch, origin):
    request, plan, intake = inputs()
    loaded, queries, captures, records = query_inputs(monkeypatch, optional_metadata=origin)
    with pytest.raises(ValueError, match="protected_context_invalid"):
        admit(request, plan, intake, loaded, queries, captures, records)


def test_retained_v1_admission_bytes_validate_through_the_old_path(monkeypatch):
    request, plan, intake = inputs()
    loaded, queries, captures, records = query_inputs(monkeypatch)
    current = admit(request, plan, intake, loaded, queries, captures, records)
    retained = to_v1(current)
    assert retained["admission_digest"] == RETAINED_V1_ADMISSION_DIGEST
    assert retained["snapshot_id"] == RETAINED_SNAPSHOT_ID
    assert retained["receipts"] == current["receipts"]
    assert retained["readings"] == current["readings"]
    assert retained["provenance"] == current["provenance"]
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
    crossed = {**retained, "contract_version": "protected_context_admission_v2"}
    crossed["admission_digest"] = canonical_digest(
        {key: value for key, value in crossed.items() if key != "admission_digest"}
    )
    with pytest.raises(ValueError, match="protected_context_invalid"):
        subject.validate_stored_context_admission(crossed, **common)
    unknown = {**retained, "contract_version": "protected_context_admission_v3"}
    with pytest.raises(ValueError, match="protected_context_invalid"):
        subject.validate_stored_context_admission(unknown, **common)


def context_for(request, intake):
    return {
        "request": request,
        "intake": intake,
        "admission": {"policy_digest": request["policy_digest"], "deployment_digest": "d" * 64},
    }


def test_retained_v1_snapshot_digest_is_unchanged_and_v2_carries_the_sidecar(monkeypatch):
    request, plan, intake = inputs()
    loaded, queries, captures, records = query_inputs(monkeypatch)
    current = admit(request, plan, intake, loaded, queries, captures, records)
    retained = to_v1(current)
    v1_provenance = {
        "profile": "protected_context_v1",
        "source_binding": loaded.source_binding,
        "authority_captures": [],
        "captures": captures,
        "admission": retained,
        "limits": {},
    }
    v1 = snapshot._project_context_snapshot(
        context_for(request, intake), plan, retained, v1_provenance
    )
    assert v1["snapshot_digest"] == RETAINED_V1_SNAPSHOT_DIGEST
    assert v1["contract_version"] == "general_question_snapshot_v1"
    sidecar = snapshot._identity_sidecar(current, loaded.source_binding)
    v2_provenance = {
        **v1_provenance,
        "profile": "protected_context_v3",
        "admission": current,
        "identity_sidecar": sidecar,
    }
    v2 = snapshot._project_context_snapshot(
        context_for(request, intake), plan, current, v2_provenance
    )
    assert v2["contract_version"] == "general_question_snapshot_v1"
    assert v2["snapshot_id"] == v1["snapshot_id"] == RETAINED_SNAPSHOT_ID
    assert v2["receipts"] == v1["receipts"]
    for receipt in v2["receipts"]:
        assert set(receipt) >= {"receipt_id", "source_row_id", "content_digest"}
        assert receipt["receipt_id"] == "gqctx_" + receipt["content_digest"]
    assert v2["provenance"]["identity_sidecar"] == json.loads(json.dumps(sidecar))
    assert v2["snapshot_digest"] != v1["snapshot_digest"]


def test_identity_sidecar_binds_source_policy_and_groups(monkeypatch):
    request, plan, intake = inputs()
    loaded, queries, captures, records = query_inputs(monkeypatch)
    admission = admit(request, plan, intake, loaded, queries, captures, records)
    sidecar = snapshot._identity_sidecar(admission, loaded.source_binding)
    binding = loaded.source_binding
    assert sidecar == {
        "contract_version": "general_question_identity_sidecar_v2",
        "profile_id": binding["profile_id"],
        "source_binding_digest": binding["source_binding_digest"],
        "source_snapshot_digest": binding["snapshot_digest"],
        "cutoff_date": binding["cutoff_date"],
        "snapshot_id": admission["snapshot_id"],
        "identity_policy_digest": admission["identity_policy_digest"],
        "native_id_projection": admission["native_id_projection"],
        "observation_keys": admission["observation_keys"],
        "origin_groups": admission["origin_groups"],
        "origin_authority_projection": admission["origin_authority_projection"],
        "independent_origin_count": admission["independent_origin_count"],
        "unknown_origin_count": admission["unknown_origin_count"],
        "units": admission["units"],
        "sidecar_digest": sidecar["sidecar_digest"],
    }
    assert sidecar["sidecar_digest"] == canonical_digest(
        {key: value for key, value in sidecar.items() if key != "sidecar_digest"}
    )
    with pytest.raises(ValueError):
        snapshot._identity_sidecar(to_v1(admission), loaded.source_binding)


def test_identity_refuses_a_profile_with_no_bound_native_id_projection():
    """Each retained profile binds its own projection; an unbound one refuses rather than
    assuming the legacy shape, so a new capture cannot inherit an identity state it never
    projected."""
    with pytest.raises(ValueError, match="protected_context_invalid"):
        subject._identity([], "protected_context_unbound_v1", subject._CURRENT_ADMISSION_VERSION)
    bound, line = subject._identity(
        [], "protected_context_20260907_v1", subject._CURRENT_ADMISSION_VERSION
    )
    assert bound["native_id_projection"]["version"] == rows_module.NATIVE_ID_PROJECTION_LEGACY
    assert bound["units"] == {
        "collected_records": 0,
        "unique_observations": 0,
        "source_families": 0,
        "verified_independent_origins": 0,
        "unknown_origins": 0,
    }
    assert line.startswith("Coverage counts name their units: 0 collected records")


def test_unknown_context_profile_is_refused_before_query_replay():
    value = {
        "contract_version": "general_question_snapshot_v1",
        "provenance": {"profile": "protected_context_v3"},
    }
    value["snapshot_digest"] = canonical_digest(value)
    with pytest.raises(ValueError):
        snapshot.validate_stored_general_question_snapshot(
            value, request={"request_id": "x"}, intake={}, plan={}, query_records={}
        )


# Native id projection, D03. The retained v3 fixture schema declares no native_id column.


def test_legacy_projection_keeps_retained_material_bytes():
    material = rows_fixture.normalize(rows_fixture.rows())
    assert material["material_digest"] == RETAINED_LEGACY_MATERIAL_DIGEST
    assert "native_id_projection" not in material
    assert "native_id" not in rows_module.physical_fields("raw_content")
    assert rows_module.physical_fields("raw_content") == rows_module.physical_fields(
        "raw_content", projection_version=rows_module.NATIVE_ID_PROJECTION_LEGACY
    )
    with pytest.raises(ValueError, match="snapshot_rows_invalid"):
        rows_module.physical_fields("raw_content", projection_version="native_id_bound_v9")


def bound_metadata(declare):
    metadata = json.loads(rows_fixture.metadata_bytes())
    if declare:
        for lane in ("enriched_content", "raw_content"):
            metadata["tables"][f"trends_v2_dev.{lane}"]["schema"]["fields"].append(
                {"name": "native_id", "type": "STRING", "mode": "NULLABLE"}
            )
    return json.dumps(metadata).encode()


def bound_normalize(values, metadata):
    return rows_module.normalize_snapshot_rows(
        rows_fixture.CUTOFF,
        source_as_of=rows_fixture.SOURCE_AS_OF,
        captured_at=rows_fixture.CAPTURED_AT,
        market_scope=rows_fixture.MARKETS,
        reviewed_metadata=metadata,
        rows_by_table=values,
        projection_version=rows_module.NATIVE_ID_PROJECTION_BOUND,
    )


def test_bound_projection_distinguishes_absent_column_sql_null_and_populated_id():
    bound = rows_module.NATIVE_ID_PROJECTION_BOUND
    declared = set(rows_fixture.field_schema("raw_content")) | {"native_id"}
    assert "native_id" in rows_module.physical_fields(
        "raw_content", projection_version=bound, schema_fields=declared
    )
    assert "native_id" not in rows_module.physical_fields(
        "raw_content",
        projection_version=bound,
        schema_fields=set(rows_fixture.field_schema("raw_content")),
    )
    absent = rows_fixture.row("raw_content", row_id="absent")
    null = {**rows_fixture.row("raw_content", row_id="null"), "native_id": None}
    populated = {**rows_fixture.row("raw_content", row_id="populated"), "native_id": "post-1"}
    states = {
        name: rows_module.native_identity_state(row, projection_version=bound)
        for name, row in (("absent", absent), ("null", null), ("populated", populated))
    }
    assert states["absent"] == {
        "projection_version": bound,
        "state": "absent_column",
        "native_key": None,
        "uncertain": True,
    }
    assert states["null"] == {
        "projection_version": bound,
        "state": "sql_null",
        "native_key": None,
        "uncertain": True,
    }
    assert states["populated"] == {
        "projection_version": bound,
        "state": "populated",
        "native_key": ["reddit", "post-1"],
        "uncertain": False,
    }
    legacy = rows_module.native_identity_state(
        populated, projection_version=rows_module.NATIVE_ID_PROJECTION_LEGACY
    )
    assert legacy["state"] == "not_projected"
    assert legacy["uncertain"] is True
    with pytest.raises(ValueError, match="snapshot_rows_invalid"):
        rows_module.native_identity_state({**populated, "native_id": ""}, projection_version=bound)


def test_native_identity_groups_collapse_same_id_keep_distinct_ids_and_flag_no_id():
    bound = rows_module.NATIVE_ID_PROJECTION_BOUND
    same_a = {**rows_fixture.row("raw_content", row_id="a"), "native_id": "post-1"}
    same_b = {**rows_fixture.row("enriched_content", row_id="b"), "native_id": "post-1"}
    other = {**rows_fixture.row("raw_content", row_id="c"), "native_id": "post-2"}
    no_id = {**rows_fixture.row("raw_content", row_id="d"), "native_id": None}
    groups = rows_module.native_identity_groups(
        [no_id, other, same_b, same_a], projection_version=bound
    )
    assert groups == [
        {
            "native_key": ["reddit", "post-1"],
            "uncertain": False,
            "members": [["za", "a"], ["za", "b"]],
        },
        {"native_key": ["reddit", "post-2"], "uncertain": False, "members": [["za", "c"]]},
        {"native_key": None, "uncertain": True, "members": [["za", "d"]]},
    ]
    assert groups == rows_module.native_identity_groups(
        [same_a, same_b, other, no_id], projection_version=bound
    )
    from src.analysis.open_intelligence.candidates import deduplicate_wave1_content

    survivors = deduplicate_wave1_content(
        [{**row, "row_id": row["id"], "url": row["id"]} for row in (same_a, same_b, other)]
    )
    assert [row["id"] for row in survivors] == ["a", "c"]
    legacy = rows_module.native_identity_groups(
        [same_a, same_b], projection_version=rows_module.NATIVE_ID_PROJECTION_LEGACY
    )
    assert [group["uncertain"] for group in legacy] == [True, True]


def test_bound_projection_normalizes_new_profile_rows_and_retains_column_provenance():
    values = rows_fixture.rows()
    values["raw_content"] = [{**values["raw_content"][0], "native_id": "post-raw"}]
    values["enriched_content"] = [{**row, "native_id": None} for row in values["enriched_content"]]
    material = bound_normalize(values, bound_metadata(True))
    assert material["native_id_projection"] == {
        "version": rows_module.NATIVE_ID_PROJECTION_BOUND,
        "source_columns": {"enriched_content": "present", "raw_content": "present"},
    }
    assert material["physical_rows_by_table"]["raw_content"][0]["native_id"] == "post-raw"
    assert material["adapted_rows_by_table"]["raw_content"][0]["native_id"] == "post-raw"
    assert material["physical_rows_by_table"]["enriched_content"][0]["native_id"] is None
    assert material["material_digest"] != RETAINED_LEGACY_MATERIAL_DIGEST
    absent = bound_normalize(rows_fixture.rows(), bound_metadata(False))
    assert absent["native_id_projection"]["source_columns"] == {
        "enriched_content": "absent",
        "raw_content": "absent",
    }
    assert "native_id" not in absent["physical_rows_by_table"]["raw_content"][0]
    assert absent["adapted_rows_by_table"]["raw_content"][0]["native_id"] is None
    legacy = rows_fixture.normalize(rows_fixture.rows())
    assert absent["physical_table_digests"] == legacy["physical_table_digests"]
    assert absent["adapted_table_digests"] == legacy["adapted_table_digests"]
    with pytest.raises(ValueError, match="snapshot_rows_invalid"):
        rows_fixture.normalize(values)
    with pytest.raises(ValueError, match="snapshot_rows_invalid"):
        bound_normalize(values, bound_metadata(False))


def test_one_row_with_an_origin_beside_one_without_is_kept_over_the_same_observation():
    """An absent origin never contradicts a present one, so the pair is kept, not refused.

    A raw row and an enriched row of one observation routinely differ in whether the origin
    was projected at all. Treating the absent one as a second claim would refuse both and
    lose an established origin to the copy that simply did not carry the column.
    """
    facts = [
        admitted_fact("row_1", "a"),
        admitted_fact(
            "row_1",
            "a",
            origin_id="publisher-a",
            origin_authority_digest="b" * 64,
            origin_projected=True,
        ),
    ]
    kept, refusals = subject._identifiable_facts(facts)
    assert [fact["row_id"] for fact in kept] == ["row_1", "row_1"]
    assert refusals == []


def test_a_contested_observation_is_refused_even_when_one_claimant_was_already_refused():
    """A row refused on its own terms still claims its observation, so the sweep sees it.

    Sweeping only the rows that individually validated lets a contested observation through
    on its surviving claimant: the second row is refused for its origin, and the first would
    otherwise reach the answer as an uncontested single reading of that observation.
    """
    facts = [
        admitted_fact("row_1", "a"),
        admitted_fact(
            "row_1",
            "b",
            origin_id="Wire-Story-7",
            origin_authority_digest="c" * 64,
            origin_projected=True,
        ),
        admitted_fact("row_9", "z"),
    ]
    kept, refusals = subject._identifiable_facts(facts)
    assert [fact["row_id"] for fact in kept] == ["row_9"]
    assert len(refusals) == 2
    assert any("origin_id" in line for line in refusals)
    assert any("content_digest" in line and "claimed two ways" in line for line in refusals)


def test_a_row_refused_on_its_own_terms_still_contests_its_observation_through_an_origin():
    """Two origins over one observation, where one of the two rows is itself unreadable."""
    facts = [
        admitted_fact(
            "row_1",
            "a",
            origin_id="publisher-a",
            origin_authority_digest="b" * 64,
            origin_projected=True,
        ),
        admitted_fact(
            "row_1",
            "a",
            origin_id="publisher-b",
            origin_authority_digest="not-a-digest",
            origin_projected=True,
        ),
    ]
    kept, refusals = subject._identifiable_facts(facts)
    assert kept == []
    assert len(refusals) == 2
    assert any("origin_id" in line and "claimed two ways" in line for line in refusals)


def test_a_row_whose_observation_key_cannot_be_read_contests_nothing():
    """A row with no readable key claims no observation, so it refuses alone."""
    facts = [admitted_fact(MALFORMED_ROW_ID, "a"), admitted_fact("row_2", "b")]
    kept, refusals = subject._identifiable_facts(facts)
    assert [fact["row_id"] for fact in kept] == ["row_2"]
    assert len(refusals) == 1


def test_row_refusal_lines_are_capped_and_the_remainder_is_published_as_a_count():
    """Two hundred refused rows must not take the answer down with them.

    The answer refuses a snapshot carrying more than two hundred limitations, so an
    uncapped line per refused row turns ordinary malformed producer data into a lost
    answer, after the snapshot is written and the model call is already paid for.
    """
    facts = [admitted_fact(f"{MALFORMED_ROW_ID}_{index}", "a") for index in range(500)]
    kept, refusals = subject._identifiable_facts(facts)
    assert kept == []
    assert len(refusals) == subject._ROW_REFUSAL_LIMIT + 1
    assert subject._ROW_REFUSAL_LIMIT < 200
    assert all(line.startswith("Refused admitted row") for line in refusals[:-1])
    assert refusals[-1] == (
        f"Refused {500 - subject._ROW_REFUSAL_LIMIT} further admitted rows whose identity the "
        "kernel cannot establish, beyond the "
        f"{subject._ROW_REFUSAL_LIMIT} named above. The remaining admitted rows proceed."
    )
    exact = [admitted_fact(f"{MALFORMED_ROW_ID}_{index}", "a") for index in range(20)]
    _kept, at_limit = subject._identifiable_facts(exact)
    assert len(at_limit) == 20
    assert all(line.startswith("Refused admitted row") for line in at_limit)


def test_a_capped_refusal_list_keeps_the_answer_inside_its_limitation_ceiling():
    """The cap is what keeps a whole admission of malformed rows inside the answer cap."""
    from src.analysis.open_intelligence import general_question_answer as answer

    facts = [admitted_fact(f"{MALFORMED_ROW_ID}_{index}", "a") for index in range(400)]
    _kept, refusals = subject._identifiable_facts(facts)
    answer._strings(refusals)
    assert len(refusals) < 200


def to_v2(admission):
    """The retained v2 shape: no origin authority projection, v2 contract, digest recomputed."""
    value = {
        key: copy.deepcopy(item)
        for key, item in admission.items()
        if key not in ("origin_authority_projection", "admission_digest")
    }
    value["contract_version"] = "protected_context_admission_v2"
    value["admission_digest"] = canonical_digest(value)
    return value


def test_retained_v2_admission_bytes_validate_through_their_own_version(monkeypatch):
    """A record written before the projection field existed is still readable.

    The field was added to the v2 field set while the version string stayed v2, so every
    retained v2 admission was refused for a field that did not exist when it was written.
    The field belongs to v3; v2 keeps the shape it was stored with, byte for byte.
    """
    request, plan, intake = inputs()
    loaded, queries, captures, records = query_inputs(monkeypatch)
    current = admit(request, plan, intake, loaded, queries, captures, records)
    assert current["contract_version"] == "protected_context_admission_v3"
    retained = to_v2(current)
    assert "origin_authority_projection" not in retained
    assert set(retained) == subject._ADMISSION_VERSIONS["protected_context_admission_v2"]
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
    assert retained["receipts"] == current["receipts"]
    assert retained["readings"] == current["readings"]
    assert retained["limitations"] == current["limitations"]
    assert retained["observation_keys"] == current["observation_keys"]
    crossed = {**retained, "contract_version": "protected_context_admission_v3"}
    with pytest.raises(ValueError, match="protected_context_invalid"):
        subject.validate_stored_context_admission(crossed, **common)
    padded = {**current, "contract_version": "protected_context_admission_v2"}
    with pytest.raises(ValueError, match="protected_context_invalid"):
        subject.validate_stored_context_admission(padded, **common)


def test_a_retained_v2_snapshot_keeps_its_sidecar_shape_and_still_reads(monkeypatch):
    """The v2 sidecar rebuilds as a v2 sidecar, and its support reads without the field."""
    request, plan, intake = inputs()
    loaded, queries, captures, records = query_inputs(monkeypatch)
    current = admit(request, plan, intake, loaded, queries, captures, records)
    retained = to_v2(current)
    sidecar = snapshot._identity_sidecar(retained, loaded.source_binding)
    assert sidecar["contract_version"] == "general_question_identity_sidecar_v1"
    assert "origin_authority_projection" not in sidecar
    stored = snapshot._project_context_snapshot(
        context_for(request, intake),
        plan,
        retained,
        {
            "profile": "protected_context_v2",
            "source_binding": loaded.source_binding,
            "authority_captures": [],
            "captures": captures,
            "admission": retained,
            "identity_sidecar": sidecar,
            "limits": {},
        },
    )
    support = snapshot.read_context_evidence_support(
        stored, [row["receipt_id"] for row in stored["receipts"]]
    )
    assert support["origin_authority_projection"] == {
        "state": "not_recorded",
        "projected_records": None,
        "unprojected_records": None,
    }
    assert support["units"]["verified_independent_origins"] == 0
    assert any(
        line.startswith("Origin authority is not projected") for line in support["limitations"]
    )


def test_an_admission_whose_projection_denies_its_own_origin_groups_refuses(monkeypatch):
    """A caveat that contradicts the count beside it cannot both be published and be true."""
    request, plan, intake = inputs()
    loaded, queries, captures, records = query_inputs(monkeypatch)
    current = admit(request, plan, intake, loaded, queries, captures, records)
    contested = copy.deepcopy(current)
    contested["origin_groups"] = {"wire-story-7": contested["observation_keys"]}
    contested["independent_origin_count"] = 1
    assert contested["origin_authority_projection"]["state"] == "not_projected"
    with pytest.raises(ValueError, match="context_identity_invalid"):
        snapshot._identity_sidecar(contested, loaded.source_binding)
    agreeing = copy.deepcopy(contested)
    agreeing["origin_authority_projection"] = {
        "state": "projected",
        "projected_records": 1,
        "unprojected_records": 0,
    }
    assert snapshot._identity_sidecar(agreeing, loaded.source_binding)["origin_groups"] == {
        "wire-story-7": contested["observation_keys"]
    }


@pytest.mark.parametrize("field", IDENTITY_FIELDS)
def test_an_admission_missing_one_sidecar_field_refuses_by_name(monkeypatch, field):
    """A field the sidecar needs and the admission lacks is a named refusal, not a key error."""
    request, plan, intake = inputs()
    loaded, queries, captures, records = query_inputs(monkeypatch)
    admission = admit(request, plan, intake, loaded, queries, captures, records)
    short = {key: value for key, value in admission.items() if key != field}
    with pytest.raises(ValueError, match="context_identity_invalid"):
        snapshot._identity_sidecar(short, loaded.source_binding)


def test_a_stored_admission_of_the_wrong_shape_refuses_before_its_fields_are_read(monkeypatch):
    """The field set is checked first, so a truncated record refuses by name.

    Reaching into provenance before the shape is established turns a malformed retained
    record into a key error out of a module whose refusal is protected_context_invalid.
    """
    request, plan, intake = inputs()
    _loaded, queries, captures, records = query_inputs(monkeypatch)
    common = {
        "request": request,
        "plan": plan,
        "intake": intake,
        "queries": queries,
        "captures": captures,
        "query_records": records,
    }
    for value in (
        {"contract_version": "protected_context_admission_v3"},
        {"contract_version": "protected_context_admission_v2", "receipts": []},
        {"contract_version": "protected_context_admission_v1"},
    ):
        with pytest.raises(ValueError, match="protected_context_invalid"):
            subject.validate_stored_context_admission(value, **common)


def test_a_retained_v1_snapshot_carries_no_identity_sidecar_through_the_shape_gate():
    """Only an identity profile owes a sidecar; a v1 profile that has none still reads.

    The provenance key set is exact, so requiring the sidecar of every profile would refuse
    snapshots using the retained identity profile. The fixture carries an explicit cap
    version: unmarked caps have a separate deployment compatibility gate. With no sidecar,
    this fixture must reach the parent capsule check beyond the shape gate.
    """
    request, plan, intake = inputs()
    value = {
        "deployment_digest": "0" * 64,
        "provenance": {
            "profile": "protected_context_v1",
            "cap_version": 1,
            "source_binding": {},
            "authority_captures": [],
            "captures": {},
            "admission": {"contract_version": "protected_context_admission_v1"},
            "limits": {},
            "parent_context": {"contract_version": "wrong"},
        },
    }
    with pytest.raises(QuestionStoreError, match="parent_context_invalid"):
        snapshot._validate_stored_context_snapshot(
            value,
            request,
            {**intake, "parent_context_ref": {}},
            plan,
            {},
        )
