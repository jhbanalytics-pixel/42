import copy
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from src.analysis.open_intelligence import general_question_context_admission as subject
from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.general_question_plan import validate_question_plan
from src.analysis.open_intelligence.general_question_policy import build_intake_context
from src.analysis.open_intelligence.general_question_request import normalize_question_request

from tests.unit.test_general_question_retrieval_queries import inputs


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "market",
        "date",
        "blank",
        "irrelevant",
        "identity",
        "receipt",
        "overflow",
        "foreign_local",
        "all_foreign",
        "empty",
        "empty_bad_receipt",
        "empty_bad_schema",
        "empty_failed_query",
        "empty_overflow",
    ],
)
@pytest.mark.parametrize("geo_policy", [False, True])
def test_seed_and_supplement_rows_pass_same_bound_admission(mutation, geo_policy):
    from tests.unit import test_general_question_context_queries as wires

    builder = wires.subject()
    args = wires.inputs()
    request, plan, intake, binding = args
    binding["source_binding_digest"] = canonical_digest(binding)
    candidates = wires.candidate_rows(binding)
    partitions = [
        {"lane": "__metadata__", "partition_date": None, "partition_count": 1},
        {"lane": "enriched_content", "partition_date": "2026-09-07", "partition_count": 1},
    ]
    indexes = [wires.metadata(binding, 2, 556 if mutation == "overflow" else 2, evidence=True)]
    evidence = [wires.metadata(binding, 2, 2, evidence=True)]
    for identifier, platform in (("one", "reddit"), ("direct_other", "tiktok")):
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
        payload = {
            "id": identifier,
            "market": "za",
            "source": platform,
            "platform": platform,
            "author_handle": None,
            "url": "https://example.test/" + identifier,
            "text": "Night commute observations from this source.",
            "title": None,
            "collected_at": "2026-09-07T01:00:00+00:00",
            "published_at": "2026-09-07T00:00:00+00:00",
        }
        if identifier == "direct_other":
            if mutation == "market":
                payload["market"] = "ng"
            if mutation == "date":
                payload["published_at"] = "2026-08-01T00:00:00+00:00"
            if mutation == "blank":
                payload["text"] = "   "
            if mutation == "irrelevant":
                payload["text"] = "Unrelated source subject."
            if mutation == "identity":
                payload["id"] = "unindexed"
        if mutation == "all_foreign" or (
            mutation == "foreign_local" and identifier == "direct_other"
        ):
            payload.update(
                platform="reddit",
                source="reddit",
                url="https://www.reddit.com/r/VirginiaTech/comments/1w7hzi3/labor_day_weekend_rundown_of_local_fun_events/",
                text="Virginia Tech local events in Blacksburg this weekend include night commute specials.",
            )
        evidence.append(
            {
                "lane": "enriched_content",
                "market": payload["market"],
                "id": payload["id"],
                "match_count": 1,
                "payload": json.dumps(payload),
            }
        )
    if mutation and mutation.startswith("empty"):
        indexes = [
            wires.metadata(binding, 0, 3 if mutation == "empty_overflow" else 0, evidence=True)
        ]
        evidence = [wires.metadata(binding, 0, 0, evidence=True)]
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
            "geo_policy": geo_policy,
            **({"enriched_rows": evidence} if key == "raw" else {}),
        }
        rows[key + "_index"] = (
            indexes if key == "enriched" else [wires.metadata(binding, 0, 0, evidence=True)]
        )
        rows[key] = (
            evidence if key == "enriched" else [wires.metadata(binding, 0, 0, evidence=True)]
        )
        queries[key + "_index"] = builder.build_context_index_query(*args, **common)
        queries[key] = builder.build_context_evidence_query(
            *args, index_rows=rows[key + "_index"], **common
        )
    if mutation == "empty_bad_schema":
        payload = json.loads(evidence[0]["payload"])
        payload["unexpected"] = True
        evidence[0]["payload"] = json.dumps(payload)
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
    if mutation in {"receipt", "empty_bad_receipt"}:
        records["enriched"]["receipt"]["result_digest"] = "0" * 64
    if mutation == "empty_failed_query":
        records["enriched"]["receipt"]["query_state"] = "failed"
        captures["enriched"]["receipt"]["query_state"] = "failed"
    loaded = subject.LoadedProtectedContext(binding["profile_id"], {}, binding)

    def admit():
        return subject.admit_protected_context(
            request,
            plan,
            intake,
            source=loaded,
            queries=queries,
            captures=captures,
            query_records=records,
        )

    if mutation == "empty":
        with pytest.raises(subject.NoMatchingContextEvidence):
            admit()
    elif mutation and mutation.startswith("empty"):
        with pytest.raises(ValueError) as raised:
            admit()
        assert not isinstance(raised.value, subject.NoMatchingContextEvidence)
    elif mutation == "all_foreign" and geo_policy:
        with pytest.raises(subject.ForeignLocalEvidenceOnly) as caught:
            admit()
        assert caught.value.count == 2
    elif mutation and mutation not in {"overflow", "foreign_local", "all_foreign"}:
        with pytest.raises(ValueError):
            admit()
    else:
        result = admit()
        assert {row["platform"] for row in result["receipts"]} == (
            {"reddit"} if mutation in {"foreign_local", "all_foreign"} else {"reddit", "tiktok"}
        )
        assert len(result["receipts"]) == (1 if geo_policy and mutation == "foreign_local" else 2)
        if geo_policy and mutation == "foreign_local":
            assert any(
                "Excluded 1 corroborated foreign-local" in item for item in result["limitations"]
            )
        assert result["fulfilled_requirement_ids"] == ["context"]
        if mutation == "overflow":
            assert (
                "Enriched_index selection was truncated to 2 of 556 matching records."
                in result["limitations"]
            )
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


