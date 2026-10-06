from time import monotonic

import pytest
from src.analysis.open_intelligence.general_question_control import QuestionStoreError
from src.analysis.open_intelligence.general_question_parent_context import parent_read_attempt

from tests.unit import test_general_question_store as fixture


def capsule_bundle(
    identifier="00000000-0000-4000-8000-000000000098", row_id="grocery", market="za"
):
    import json

    from src.analysis.open_intelligence.brain_contract import canonical_digest

    ref = {
        "request_id": identifier,
        **dict.fromkeys(
            ("request_digest", "intake_digest", "plan_digest", "snapshot_digest", "result_digest"),
            "a" * 64,
        ),
        **dict.fromkeys(
            (
                "request_generation",
                "intake_generation",
                "plan_generation",
                "snapshot_generation",
                "result_generation",
            ),
            "1",
        ),
    }
    payload = {
        "id": row_id,
        "market": market,
        "platform": "reddit",
        "collected_at": "2026-09-01T00:00:00Z",
    }
    receipt = {
        "kind": "content",
        "receipt_id": "r_" + row_id,
        "source_row_id": row_id,
        "market": market,
        "content_digest": canonical_digest(payload),
        "excerpt": "A grocery anecdote.",
    }
    claim = {
        "claim_id": "q",
        "kind": "observation",
        "reading_ids": [],
        "receipt_ids": [receipt["receipt_id"]],
        "parent_claim_ids": [],
        "text": receipt["excerpt"],
        "support_state": "source_record",
    }
    snapshot = {
        "readings": [],
        "provenance": {
            "profile": "protected_context_v1",
            "source_binding": {"source_binding_digest": "b" * 64},
            "captures": {
                "enriched": {
                    "rows": [{"id": row_id, "market": market, "payload": json.dumps(payload)}]
                }
            },
        },
    }
    return {
        "reference": ref,
        "intake": {"selected_market": None},
        "plan": {
            "markets": [market],
            "intent": "discovery",
            "window": {"start": "2026-09-01", "end": "2026-09-05", "closed": True},
        },
        "snapshot": snapshot,
        "result": {"response": {"intelligence": {"claims": [claim], "receipts": [receipt]}}},
    }


def test_capsule_frame_comes_from_parent_and_preview_is_bound():
    import copy

    from src.analysis.open_intelligence.brain_contract import canonical_digest
    from src.analysis.open_intelligence.general_question_parent_capsule import (
        build_capsule,
        planner_parent_view,
        validate_capsule,
    )

    _, request, _ = fixture.prepared()
    parent, anchor = (
        capsule_bundle(),
        capsule_bundle("00000000-0000-4000-8000-000000000097", "anchor_grocery"),
    )
    anchor["plan"]["window"]["start"] = "2026-08-01"
    value = build_capsule(request, parent, anchor)
    assert value["resolved_frame"]["window"] == parent["plan"]["window"]
    assert value["resolved_frame"]["selected_market"] is None
    view = planner_parent_view(value)
    assert len(view["source_previews"]) == 2
    assert view["source_previews"][0]["origin_role"] == "anchor"
    changed = copy.deepcopy(value)
    changed["parent_receipt_refs"][0]["source_preview"]["text"] += " altered"
    assert canonical_digest(changed) != canonical_digest(value)
    with pytest.raises(QuestionStoreError, match="parent_context_invalid"):
        validate_capsule(changed, request=request)


def test_hidden_parent_alias_cannot_be_selected():
    from src.analysis.open_intelligence.general_question_parent_capsule import (
        build_capsule,
        select_parent_aliases,
    )

    _, request, _ = fixture.prepared()
    value = build_capsule(request, capsule_bundle())
    alias = value["parent_receipt_refs"][0]["alias"]
    assert select_parent_aliases(value, [alias])[0]["source_row_id"] == "grocery"
    value["parent_receipt_refs"][0]["source_preview"] = None
    with pytest.raises(QuestionStoreError, match="parent_alias_invalid"):
        select_parent_aliases(value, [alias])


