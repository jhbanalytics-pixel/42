import pytest
from src.analysis.open_intelligence.daily_products import DailyProducts
from src.analysis.open_intelligence.daily_stages import _admitted_capture

from tests.unit.daily_product_authority_fixture import funded_product_authority
from tests.unit.test_daily_product_completion import Objects
from tests.unit.test_daily_stages import CUTOFF, Fixture, manifest


def test_native_product_lifecycle_refuses_missing_history_policy_before_queries(
    tmp_path, monkeypatch
):
    fx = funded_product_authority(tmp_path, monkeypatch)
    stages = Fixture(monkeypatch)
    stages.run(deny=("compose",))
    entry = _admitted_capture(stages.clients, stages.record("capture"))
    products = DailyProducts(
        objects=Objects(),
        build={
            "source_sha": fx.authority.manifest.source_sha,
            "image_uri": fx.authority.manifest.image_uri,
        },
        now=lambda: CUTOFF,
    )
    with pytest.raises(ValueError, match="products_history_policy_unavailable"):
        products.admit(entry=entry, manifest=manifest("compose"), authority_receipt=fx.receipt)


def test_forged_capture_cutoff_refuses_before_external_factory(tmp_path, monkeypatch):
    fx = funded_product_authority(tmp_path, monkeypatch)
    stages = Fixture(monkeypatch)
    stages.run(deny=("compose",))
    entry = _admitted_capture(stages.clients, stages.record("capture"))
    calls = []
    products = DailyProducts(
        objects=Objects(),
        build={
            "source_sha": fx.authority.manifest.source_sha,
            "image_uri": fx.authority.manifest.image_uri,
        },
        now=lambda: CUTOFF,
        io_factory=lambda **kwargs: calls.append(kwargs),
    )
    changed = manifest("compose")
    changed["cutoff_utc"] = "2026-09-12T00:00:00+00:00"
    with pytest.raises(ValueError, match="products_capture_differs"):
        products.admit(entry=entry, manifest=changed, authority_receipt=fx.receipt)
    assert calls == []


def test_native_factory_always_requires_products_and_missing_binding_refuses(monkeypatch):
    from src.analysis.open_intelligence import daily_native_clients

    from tests.unit.test_daily_native_clients import Fixture as NativeFixture
    from tests.unit.test_daily_stages import kernel_profile

    native = NativeFixture(monkeypatch)
    assert isinstance(native.clients["products"], DailyProducts)
    clients = dict(native.clients)
    del clients["products"]
    with pytest.raises(ValueError, match="products_native_binding_missing"):
        daily_native_clients.guarded_stage_handlers(clients, profile=kernel_profile())


def test_missing_dynamic_metering_refuses_before_the_external_factory(tmp_path, monkeypatch):
    fx = funded_product_authority(tmp_path, monkeypatch)
    stages = Fixture(monkeypatch)
    stages.run(deny=("compose",))
    entry = _admitted_capture(stages.clients, stages.record("capture"))
    calls = []
    products = DailyProducts(
        objects=Objects(),
        build={
            "source_sha": fx.authority.manifest.source_sha,
            "image_uri": fx.authority.manifest.image_uri,
        },
        now=lambda: CUTOFF,
        io_factory=lambda **kwargs: calls.append(kwargs),
    )
    with pytest.raises(ValueError, match="products_dynamic_metering_unavailable"):
        products.admit(entry=entry, manifest=manifest("compose"), authority_receipt=fx.receipt)
    assert calls == []