def source():
    binding = {
        "profile_id": "protected_context_20260907_v1",
        "source_relation_set_id": "trends_v2_dev_table_snapshot_v1",
        "operation": "source_snapshot_capture",
        "manifest_sha256": "a" * 64,
        "consumption_id": "exc_" + "b" * 64,
        "result_id": "exr_" + "c" * 64,
        "result_digest": "d" * 64,
        "approval_id": "exa_" + "e" * 64,
        "execution_name": "projects/p/locations/us/jobs/j/executions/x",
        "source_sha": "f" * 40,
        "image_uri": "image",
        "stored_artifact": {
            "uri": "gs://bucket/capture.json",
            "generation": 1,
            "size_bytes": 1,
            "sha256": "1" * 64,
            "created_at": "2026-09-08T00:06:00+00:00",
        },
        "cutoff_date": "2026-09-07",
        "source_as_of": "2026-09-08T00:00:00+00:00",
        "captured_at": "2026-09-08T00:05:00+00:00",
        "snapshot_digest": "2" * 64,
        "capture_receipt_digest": "3" * 64,
        "snapshot_plan_digest": "4" * 64,
        "client_scope_id": "synthetic_scope",
        "market_scope": ["ke", "ng", "za"],
        "snapshot_tables": [],
    }
    binding["source_binding_digest"] = canonical_digest(binding)
    return subject.LoadedProtectedContext(
        binding["profile_id"], {"assembly": {"row_material": {}}}, binding
    )


def query_inputs(
    monkeypatch,
    *,
    published_at="2026-08-25T10:00:00+00:00",
    text="Night commute rituals now include shared taxi queues.",
    optional_metadata=None,
):
    loaded = source()
    materials = {
        "candidate": {
            "candidate_count": 1,
            "full_matching_count": 1,
            "overflow": False,
            "records": [
                {
                    "lane": "seed_graph",
                    "market": "ng",
                    "candidate_key": "night commute",
                    "sample_row_ids": ["row_1"],
                    "payload": {},
                }
            ],
        },
        "enriched": {
            "candidate_count": 1,
            "full_matching_count": 1,
            "overflow": False,
            "records": [
                {
                    "lane": "enriched_content",
                    "market": "ng",
                    "id": "row_1",
                    "match_count": 1,
                    "payload": {
                        "id": "row_1",
                        "market": "ng",
                        "source": "news",
                        "source_family": None,
                        "platform": "web",
                        "author_handle": None,
                        "url": "https://example.invalid/row-1",
                        "title": "Night commute rituals",
                        "text": text,
                        "published_at": published_at,
                        "collected_at": "2026-08-25T11:00:00+00:00",
                    },
                }
            ],
        },
        "raw": {
            "candidate_count": 0,
            "full_matching_count": 0,
            "overflow": False,
            "records": [],
        },
    }
    if optional_metadata is not None:
        materials["enriched"]["records"][0]["payload"].update(optional_metadata)
    for key in ("enriched_index", "raw_index"):
        materials[key] = {
            "candidate_count": 0,
            "full_matching_count": 0,
            "overflow": False,
            "records": [],
        }
    monkeypatch.setattr(subject, "_decode_context_rows", lambda rows, query: materials[query.key])
    queries, captures, records = {}, {}, {}
    for key in subject._CAPTURE_KEYS:
        template = subject._TEMPLATES[key]
        query = SimpleNamespace(
            key=key,
            template_id=template,
            sql_digest=key[0] * 64,
            parameters_digest=key[-1] * 64,
            source_binding_digest=loaded.source_binding["source_binding_digest"],
            source_snapshot_digest=loaded.source_binding["snapshot_digest"],
            profile_id=loaded.profile_id,
        )
        rows = [{"synthetic": key}]
        receipt = {
            "job_id": "job_" + key,
            "template_id": template,
            "sql_digest": query.sql_digest,
            "parameters_digest": query.parameters_digest,
            "query_state": "succeeded",
            "result_digest": canonical_digest(rows),
        }
        queries[key] = query
        captures[key] = {"rows": rows, "receipt": receipt}
        records[key] = {
            "reservation": {
                "template_id": template,
                "sql_digest": query.sql_digest,
                "parameters_digest": query.parameters_digest,
            },
            "receipt": copy.deepcopy(receipt),
        }
    return loaded, queries, captures, records