@pytest.mark.parametrize(
    "parent_selected,current_selected,parent_markets,expected",
    [
        ("za", "za", ["ng"], ["ng"]),
        ("za", "ke", ["ng"], ["ke"]),
        (None, "za", ["ng"], ["za"]),
        ("za", None, ["ke", "ng"], ["ke", "ng"]),
    ],
)
def test_parent_planner_frame_and_visible_aliases(
    parent_selected, current_selected, parent_markets, expected
):
    from src.analysis.open_intelligence.brain_contract import canonical_digest
    from src.analysis.open_intelligence.general_question_parent_capsule import (
        REF_VERSION,
        build_capsule,
    )
    from src.analysis.open_intelligence.general_question_plan import (
        build_question_planning_context,
        validate_question_plan,
    )
    from src.analysis.open_intelligence.general_question_planning import (
        build_question_planning_request,
    )
    from src.analysis.open_intelligence.general_question_policy import build_intake_context

    from tests.unit.test_general_question_runtime import plan_draft

    policy, request, _ = fixture.prepared()
    parent = capsule_bundle(market=parent_markets[0])
    parent["intake"]["selected_market"] = parent_selected
    parent["plan"]["markets"] = parent_markets
    capsule = build_capsule(request, parent)
    reference = {
        "contract_version": REF_VERSION,
        "parent": capsule["parent"],
        "anchor": None,
        "context_digest": canonical_digest(capsule),
        "context_generation": "9",
    }
    intake = build_intake_context(
        request, selected_market=current_selected, parent_context_ref=reference
    )
    context = build_question_planning_context(request, intake, parent_context=capsule)
    assert context["default_markets"] == expected
    assert context["default_observation_window"] == parent["plan"]["window"]
    assert "parent_context_ref" not in context["intake"]
    sdk = build_question_planning_request(
        request, intake, policy=policy, remaining_seconds=60, parent_context=capsule
    )
    assert capsule["parent"]["request_id"] not in sdk.contents.replace(
        capsule["resolved_frame"]["origin_request_id"], ""
    )
    draft = plan_draft()
    draft["parent_receipt_aliases"] = [capsule["parent_receipt_refs"][0]["alias"]]
    assert (
        validate_question_plan(draft, request=request, intake=intake, parent_context=capsule)[
            "contract_version"
        ]
        == "general_question_plan_v2"
    )
    draft["parent_receipt_aliases"] = ["s48"]
    with pytest.raises(QuestionStoreError, match="parent_alias_invalid"):
        validate_question_plan(draft, request=request, intake=intake, parent_context=capsule)


def test_attempt_cache_avoids_repeated_metadata_for_selected_generation():
    store, bucket = fixture.store()
    bucket.seed("requests/00000000-0000-4000-8000-000000000099/plan.json", {"known": True})
    key = "requests/00000000-0000-4000-8000-000000000099/plan.json"
    calls = []
    original = bucket.get_blob
    bucket.get_blob = lambda *a, **k: calls.append((a, k)) or original(*a, **k)
    with parent_read_attempt(store, deadline=monotonic() + 20) as attempt:
        first = store._objects.read(key)
        second = store._objects.read(key, generation=first.generation)
        third = store._objects.read(key)
        assert first == second == third
        assert attempt.read_attempts == 1
    assert len(calls) == 1
    store._objects.read(key, generation=first.generation)
    assert len(calls) == 2


def test_read_budget_counts_missing_reads_and_refuses_before_attempt_41():
    store, bucket = fixture.store()
    calls = []
    original = bucket.get_blob
    bucket.get_blob = lambda *a, **k: calls.append(a) or original(*a, **k)
    with parent_read_attempt(store, deadline=monotonic() + 20):
        for _ in range(40):
            assert (
                store._objects.read("requests/00000000-0000-4000-8000-000000000099/plan.json")
                is None
            )
        with pytest.raises(QuestionStoreError, match="parent_read_budget_exhausted"):
            store._objects.read("requests/00000000-0000-4000-8000-000000000099/plan.json")
    assert len(calls) == 40


def test_frozen_parent_ledger_does_not_cache_fresh_cas_comparison():
    store, bucket = fixture.store()
    fixture.activate_fixture(bucket)
    with parent_read_attempt(store, deadline=monotonic() + 20) as attempt:
        frozen = store._objects.read(fixture.LEDGER)
        changed = dict(frozen.value)
        changed["reserved_microusd"] = 123
        bucket.seed(fixture.LEDGER, changed)
        with attempt.freeze_ledger(frozen):
            assert store._objects.read(fixture.LEDGER).value == frozen.value
        assert store._objects.read(fixture.LEDGER).value == changed
        assert attempt.read_attempts == 2


