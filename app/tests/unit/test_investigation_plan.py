from __future__ import annotations

import importlib
import importlib.util
from datetime import UTC, datetime

import pytest


def investigation_module():
    name = "src.api.investigations"
    assert importlib.util.find_spec(name) is not None, "investigation plan module is missing"
    return importlib.import_module(name)


def frame(module, **overrides):
    values = {
        "client_scope_id": "ogilvy_default",
        "market_scope": ("za", "ng", "ke"),
        "brand_config_id": None,
        "audience_lens_ids": (),
        "theme_id": None,
        "run_id": "investigation_run_001",
        "contract_version": "2.1.0",
        "decision_question": "Which emerging behaviour should the brand act on in six weeks?",
        "time_horizon_days": 42,
        "brand_context": None,
        "known_assumptions": (),
        "change_my_mind_if": (),
        "research_role_id": None,
        "research_role_version": None,
        "output_mode": "internal_working_paper",
    }
    values.update(overrides)
    return module.InvestigationFrame(**values)


def plan_inputs(module, **overrides):
    values = {
        "questions": (
            "Which emerging behaviour should the brand act on in six weeks?",
            "Which observed behaviours changed inside the declared window?",
            "What evidence supports and opposes acting now?",
        ),
        "required_source_families": ("social", "search", "news", "history"),
        "socialcrawl_lanes": ("baseline_sensing", "evidence_deepening"),
        "historical_window_days": 365,
        "credit_ceiling": 0,
        "token_budget": {},
        "stopping_conditions": (
            "Stop when every factual claim has a resolvable receipt.",
            "Stop when a blocking contradiction remains unresolved.",
        ),
        "known_gaps": ("optional_socialcrawl_funding_unverified",),
    }
    values.update(overrides)
    return module.ResearchPlanInputs(**values)


def test_plan_ready_response_matches_the_approved_2_1_contract():
    module = investigation_module()
    now = datetime(2026, 8, 26, 8, 0, tzinfo=UTC)

    response = module.build_plan_ready(
        frame=frame(module),
        plan=plan_inputs(module),
        investigation_id="inv_0123456789abcdef",
        created_at=now,
        active_role_versions={},
    )

    assert response == {
        "investigation_id": "inv_0123456789abcdef",
        "client_scope_id": "ogilvy_default",
        "market_scope": ["za", "ng", "ke"],
        "brand_config_id": None,
        "audience_lens_ids": [],
        "theme_id": None,
        "run_id": "investigation_run_001",
        "contract_version": "2.1.0",
        "research_role_id": None,
        "research_role_version": None,
        "status": "plan_ready",
        "research_plan": {
            "questions": [
                "Which emerging behaviour should the brand act on in six weeks?",
                "Which observed behaviours changed inside the declared window?",
                "What evidence supports and opposes acting now?",
            ],
            "required_source_families": ["social", "search", "news", "history"],
            "socialcrawl_lanes": ["baseline_sensing", "evidence_deepening"],
            "historical_window_days": 365,
            "credit_ceiling": 0,
            "token_budget": {},
            "stopping_conditions": [
                "Stop when every factual claim has a resolvable receipt.",
                "Stop when a blocking contradiction remains unresolved.",
            ],
            "known_gaps": ["optional_socialcrawl_funding_unverified"],
        },
        "created_at": "2026-08-26T08:00:00Z",
        "updated_at": "2026-08-26T08:00:00Z",
    }


def test_plan_ready_rejects_wrong_version_and_partial_role_identity():
    module = investigation_module()
    now = datetime(2026, 8, 26, 8, 0, tzinfo=UTC)

    with pytest.raises(ValueError, match="contract_version_unsupported"):
        frame(module, contract_version="2.0.0")
    with pytest.raises(ValueError, match="research role id and version must both be null or set"):
        frame(module, research_role_id="strategist", research_role_version=None)
    with pytest.raises(ValueError, match="research role id and version must both be null or set"):
        frame(module, research_role_id=None, research_role_version="1")
    with pytest.raises(ValueError, match="created_at must be UTC"):
        module.build_plan_ready(
            frame=frame(module),
            plan=plan_inputs(module),
            investigation_id="inv_0123456789abcdef",
            created_at=now.replace(tzinfo=None),
            active_role_versions={},
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"client_scope_id": ""}, "client scope id must be a nonempty string"),
        ({"market_scope": ()}, "market scope must be nonempty"),
        ({"market_scope": ("ZA",)}, "market scope is invalid"),
        ({"market_scope": ("za", "za")}, "market scope must be unique"),
        ({"market_scope": ("uk",)}, "market scope is invalid"),
        ({"audience_lens_ids": ("measured", "measured")}, "audience lens ids must be unique"),
        ({"time_horizon_days": True}, "time horizon days must be a positive integer"),
        ({"time_horizon_days": 0}, "time horizon days must be a positive integer"),
        ({"output_mode": "generated_deck"}, "output mode is unsupported"),
    ],
)
def test_frame_rejects_invalid_scope_and_output(overrides, message):
    module = investigation_module()
    with pytest.raises(ValueError, match=message):
        frame(module, **overrides)