def current_context(
    *, client_scope_id="synthetic_scope", market_scope=("ke", "ng", "za"), selected_market="ng"
):
    request = normalize_question_request(
        {"message": "How are night commute rituals changing?"},
        scope={
            "client_scope_id": client_scope_id,
            "market_scope": list(market_scope),
            "brand_config_id": None,
            "audience_lens_ids": [],
            "theme_id": None,
        },
        request_id="00000000-0000-4000-8000-000000000099",
        admitted_at=datetime(2026, 9, 9, tzinfo=UTC),
        policy_digest="a" * 64,
        requested_window={"start": "2026-09-01", "end": "2026-09-07"},
    )
    intake = build_intake_context(request, selected_market=selected_market)
    plan = validate_question_plan(
        {
            "status": "ready",
            "intent": "discovery",
            "markets": [selected_market],
            "window": {"start": "2026-09-01", "end": "2026-09-07", "closed": True},
            "decision": None,
            "requirements": [
                {
                    "requirement_id": "mobility",
                    "question": "Which night commute rituals matter?",
                    "kind": "content",
                    "mandatory": True,
                    "search_terms": ["night commute"],
                }
            ],
            "clarification": None,
            "limitations": [],
        },
        request=request,
        intake=intake,
    )
    return request, plan, intake


def test_loader_uses_only_code_pin_scoped_readers_and_current_native_metadata(monkeypatch):
    request, plan, intake = current_context(client_scope_id="ogilvy_default")
    loaded = source()
    profile = {
        key: loaded.source_binding[key]
        for key in (
            "profile_id",
            "cutoff_date",
            "manifest_sha256",
            "consumption_id",
            "result_id",
            "result_digest",
        )
    }
    profile.update(
        {
            key: actual_capture_result()[key]
            for key in ("manifest_sha256", "consumption_id", "result_id", "result_digest")
        }
    )
    table = {
        "lane": "event_ledger",
        "snapshot_table": "ogilvy-trends-v2.trends_v2_staging.snapshot_event_ledger",
        "snapshot_time": "2026-09-08T00:00:00+00:00",
        "schema_digest": "5" * 64,
        "row_count": 1,
        "physical_result_digest": "6" * 64,
        "adapted_result_digest": "7" * 64,
    }
    completed = {
        "capture": {"assembly": {"snapshot": {"table_snapshot_receipts": [table]}}},
        "binding": {
            key: copy.deepcopy(value)
            for key, value in loaded.source_binding.items()
            if key
            not in {
                "profile_id",
                "source_relation_set_id",
                "snapshot_tables",
                "source_binding_digest",
            }
        },
    }
    observed = {}
    completed["binding"].update(
        {key: value for key, value in profile.items() if key != "profile_id"}
    )
    completed["binding"]["client_scope_id"] = "ogilvy_default"
    monkeypatch.setattr(subject, "_PROTECTED_CONTEXT_PROFILES", (profile,))
    monkeypatch.setattr(
        subject,
        "_read_protected_capture",
        lambda **kwargs: observed.update(kwargs) or completed,
    )
    metadata_checks = []
    monkeypatch.setattr(
        subject,
        "_validate_live_snapshot_metadata",
        lambda prepared, delegate: metadata_checks.append((prepared, delegate)),
    )
    result_reader, approval_reader = [actual_capture_result()], object()

    native_client = object()
    result = subject.load_protected_context_source(
        request,
        plan,
        intake,
        objects=object(),
        native_client=native_client,
        result_reader=lambda value: result_reader,
        approval_reader=lambda value: approval_reader,
        now=datetime(2026, 9, 9, 0, 1, tzinfo=UTC),
    )

    assert result.source_binding["snapshot_tables"] == [table]
    assert metadata_checks == [(completed["capture"]["assembly"], native_client)]
    assert observed["result_reader"] is not None
    assert observed["approval_reader"] is not None