def test_raw_parent_overlap_is_reserved_for_raw_and_hash_checked():
    import copy
    import json

    from scripts.staging.replay_open_intelligence import _digest
    from src.analysis.open_intelligence.brain_contract import canonical_digest
    from src.analysis.open_intelligence.general_question_parent_capsule import (
        REF_VERSION,
        build_capsule,
    )
    from src.analysis.open_intelligence.general_question_plan import validate_question_plan
    from src.analysis.open_intelligence.general_question_policy import build_intake_context

    from tests.unit import test_general_question_context_queries as wires

    builder = wires.subject()
    request, old_plan, _, source = wires.inputs()
    bundle = capsule_bundle(row_id="one")
    row = bundle["snapshot"]["provenance"]["captures"].pop("enriched")["rows"][0]
    payload = {
        "id": "one",
        "market": "za",
        "platform": "reddit",
        "source": "synthetic",
        "text": "A grocery anecdote.",
        "title": None,
        "url": None,
        "author_handle": None,
        "published_at": None,
        "collected_at": "2026-09-07T00:00:00+00:00",
    }
    row["payload"] = json.dumps(payload)
    bundle["snapshot"]["provenance"]["captures"]["raw"] = {"rows": [row]}
    bundle["snapshot"]["provenance"]["source_binding"]["source_binding_digest"] = (
        builder._binding_digest(source)
    )
    bundle["result"]["response"]["intelligence"]["receipts"][0]["content_digest"] = _digest(payload)
    capsule = build_capsule(request, bundle)
    ref = {
        "contract_version": REF_VERSION,
        "parent": capsule["parent"],
        "anchor": None,
        "context_digest": canonical_digest(capsule),
        "context_generation": "1",
    }
    intake = build_intake_context(request, selected_market="za", parent_context_ref=ref)
    draft = {
        key: value
        for key, value in old_plan.items()
        if key
        not in {"contract_version", "request_id", "request_digest", "intake_digest", "plan_digest"}
    }
    draft["parent_receipt_aliases"] = [capsule["parent_receipt_refs"][0]["alias"]]
    plan = validate_question_plan(draft, request=request, intake=intake, parent_context=capsule)
    args = request, plan, intake, source
    common = {
        "candidate_rows": wires.candidate_rows(source),
        "candidate_limit": 1,
        "parent_context": capsule,
        "continuity_policy": True,
        "geo_policy": True,
        "partition_rows": [
            {"lane": "__metadata__", "partition_date": None, "partition_count": 2},
            {"lane": "enriched_content", "partition_date": "2026-09-07", "partition_count": 2},
            {"lane": "raw_content", "partition_date": "2026-09-07", "partition_count": 2},
        ],
    }
    enriched_query = builder.build_context_index_query(*args, lane="enriched_content", **common)
    assert "@excluded_parent_ids" in enriched_query.sql
    assert ("za", "one") not in enriched_query.expected_sample_keys
    indexed = [
        wires.metadata(source, 1, 1, evidence=True),
        {
            "lane": "enriched_content",
            "market": "za",
            "id": "one",
            "match_count": 1,
            "payload": json.dumps({"market": "za", "id": "one", "collected_date": "2026-09-07"}),
        },
    ]
    with pytest.raises(ValueError, match="parent_source_unavailable"):
        builder.decode_context_rows(indexed, query=enriched_query)
    raw_common = {**common, "enriched_rows": [wires.metadata(source, 0, 0, evidence=True)]}
    raw_query = builder.build_context_index_query(*args, lane="raw_content", **raw_common)
    assert "@parent_ids" in raw_query.sql
    assert "OR NOT EXISTS" in raw_query.sql
    indexed[1]["lane"] = "raw_content"
    projection = builder.build_context_evidence_query(
        *args, lane="raw_content", index_rows=indexed, **raw_common
    )
    rows = [
        wires.metadata(source, 1, 1, evidence=True),
        {
            "lane": "raw_content",
            "market": "za",
            "id": "one",
            "match_count": 1,
            "payload": json.dumps(payload),
        },
    ]
    assert builder.decode_context_rows(rows, query=projection)["candidate_count"] == 1
    changed = copy.deepcopy(payload)
    changed["text"] = "Different source content."
    rows[1]["payload"] = json.dumps(changed)
    with pytest.raises(ValueError, match="parent_source_unavailable"):
        builder.decode_context_rows(rows, query=projection)