def test_plan_inputs_fail_closed_on_invented_or_unsafe_capabilities():
    module = investigation_module()

    with pytest.raises(ValueError, match="credit ceiling must be a nonnegative integer"):
        plan_inputs(module, credit_ceiling=True)
    with pytest.raises(ValueError, match="historical window days must be a positive integer"):
        plan_inputs(module, historical_window_days=0)
    with pytest.raises(ValueError, match="token budget must be an object"):
        plan_inputs(module, token_budget=[])
    with pytest.raises(ValueError, match="token budget summary must be a nonnegative integer"):
        plan_inputs(module, token_budget={"summary": -1})


def test_plan_ready_preserves_complete_role_pair_and_token_budget():
    module = investigation_module()
    response = module.build_plan_ready(
        frame=frame(
            module,
            research_role_id="strategist",
            research_role_version="role_sha256_abc",
        ),
        plan=plan_inputs(module, token_budget={"synthesis": 800, "planning": 200}),
        investigation_id="inv_0123456789abcdef",
        created_at=datetime(2026, 8, 26, 8, 0, tzinfo=UTC),
        active_role_versions={"strategist": "role_sha256_abc"},
    )

    assert response["research_role_id"] == "strategist"
    assert response["research_role_version"] == "role_sha256_abc"
    assert response["research_plan"]["token_budget"] == {"synthesis": 800, "planning": 200}


def test_plan_ready_accepts_empty_contract_arrays():
    module = investigation_module()
    response = module.build_plan_ready(
        frame=frame(module),
        plan=plan_inputs(
            module,
            questions=(),
            required_source_families=(),
            socialcrawl_lanes=(),
            stopping_conditions=(),
            known_gaps=(),
        ),
        investigation_id="inv_empty_plan",
        created_at=datetime(2026, 8, 26, 8, 0, tzinfo=UTC),
        active_role_versions={},
    )

    assert response["research_plan"]["questions"] == []
    assert response["research_plan"]["required_source_families"] == []
    assert response["research_plan"]["stopping_conditions"] == []


@pytest.mark.parametrize(
    "active_role_versions",
    [{}, {"strategist": "stale_version"}],
)
def test_plan_ready_rejects_unknown_or_stale_role_version(active_role_versions):
    module = investigation_module()
    with pytest.raises(ValueError, match="research role version is not active"):
        module.build_plan_ready(
            frame=frame(
                module,
                research_role_id="strategist",
                research_role_version="role_sha256_abc",
            ),
            plan=plan_inputs(module),
            investigation_id="inv_role_check",
            created_at=datetime(2026, 8, 26, 8, 0, tzinfo=UTC),
            active_role_versions=active_role_versions,
        )


def test_plan_ready_rejects_invalid_builder_inputs():
    module = investigation_module()
    now = datetime(2026, 8, 26, 8, 0, tzinfo=UTC)

    with pytest.raises(ValueError, match="investigation frame is invalid"):
        module.build_plan_ready(
            frame={},
            plan=plan_inputs(module),
            investigation_id="inv_0123456789abcdef",
            created_at=now,
            active_role_versions={},
        )
    with pytest.raises(ValueError, match="research plan inputs are invalid"):
        module.build_plan_ready(
            frame=frame(module),
            plan={},
            investigation_id="inv_0123456789abcdef",
            created_at=now,
            active_role_versions={},
        )
    with pytest.raises(ValueError, match="investigation id is invalid"):
        module.build_plan_ready(
            frame=frame(module),
            plan=plan_inputs(module),
            investigation_id="research_001",
            created_at=now,
            active_role_versions={},
        )


def test_plan_ready_preserves_fractional_utc_timestamp():
    module = investigation_module()
    response = module.build_plan_ready(
        frame=frame(module),
        plan=plan_inputs(module),
        investigation_id="inv_fractional_time",
        created_at=datetime(2026, 8, 26, 8, 0, 0, 123456, tzinfo=UTC),
        active_role_versions={},
    )

    assert response["created_at"] == "2026-08-26T08:00:00.123456Z"
    assert response["updated_at"] == response["created_at"]


def test_plan_ready_has_no_execution_or_generated_answer_fields():
    module = investigation_module()
    response = module.build_plan_ready(
        frame=frame(module),
        plan=plan_inputs(module),
        investigation_id="inv_0123456789abcdef",
        created_at=datetime(2026, 8, 26, 8, 0, tzinfo=UTC),
        active_role_versions={},
    )

    def keys(value):
        if isinstance(value, dict):
            return set(value).union(*(keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(item) for item in value)) if value else set()
        return set()

    assert keys(response).isdisjoint(
        {"answer", "recommendation", "artifact", "source_calls", "model_output"}
    )