def test_contextual_native_rows_outside_original_bounded_capture_become_content_only(monkeypatch):
    request, plan, intake = inputs()
    loaded, queries, captures, records = query_inputs(monkeypatch)

    result = subject.admit_protected_context(
        request,
        plan,
        intake,
        source=loaded,
        queries=queries,
        captures=captures,
        query_records=records,
    )

    assert result["contract_version"] == "protected_context_admission_v3"
    assert result["fulfilled_requirement_ids"] == ["mobility"]
    assert len(result["receipts"]) == len(result["readings"]) == 1
    assert result["receipts"][0]["published_at"] == "2026-08-25T10:00:00+00:00"
    assert result["receipts"][0]["collected_at"] == "2026-08-25T11:00:00+00:00"
    assert result["receipts"][0]["source_family"] is None
    assert result["readings"][0]["method"] == "selected_receipt_count"
    assert result["readings"][0]["value"] == 1
    assert "rate" not in repr(result).lower()
    assert "signal_id" not in repr(result)


def test_missing_publication_time_stays_null_with_explicit_limitation(monkeypatch):
    request, plan, intake = inputs()
    loaded, queries, captures, records = query_inputs(monkeypatch, published_at=None)
    result = subject.admit_protected_context(
        request,
        plan,
        intake,
        source=loaded,
        queries=queries,
        captures=captures,
        query_records=records,
    )
    receipt = result["receipts"][0]
    assert receipt["published_at"] is None
    assert receipt["collected_at"] == "2026-08-25T11:00:00+00:00"
    assert receipt["limitations"][-1].startswith("Publication time is unavailable")


def test_broad_overflow_and_long_source_text_admit_bounded_supported_portion(monkeypatch):
    request, plan, intake = inputs()
    loaded, queries, captures, records = query_inputs(
        monkeypatch, text="Night commute " + "x" * 20000
    )
    decoder = subject._decode_context_rows
    monkeypatch.setattr(
        subject,
        "_decode_context_rows",
        lambda rows, query: (
            {
                **decoder(rows, query),
                "candidate_count": 1,
                "full_matching_count": 50,
                "overflow": query.key == "candidate",
            }
            if query.key == "candidate"
            else decoder(rows, query)
        ),
    )
    result = subject.admit_protected_context(
        request,
        plan,
        intake,
        source=loaded,
        queries=queries,
        captures=captures,
        query_records=records,
    )
    assert result["readings"][0]["value"] == 1
    assert result["limitations"][-2] == (
        "Candidate selection was truncated to 1 of 50 matching records."
    )
    assert result["limitations"][-1].startswith("Coverage counts name their units: 1 collected")
    assert len(result["receipts"][0]["excerpt"]) == 4000
    assert result["receipts"][0]["limitations"][-1].startswith("The source excerpt was truncated")


def test_stored_revalidation_uses_all_owned_query_rows_and_receipts(monkeypatch):
    request, plan, intake = inputs()
    loaded, queries, captures, records = query_inputs(monkeypatch)
    result = subject.admit_protected_context(
        request,
        plan,
        intake,
        source=loaded,
        queries=queries,
        captures=captures,
        query_records=records,
    )
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
    captures["enriched"]["rows"][0]["synthetic"] = "changed"
    with pytest.raises(ValueError, match="protected_context_invalid"):
        subject.validate_stored_context_admission(
            result,
            request=request,
            plan=plan,
            intake=intake,
            queries=queries,
            captures=captures,
            query_records=records,
        )


