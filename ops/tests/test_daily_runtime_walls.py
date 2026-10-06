from types import SimpleNamespace

import pytest

from ops.runners import managed_runtime as runtime
from ops.tests import test_foundation_scheduler as fixture


def adapter(job):
    origin = SimpleNamespace(
        contract_sha256="a" * 64,
        operation_bindings={
            name: SimpleNamespace(job_resource=job)
            for name in (
                "collection_exposure_issue",
                "source_snapshot_capture",
                "r3_apply",
                "r3_proof_issue",
                "r3_release",
            )
        },
    )
    module = SimpleNamespace(
        execution_generations=SimpleNamespace(
            active_generation=lambda: SimpleNamespace(registry=object())
        ),
        _v2_origin_for_operation=lambda *args, **kwargs: origin,
    )
    return runtime._EngineAuthority(module)


def test_real_describe_wrong_job_refuses_before_claim():
    engine = fixture._engine()
    grant = fixture._grant(engine)
    client = fixture.FakeObjectClient()
    wiring, config, _clock = fixture._wiring(
        engine, grant, client=client, handlers=fixture._succeeding_handlers([])
    )
    with pytest.raises(ValueError, match="^operation_unbound$"):
        runtime.run_managed_mode(
            fixture._build(config), authority=adapter("wrong"), daily=wiring
        )
    assert client.writes == []


def test_real_describe_accepts_daily_job_binding():
    assert adapter(
        "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
        "intelligence-42-daily-staging"
    ).describe("collection_exposure_issue") == {
        "operation": "collection_exposure_issue",
        "contract_sha256": "a" * 64,
    }


def test_adapter_routes_capture_reserve_and_consume():
    calls = []
    authority = SimpleNamespace(operation="source_snapshot_capture")
    module = SimpleNamespace(
        _load_source_snapshot_authority=lambda **kwargs: (
            calls.append(("reserve", kwargs)) or authority
        ),
        _consume_source_snapshot_authority=lambda item: (
            calls.append(("consume", item)) or "consumed"
        ),
    )
    subject = runtime._EngineAuthority(module)
    assert subject.reserve({}, {"operation": "source_snapshot_capture"}) is authority
    assert subject.consume(authority) == "consumed"
    assert calls == [("reserve", {"mode": "new_consume"}), ("consume", authority)]


def test_native_adapters_share_one_authority_module(monkeypatch):
    import importlib
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "engine" / "src"))
    monkeypatch.syspath_prepend(str(root / "engine"))
    subject = runtime.engine_authority_adapter()
    canonical = importlib.import_module(
        "src.analysis.open_intelligence.execution_approval"
    )
    assert subject._module is canonical
    assert subject._module.ExecutionConsumptionV2 is canonical.ExecutionConsumptionV2


DAILY_OPERATIONS = (
    "collection_exposure_issue",
    "source_snapshot_capture",
    "r3_apply",
    "r3_proof_issue",
    "r3_release",
)


def test_real_registry_binds_every_daily_operation_to_the_daily_job():
    subject = runtime.engine_authority_adapter()
    registry = subject._module.execution_generations.active_generation().registry
    for operation in DAILY_OPERATIONS:
        origin = subject._module._v2_origin_for_operation(
            operation, mode="new_consume", registry=registry
        )
        assert origin.operation_bindings[operation].job_resource == (
            "projects/ogilvy-trends-v2/locations/us-central1/jobs/"
            "intelligence-42-daily-staging"
        )
        assert subject.describe(operation) == {
            "operation": operation,
            "contract_sha256": origin.contract_sha256,
        }


def test_real_describe_refuses_a_host_qualified_binding():
    assert adapter(runtime.DAILY_JOB_RESOURCE).describe("r3_apply") is None
    assert (
        adapter(
            "//other.googleapis.com/projects/ogilvy-trends-v2/locations/"
            "us-central1/jobs/intelligence-42-daily-staging"
        ).describe("r3_apply")
        is None
    )
