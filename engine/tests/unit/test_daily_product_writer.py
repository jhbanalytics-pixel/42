import pytest
from src.analysis.open_intelligence import daily_composer, persistence

from tests.unit import test_daily_composer as fixtures
from tests.unit.daily_product_authority_fixture import funded_product_authority
from tests.unit.test_daily_composer_persistence import AtomicClient, call


def test_native_dynamic_writer_uses_consumed_manifest_principal(tmp_path, monkeypatch):
    fx = funded_product_authority(tmp_path, monkeypatch)
    composer, _old, _client, _warehouse = fixtures.build()
    client = AtomicClient()
    bundle = daily_composer.daily_rule_bundle(
        fixtures.staging_profile(),
        approved_by="existing-rule-approval",
        approved_at=fixtures.stages.STARTED,
        rule_version="composition_rules_v3",
    )
    persist = daily_composer.build_native_persist(
        client=client,
        dataset=fixtures.TARGET_DATASET,
        rule_bundle=bundle,
        run_receipt_inputs=daily_composer.composition_receipt_inputs(
            composer, source_sha=fixtures.SOURCE_SHA
        ),
        authority_receipt_from_run=lambda run_id: fx.receipt,
    )
    bridge = fixtures.bridge_for(fixtures.compose(composer))
    with pytest.raises(persistence.TargetInvalid, match="approved staging writer"):
        call(persist, bridge)
    assert client.loads == []


def test_acting_persister_accepts_no_caller_supplied_writer_identity():
    client = AtomicClient()
    with pytest.raises(TypeError, match="writer_identity"):
        persistence.persist_daily_composition(
            project="ogilvy-trends-v2",
            dataset=fixtures.TARGET_DATASET,
            client=client,
            batch=None,
            rule_bundle=None,
            receipt_fields={},
            writer_identity="unapproved@example.invalid",
        )
    assert client.loads == []