@pytest.mark.parametrize("mutation", ["foreign", "raw_fallback", "unowned"])
def test_scope_fallback_truncation_and_unowned_results_refuse(monkeypatch, mutation):
    request, plan, intake = inputs()
    loaded, queries, captures, records = query_inputs(monkeypatch)
    material = subject._decode_context_rows
    if mutation == "foreign":
        original = material
        monkeypatch.setattr(
            subject,
            "_decode_context_rows",
            lambda rows, query: {
                **original(rows, query),
                "records": [{**original(rows, query)["records"][0], "market": "za"}]
                if query.key == "enriched"
                else original(rows, query)["records"],
            },
        )
    elif mutation == "raw_fallback":
        enriched = material(captures["enriched"]["rows"], queries["enriched"])
        raw = {
            **enriched,
            "records": [{**enriched["records"][0], "lane": "raw_content"}],
        }
        monkeypatch.setattr(
            subject,
            "_decode_context_rows",
            lambda rows, query: raw if query.key == "raw" else material(rows, query),
        )
    else:
        records["enriched"]["receipt"]["result_digest"] = "f" * 64
    with pytest.raises(ValueError, match="protected_context_invalid"):
        subject.admit_protected_context(
            request,
            plan,
            intake,
            source=loaded,
            queries=queries,
            captures=captures,
            query_records=records,
        )


@pytest.mark.parametrize("wrong_identity", [False, True])
def test_default_profile_selects_actual_validated_capture(monkeypatch, wrong_identity):
    expected = {
        "consumption_id": "exc_ae2164ad35aaef39e92003466e32d65d434b57148184b8d91343116c0339aac4",
        "cutoff_date": "2026-09-07",
        "manifest_sha256": "525202e9a55abad9efd2c757f1247f3ded070361be4462f3e93afbb10128f156",
        "profile_id": "protected_context_20260907_v1",
        "result_digest": "2ee1ad49e17745f8ff42f71fcd061818b5bb8af692746706a9962e03238d9ff3",
        "result_id": "exr_b1e13578bac9fd5a070bb8eb2c8a2ecd956c9912584f04465fb6a53c64c5b1bd",
    }
    assert (expected,) == subject._PROTECTED_CONTEXT_PROFILES
    request, plan, intake = current_context(
        client_scope_id="ogilvy_default", market_scope=("za",), selected_market="za"
    )
    binding = {**source().source_binding, **expected}
    binding["client_scope_id"] = "ogilvy_default"
    if wrong_identity:
        binding["manifest_sha256"] = "f" * 64
    observed = {}
    completed = {
        "binding": binding,
        "capture": {"assembly": {"snapshot": {"table_snapshot_receipts": []}}},
    }

    def read_capture(**kwargs):
        observed.update(kwargs)
        assert kwargs["result_reader"](expected["consumption_id"]) == [actual_capture_result()]
        return completed

    monkeypatch.setattr(subject, "_read_protected_capture", read_capture)
    metadata_clients = []
    monkeypatch.setattr(
        subject,
        "_validate_live_snapshot_metadata",
        lambda prepared, delegate: metadata_clients.append(delegate),
    )
    metadata_reads = []
    kwargs = {
        "objects": object(),
        "native_client": object(),
        "result_reader": lambda cid: metadata_reads.append(cid) or [actual_capture_result()],
        "approval_reader": lambda _: None,
        "now": datetime(2026, 9, 9, tzinfo=UTC),
    }
    if wrong_identity:
        with pytest.raises(ValueError, match="protected_context_invalid"):
            subject.load_protected_context_source(request, plan, intake, **kwargs)
    else:
        result = subject.load_protected_context_source(request, plan, intake, **kwargs)
        assert result.profile_id == expected["profile_id"]
        assert metadata_clients == [kwargs["native_client"]]
    assert observed["market_scope"] == ["ke", "ng", "za"]
    assert metadata_reads == [expected["consumption_id"]]
    assert {
        key: observed[key]
        for key in ("manifest_sha256", "consumption_id", "result_id", "result_digest")
    } == {
        key: expected[key]
        for key in ("manifest_sha256", "consumption_id", "result_id", "result_digest")
    }


