from src.analysis.open_intelligence.daily_products import admit_execution

from tests.unit.daily_product_authority_fixture import funded_product_authority


def test_test_only_generation_admits_real_consumed_authority(tmp_path, monkeypatch):
    fx = funded_product_authority(tmp_path, monkeypatch)
    admitted = admit_execution(fx.receipt)
    assert admitted.consumption is fx.consumption
    assert admitted.manifest.limits["max_model_calls"] == 100
    assert admitted.manifest.limits["max_rows_written"] == 10_000
    assert admitted.generation.origin_registry_sha256 == fx.generation.origin_registry_sha256
    assert admitted.manifest.service_identity == (
        "intelligence-42-orchestration@ogilvy-trends-v2.iam.gserviceaccount.com"
    )
    assert admitted.manifest.job_resource == (
        "projects/ogilvy-trends-v2/locations/us-central1/jobs/intelligence-42-daily-staging"
    )