def test_response_versions_bind_a_parent_planning_schema_to_the_visible_aliases():
    from types import SimpleNamespace

    from src.analysis.open_intelligence.brain_contract import canonical_digest
    from src.analysis.open_intelligence.general_question_parent_capsule import (
        REF_VERSION,
        build_capsule,
        planner_parent_view,
    )
    from src.analysis.open_intelligence.general_question_parent_context import (
        parent_planning_aliases,
        recorded_response_versions,
        resolve_response_versions,
    )
    from src.analysis.open_intelligence.general_question_planning import (
        build_question_planning_request,
        planning_schema_digest,
    )
    from src.analysis.open_intelligence.general_question_policy import build_intake_context

    policy, request, _ = fixture.prepared()
    capsule = build_capsule(request, capsule_bundle())
    reference = {
        "contract_version": REF_VERSION,
        "parent": capsule["parent"],
        "anchor": None,
        "context_digest": canonical_digest(capsule),
        "context_generation": "9",
    }
    intake = build_intake_context(request, selected_market="za", parent_context_ref=reference)
    sdk = build_question_planning_request(
        request, intake, policy=policy, remaining_seconds=60, parent_context=capsule
    )
    aliases = [row["alias"] for row in planner_parent_view(capsule)["source_previews"]]
    assert aliases == ["s01"]
    binding = {
        "system_instruction_digest": sdk.system_instruction_digest,
        "response_schema_digest": sdk.response_schema_digest,
        "model": sdk.model,
        "policy_digest": policy["policy_digest"],
    }
    resolved = resolve_response_versions(
        policy_digest=policy["policy_digest"],
        planning=binding,
        answering=None,
        parent_aliases=aliases,
    )
    assert resolved["planning"]["adapter"] == "parent_context_v3"
    assert resolved["planning"]["response_schema_digest"] == planning_schema_digest(
        "parent_context_v3", parent_aliases=aliases
    )
    for wrong in (None, [], ["s02"], ["s01", "s02"]):
        with pytest.raises(ValueError, match="response_version_unresolved"):
            resolve_response_versions(
                policy_digest=policy["policy_digest"],
                planning=binding,
                answering=None,
                parent_aliases=wrong,
            )
    reads = []

    def read(key, generation=None):
        reads.append((key, generation))
        return SimpleNamespace(value=capsule)

    store = SimpleNamespace(_objects=SimpleNamespace(read=read))
    assert parent_planning_aliases(store, request, intake) == aliases
    assert reads == [(f"requests/{request['request_id']}/parent_context.json", 9)]
    assert parent_planning_aliases(store, request, {"selected_market": "za"}) is None
    assert len(reads) == 1
    context = {
        "request": request,
        "intake": intake,
        "admission": {
            "execution": {
                "calls": {
                    "planning": {
                        **binding,
                        "stage": "planning",
                        "response": {"generation": "1", "digest": "d" * 64},
                    }
                }
            }
        },
    }
    recorded = recorded_response_versions(context, parent_aliases=aliases)
    assert recorded["planning"]["adapter"] == "parent_context_v3"
    with pytest.raises(ValueError, match="response_version_unresolved"):
        recorded_response_versions(context)


def test_a_follow_up_keeps_the_client_lens_its_parent_and_anchor_were_asked_under():
    """A follow-up continues its own thread's lens and never crosses into another.

    The same wording asked under general 42 and under the BSA lens is two threads.
    A follow-up carrying one lens cannot take a parent or an anchor admitted under
    the other as its context, in either direction. A follow-up under the same lens
    as its parent and anchor is the positive control.
    """
    import copy

    from src.analysis.open_intelligence.general_question_parent_capsule import build_capsule

    lens = {
        "lens_binding_version": "client_lens_binding_v1",
        "client_lens_id": "bsa_pulse_lens",
        "configuration_digest": "e1baafa865b5c9419225102d752c651c5b6e4fada034a2317f2d6f8a395336bf",
    }
    _, general_child, _ = fixture.prepared()
    lensed_child = {**copy.deepcopy(general_child), "client_lens": copy.deepcopy(lens)}

    def bundle(identifier, row_id, client_lens):
        value = capsule_bundle(identifier, row_id)
        value["request"] = {"request_id": identifier}
        if client_lens is not None:
            value["request"]["client_lens"] = copy.deepcopy(client_lens)
        return value

    parent_id, anchor_id = (
        "00000000-0000-4000-8000-000000000098",
        "00000000-0000-4000-8000-000000000097",
    )
    general_parent = bundle(parent_id, "grocery", None)
    lensed_parent = bundle(parent_id, "grocery", lens)
    lensed_anchor = bundle(anchor_id, "anchor_grocery", lens)

    for child, parent in ((lensed_child, general_parent), (general_child, lensed_parent)):
        with pytest.raises(QuestionStoreError, match="parent_context_invalid"):
            build_capsule(child, parent)
    with pytest.raises(QuestionStoreError, match="parent_context_invalid"):
        build_capsule(general_child, general_parent, lensed_anchor)

    assert build_capsule(lensed_child, lensed_parent, lensed_anchor)["parent"]["request_id"] == (
        parent_id
    )
    assert build_capsule(general_child, general_parent)["parent"]["request_id"] == parent_id