@pytest.mark.parametrize(
    "mutation", ["tuple", "digest", "status", "cutoff", "client", "foreign_market"]
)
def test_loader_rejects_unbound_or_out_of_scope_capture_before_source_read(monkeypatch, mutation):
    from src.analysis.open_intelligence import execution_approval
    from src.analysis.open_intelligence.brain_contract import canonical_bytes

    row = actual_capture_result()
    profile = copy.deepcopy(subject._PROTECTED_CONTEXT_PROFILES[0])
    request, plan, intake = current_context(
        client_scope_id="ogilvy_default", market_scope=("za",), selected_market="za"
    )
    if mutation == "tuple":
        row["consumption_id"] = "exc_" + "f" * 64
    elif mutation == "digest":
        row["result_digest"] = "f" * 64
    else:
        payload = json.loads(row["canonical_result_json"])
        if mutation == "status":
            row["status"] = "failed"
        elif mutation == "cutoff":
            payload["cutoff_date"] = "2026-09-08"
        elif mutation == "client":
            payload["client_scope_id"] = "foreign_scope"
        else:
            payload["market_scope"] = ["ng"]
        row["canonical_result_json"] = canonical_bytes(payload).decode()
        row["result_digest"] = canonical_digest(payload)
        row["result_id"] = execution_approval.result_id(
            row["consumption_id"],
            row["result_reference"],
            row["result_digest"],
            row["status"],
            row["completed_at"],
        )
        profile.update(result_id=row["result_id"], result_digest=row["result_digest"])
    monkeypatch.setattr(subject, "_PROTECTED_CONTEXT_PROFILES", (profile,))
    calls = []
    monkeypatch.setattr(subject, "_read_protected_capture", lambda **kwargs: calls.append(kwargs))
    with pytest.raises(ValueError, match="protected_context_invalid"):
        subject.load_protected_context_source(
            request,
            plan,
            intake,
            objects=object(),
            native_client=object(),
            result_reader=lambda _: [row],
            approval_reader=lambda _: None,
            now=datetime(2026, 9, 9, tzinfo=UTC),
        )
    assert calls == []


