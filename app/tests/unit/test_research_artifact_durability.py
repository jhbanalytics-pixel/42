"""Audit F7D-APP-009: a research artifact id was issued after a failed write, because the default backend (gcs) is
the expiring best-effort cache, whose write swallows its failure, and a failed BigQuery INSERT fell back to it. An id
now goes out only after a durable write: the BigQuery row, or the artifact's own bucket object outside the cache."""

from datetime import datetime, timedelta, timezone

import pytest

from src.api import bq, synth


class Blob:
    def __init__(self, bucket, name):
        self.bucket, self.name, self.updated = bucket, name, datetime.now(timezone.utc)

    def upload_from_string(self, text, content_type=None):
        if self.bucket.refuse:
            raise OSError("upload refused")
        self.bucket.objects[self.name] = text

    def download_as_text(self):
        return self.bucket.objects[self.name]

    def delete(self):
        del self.bucket.objects[self.name]


class Bucket:
    def __init__(self, refuse=False):
        self.refuse, self.objects = refuse, {}

    def blob(self, name):
        return Blob(self, name)

    def get_blob(self, name):
        if name not in self.objects:
            return None
        blob = Blob(self, name)
        blob.updated = datetime.now(timezone.utc) - timedelta(days=30)  # older than any cache TTL
        return blob


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    synth._CACHE.clear()
    yield
    synth._CACHE.clear()


def _bucket(monkeypatch, bucket):
    monkeypatch.setattr(synth, "_gcs_bucket", lambda: bucket)


@pytest.mark.parametrize("backend", ["gcs", "bq"])
def test_no_artifact_id_is_issued_when_no_durable_write_succeeds(monkeypatch, backend):
    monkeypatch.setenv("RESEARCH_PERSIST_BACKEND", backend)
    _bucket(monkeypatch, Bucket(refuse=True))
    monkeypatch.setattr(bq, "_run_query", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bq down")))
    with pytest.raises(bq.ResearchPersistError):
        bq.insert_research_artifact({"synthesis": {"title": "Brief"}})


def test_no_artifact_id_is_issued_without_a_bucket(monkeypatch):
    monkeypatch.setenv("RESEARCH_PERSIST_BACKEND", "gcs")
    _bucket(monkeypatch, None)
    with pytest.raises(bq.ResearchPersistError):
        bq.insert_research_artifact({"synthesis": {"title": "Brief"}})


def test_a_saved_artifact_is_read_after_the_process_cache_is_cleared_and_past_the_cache_ttl(monkeypatch):
    monkeypatch.setenv("RESEARCH_PERSIST_BACKEND", "gcs")
    bucket = Bucket()
    _bucket(monkeypatch, bucket)
    monkeypatch.setattr(bq, "_run_query", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bq down")))
    aid = bq.insert_research_artifact({"synthesis": {"title": "Brief"}})
    assert aid.startswith("ra_")
    synth._CACHE.clear()
    found = bq.get_research_artifact(aid)
    assert found is not None and found["synthesis"] == {"title": "Brief"}


def test_a_failed_bigquery_insert_falls_back_to_the_durable_object_not_the_cache(monkeypatch):
    monkeypatch.setenv("RESEARCH_PERSIST_BACKEND", "bq")
    bucket = Bucket()
    _bucket(monkeypatch, bucket)
    monkeypatch.setattr(bq, "_run_query", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bq down")))
    aid = bq.insert_research_artifact({"synthesis": {"title": "Brief"}})
    assert f"research/artifacts/{aid}.json" in bucket.objects


def test_a_deleted_artifact_is_not_served_from_its_bucket_object(monkeypatch):
    monkeypatch.setenv("RESEARCH_PERSIST_BACKEND", "gcs")
    bucket = Bucket()
    _bucket(monkeypatch, bucket)
    monkeypatch.setattr(bq, "_run_query", lambda *a, **k: [])
    artifact_id = bq.insert_research_artifact({"synthesis": {"title": "Brief"}})
    assert bq.delete_research_artifact(artifact_id) is True
    assert [n for n in bucket.objects if artifact_id in n] == [] and bq.get_research_artifact(artifact_id) is None