def actual_capture_result():
    row = {
        "approval_id": "exa_31348736c5f4cea95ce115a4725322eec3f35043f465709d0083e382a0ccc58f",
        "canonical_result_json": '{"artifact_attempt":{"captured_at":"2026-09-08T15:44:24.790188+00:00","sha256":"6fb2abd2d37025b064cf3fc6604fa97fda14230644a40a08f5a9faf035a841f3","size_bytes":63731275,"uri":"gs://ogilvy-trends-v2-oi-source-artifacts-staging/captures/8064a2d1a641ba1e61edca95b8db2a98846d12936baf51efdfdedcdeeda48de9/6fb2abd2d37025b064cf3fc6604fa97fda14230644a40a08f5a9faf035a841f3/capture.json"},"capture_receipt_digest":"3818f45819e56986d9965ebfc9e0f682e9b231f91cad8cd8726d4cb640be3493","captured_at":"2026-09-08T15:44:24.790188+00:00","client_scope_id":"ogilvy_default","contract_version":"open_intelligence_protected_source_snapshot_v1","creation_records":[{"destination":"ogilvy-trends-v2.trends_v2_staging.open_intelligence_v3_source_20260907_event_ledger","job_id":"oi_v3_snapshot_109ea182ba6f1b7042c76ecbea7d62cab753b3867cca57c909b422878b0a5433_event_ledger","lane":"event_ledger","native_job_digest":"be521e66d35d6662a039f52de97330994634ac666554147ba5f2c85e8b6b11e0","state":"succeeded"},{"destination":"ogilvy-trends-v2.trends_v2_staging.open_intelligence_v3_source_20260907_seed_graph","job_id":"oi_v3_snapshot_1cb4d379cbac4a450417f7c455d3d81df87a45b22c05bca7bdc09083a0d16974_seed_graph","lane":"seed_graph","native_job_digest":"752a21d94d825ac4d092df5546516408bd07114a4d8c80248f2c2613d2b97aea","state":"succeeded"},{"destination":"ogilvy-trends-v2.trends_v2_staging.open_intelligence_v3_source_20260907_seed_candidates","job_id":"oi_v3_snapshot_1cb4d379cbac4a450417f7c455d3d81df87a45b22c05bca7bdc09083a0d16974_seed_candidates","lane":"seed_candidates","native_job_digest":"d217427db398a579372727fba5f0f00f63fa2064444e20c09681210a899861fe","state":"succeeded"},{"destination":"ogilvy-trends-v2.trends_v2_staging.open_intelligence_v3_source_20260907_enriched_content","job_id":"oi_v3_snapshot_1cb4d379cbac4a450417f7c455d3d81df87a45b22c05bca7bdc09083a0d16974_enriched_content","lane":"enriched_content","native_job_digest":"66aaa588bb92ce0c2313f16483c710788f58d7955c69615b76d6b2ca3621345b","state":"succeeded"},{"destination":"ogilvy-trends-v2.trends_v2_staging.open_intelligence_v3_source_20260907_raw_content","job_id":"oi_v3_snapshot_525202e9a55abad9efd2c757f1247f3ded070361be4462f3e93afbb10128f156_raw_content","lane":"raw_content","native_job_digest":"67e3a07e97cb4b84da1b106592d670877586b6a4e7248d3a908918665e7b54d7","state":"succeeded"}],"cutoff_date":"2026-09-07","limitations":["normalized_row_query_execution_proof_required","retention_and_cost_review_required","snapshot_creation_execution_proof_required","streaming_buffer_exclusion_unproven","upstream_collection_completeness_unproven"],"market_scope":["ke","ng","za"],"missing_checks":[],"query_count":4,"snapshot_digest":"91f5de861871bb6224be355f9922313d477041556b9d11141b53845716ccfc22","snapshot_plan_digest":"866bb6439e41eb21baef9d87e50f7168e236613208c428cead643836e3844e29","source_as_of":"2026-09-08T00:00:00+00:00","stored_artifact":{"created_at":"2026-09-08T15:46:26.901000+00:00","generation":1788882386850441,"sha256":"6fb2abd2d37025b064cf3fc6604fa97fda14230644a40a08f5a9faf035a841f3","size_bytes":63731275,"uri":"gs://ogilvy-trends-v2-oi-source-artifacts-staging/captures/8064a2d1a641ba1e61edca95b8db2a98846d12936baf51efdfdedcdeeda48de9/6fb2abd2d37025b064cf3fc6604fa97fda14230644a40a08f5a9faf035a841f3/capture.json"},"total_bytes_billed":150994944}',
        "completed_at": "2026-09-08T15:46:32.936000+00:00",
        "consumption_id": "exc_ae2164ad35aaef39e92003466e32d65d434b57148184b8d91343116c0339aac4",
        "execution_name": "projects/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-source-snapshot-staging/executions/trends-engine-oi-source-snapshot-staging-b4x25",
        "manifest_sha256": "525202e9a55abad9efd2c757f1247f3ded070361be4462f3e93afbb10128f156",
        "operation": "source_snapshot_capture",
        "result_contract_version": "open_intelligence_execution_result_v1",
        "result_digest": "2ee1ad49e17745f8ff42f71fcd061818b5bb8af692746706a9962e03238d9ff3",
        "result_id": "exr_b1e13578bac9fd5a070bb8eb2c8a2ecd956c9912584f04465fb6a53c64c5b1bd",
        "result_reference": "projects/ogilvy-trends-v2/locations/us-central1/jobs/trends-engine-oi-source-snapshot-staging/executions/trends-engine-oi-source-snapshot-staging-b4x25#source-snapshot",
        "status": "succeeded",
    }
    row["completed_at"] = datetime.fromisoformat(row["completed_at"])
    return row


def test_direct_excerpt_prioritizes_required_term_and_does_not_cover_hidden_requirement(
    monkeypatch,
):
    request, plan, _ = inputs()
    loaded, queries, captures, records = query_inputs(
        monkeypatch, text="optionalword" + "x" * 5000 + " requiredword"
    )
    material, bindings = subject._query_material(queries, captures, records, loaded.source_binding)
    material["candidate"]["records"] = []
    material["partitions"] = {}
    material["enriched_index"]["records"] = [{"market": "ng", "id": "row_1"}]
    plan["requirements"] = [
        {
            "requirement_id": "optional",
            "kind": "content",
            "mandatory": False,
            "search_terms": ["optionalword"],
        },
        {
            "requirement_id": "required",
            "kind": "content",
            "mandatory": True,
            "search_terms": ["requiredword"],
        },
    ]
    _, receipts, _, fulfilled, _, _, _ = subject._project(
        request,
        plan,
        loaded.source_binding,
        material,
        bindings,
        subject._CURRENT_ADMISSION_VERSION,
        loaded.profile_id,
    )
    assert fulfilled == ["required"]
    assert "requiredword" in receipts[0]["excerpt"]
    assert "optionalword" not in receipts[0]["excerpt"]
    assert len(receipts[0]["excerpt"]) <= 4000


@pytest.mark.parametrize("blank", [""])
def test_empty_optional_source_metadata_projects_null_before_answer_context(monkeypatch, blank):
    from scripts.staging.replay_open_intelligence import _digest
    from src.analysis.open_intelligence.general_question_answer import _context
    from src.analysis.open_intelligence.general_question_snapshot import _project_context_snapshot

    request, plan, intake = inputs()
    loaded, queries, captures, records = query_inputs(
        monkeypatch, optional_metadata={"author_handle": blank, "url": blank}
    )
    material, _ = subject._query_material(queries, captures, records, loaded.source_binding)
    original_payload = copy.deepcopy(material["enriched"]["records"][0]["payload"])
    admission = subject.admit_protected_context(
        request,
        plan,
        intake,
        source=loaded,
        queries=queries,
        captures=captures,
        query_records=records,
    )
    snapshot = _project_context_snapshot(
        {
            "request": request,
            "intake": intake,
            "admission": {"policy_digest": request["policy_digest"], "deployment_digest": "d" * 64},
        },
        plan,
        admission,
        {},
    )
    _context(request, intake, plan, snapshot)
    assert snapshot["receipts"][0]["author"] is None
    assert snapshot["receipts"][0]["url"] is None
    assert snapshot["receipts"][0]["content_digest"] == _digest(original_payload)
    assert material["enriched"]["records"][0]["payload"] == original_payload
    bad = copy.deepcopy(snapshot)
    bad["receipts"][0]["url"] = "not a URL"
    bad["snapshot_digest"] = canonical_digest(
        {k: v for k, v in bad.items() if k != "snapshot_digest"}
    )
    with pytest.raises(ValueError):
        _context(request, intake, plan, bad)


@pytest.mark.parametrize("bad_value", ["   ", False, 0])
def test_optional_source_metadata_does_not_coerce_other_falsy_values(monkeypatch, bad_value):
    request, plan, intake = inputs()
    loaded, queries, captures, records = query_inputs(
        monkeypatch, optional_metadata={"author_handle": bad_value, "url": bad_value}
    )
    admission = subject.admit_protected_context(
        request,
        plan,
        intake,
        source=loaded,
        queries=queries,
        captures=captures,
        query_records=records,
    )
    assert admission["receipts"][0]["author"] == bad_value
    assert type(admission["receipts"][0]["author"]) is type(bad_value)
    assert admission["receipts"][0]["url"] == bad_value


def test_mixed_plan_retains_content_projection_without_aggregate_side_effects(monkeypatch):
    request, plan, _ = inputs()
    loaded, queries, captures, records = query_inputs(monkeypatch)
    material, bindings = subject._query_material(queries, captures, records, loaded.source_binding)
    version = (subject._CURRENT_ADMISSION_VERSION, loaded.profile_id)
    before = subject._project(request, plan, loaded.source_binding, material, bindings, *version)
    mixed = copy.deepcopy(plan)
    aggregate = copy.deepcopy(plan["requirements"][0])
    aggregate.update(
        kind="aggregate",
        requirement_id="other_aggregate",
        search_terms=["commute", "differentaggregate"],
    )
    mixed["requirements"].append(aggregate)
    assert (
        subject._project(request, mixed, loaded.source_binding, material, bindings, *version)
        == before
    )


@pytest.mark.parametrize("isolated", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("fails", [False, True])
def test_reader_gc_scope_is_execution_owned_and_restored(monkeypatch, isolated, enabled, fails):
    import gc

    original = gc.isenabled()
    gc.enable() if enabled else gc.disable()
    token = subject._ISOLATED_READER_EXECUTION.set(isolated)
    marker = object()

    def load(*args, **kwargs):
        assert gc.isenabled() == (enabled and not isolated)
        if fails:
            raise ValueError("synthetic")
        return marker

    monkeypatch.setattr(subject, "_load_protected_context_source", load)
    try:
        if fails:
            with pytest.raises(ValueError):
                subject.load_protected_context_source(
                    None,
                    None,
                    None,
                    objects=None,
                    native_client=None,
                    result_reader=None,
                    approval_reader=None,
                    now=None,
                )
        else:
            assert (
                subject.load_protected_context_source(
                    None,
                    None,
                    None,
                    objects=None,
                    native_client=None,
                    result_reader=None,
                    approval_reader=None,
                    now=None,
                )
                is marker
            )
        assert gc.isenabled() == enabled
    finally:
        subject._ISOLATED_READER_EXECUTION.reset(token)
        gc.enable() if original else gc.disable()
