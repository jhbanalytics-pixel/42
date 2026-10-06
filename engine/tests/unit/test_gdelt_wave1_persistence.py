from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from src.analysis.open_intelligence.gdelt_wave1_persistence import (
    EVENTS_TABLE,
    GCAM_TABLE,
    MARKETS_TABLE,
    GDELTImmutableConflict,
    build_wave1_gdelt_batch,
    persist_wave1_gdelt_batch,
)

MANIFEST = "a" * 64
EVENTS_RECEIPT = "gdry_" + "b" * 64
GCAM_RECEIPT = "gdry_" + "c" * 64


def event(event_id=1, **overrides):
    values = {
        "GLOBALEVENTID": event_id,
        "event_date": date(2026, 8, 27),
        "actor1_country_code": "NI",
        "actor2_country_code": "KE",
        "action_geo_country_code": "SF",
        "event_code": "010",
        "event_root_code": "01",
        "goldstein_scale": 1.0,
        "mention_count": 3,
        "source_count": 2,
        "article_count": 2,
        "average_tone": 0.4,
        "action_geo_name": "Johannesburg, South Africa",
        "action_geo_latitude": -26.2,
        "action_geo_longitude": 28.0,
        "source_url": "https://example.org/event",
        "date_added": datetime(2026, 8, 27, 10, tzinfo=UTC),
    }
    values.update(overrides)
    return values


def gcam(index=1, **overrides):
    values = {
        "document_url": f"https://example.org/document-{index}",
        "published_at": datetime(2026, 8, 27, 11, index, tzinfo=UTC),
        "v10_1": 5.1,
        "v10_2": None,
        "v19_1": 0.2,
        "v19_9": -0.1,
        "v20_1": 0.3,
    }
    values.update(overrides)
    return values


def batch(events=None, gcam_rows=None):
    return build_wave1_gdelt_batch(
        events_rows=events or (event(),),
        gcam_rows=gcam_rows or (gcam(),),
        run_id="run_20260903_dynamic_apply_v2_r16",
        manifest_sha256=MANIFEST,
        events_dry_run_receipt_id=EVENTS_RECEIPT,
        gcam_dry_run_receipt_id=GCAM_RECEIPT,
    )


def test_batch_derives_market_children_with_exact_role_priority_and_strips_geo_code():
    built = batch()
    assert tuple(built.events[0]) == (
        "GLOBALEVENTID",
        "event_date",
        "actor1_country_code",
        "actor2_country_code",
        "event_code",
        "event_root_code",
        "goldstein_scale",
        "mention_count",
        "source_count",
        "article_count",
        "average_tone",
        "action_geo_name",
        "action_geo_latitude",
        "action_geo_longitude",
        "source_url",
        "date_added",
    )
    assert "action_geo_country_code" not in built.events[0]
    roles = {(row["market"], row["evidence_role"]) for row in built.event_markets}
    assert roles == {("za", "action_geo"), ("ng", "actor1"), ("ke", "actor2")}
    assert all(row["receipt_id"].startswith("gdmr_") for row in built.event_markets)


def test_same_market_uses_action_geo_before_actor_roles():
    built = batch(
        events=(
            event(
                actor1_country_code="SF",
                actor2_country_code="SF",
                action_geo_country_code="SF",
            ),
        )
    )
    assert tuple((row["market"], row["evidence_role"]) for row in built.event_markets) == (
        ("za", "action_geo"),
    )


def test_batch_deduplicates_identical_rows_and_rejects_changed_natural_keys():
    built = batch(events=(event(), event()), gcam_rows=(gcam(), gcam()))
    assert len(built.events) == 1
    assert len(built.gcam) == 1
    with pytest.raises(ValueError, match="conflicting"):
        batch(events=(event(), event(mention_count=99)))


@pytest.mark.parametrize(
    ("events_rows", "gcam_rows", "message"),
    [
        ((), (gcam(),), "Events"),
        ((event(),), (), "GCAM"),
    ],
)
def test_batch_refuses_silent_empty_events_or_gcam(events_rows, gcam_rows, message):
    with pytest.raises(ValueError, match=message):
        build_wave1_gdelt_batch(
            events_rows=events_rows,
            gcam_rows=gcam_rows,
            run_id="run_20260903_dynamic_apply_v2_r16",
            manifest_sha256=MANIFEST,
            events_dry_run_receipt_id=EVENTS_RECEIPT,
            gcam_dry_run_receipt_id=GCAM_RECEIPT,
        )


def test_batch_enforces_deterministic_twenty_row_samples_and_hundred_row_total():
    built = batch(
        events=tuple(event(index) for index in range(30, 0, -1)),
        gcam_rows=tuple(gcam(index) for index in range(30, 0, -1)),
    )
    assert [row["GLOBALEVENTID"] for row in built.events] == list(range(1, 21))
    assert [row["document_url"] for row in built.gcam] == sorted(
        row["document_url"] for row in built.gcam
    )
    assert len(built.events) + len(built.event_markets) + len(built.gcam) == 100


class Job:
    errors = None

    def __init__(self, rows=()):
        self.rows = rows

    def result(self, **_kwargs):
        return self.rows


class Client:
    project = "ogilvy-trends-v2"
    location = "US"
    _credentials = SimpleNamespace(
        service_account_email=("intelligence-42-funded@ogilvy-trends-v2.iam.gserviceaccount.com"),
        quota_project_id="ogilvy-trends-v2",
    )

    def __init__(self, built, *, conflict=False):
        self.built = built
        self.conflict = conflict
        self.calls = []

    def query(self, sql, *, location, job_config, retry, job_retry):
        self.calls.append((sql, location, job_config, retry, job_retry))
        if job_config.dry_run:
            return Job()
        if sql.startswith("DECLARE"):
            return Job(
                (
                    {
                        "events_inserted": 0 if self.conflict else len(self.built.events),
                        "events_unchanged": 0,
                        "events_conflict": 1 if self.conflict else 0,
                        "markets_inserted": 0 if self.conflict else len(self.built.event_markets),
                        "markets_unchanged": 0,
                        "markets_conflict": 0,
                        "gcam_inserted": 0 if self.conflict else len(self.built.gcam),
                        "gcam_unchanged": 0,
                        "gcam_conflict": 0,
                    },
                )
            )
        if EVENTS_TABLE in sql:
            return Job(self.built.events)
        if MARKETS_TABLE in sql:
            return Job(self.built.event_markets)
        if GCAM_TABLE in sql:
            return Job(self.built.gcam)
        raise AssertionError("unexpected query")


def _capability_for_manifest(monkeypatch):
    from src.analysis.open_intelligence import funded_lane

    cap = SimpleNamespace(manifest_sha256=MANIFEST)
    monkeypatch.setattr(
        "src.analysis.open_intelligence.gdelt_wave1_persistence._require_wave1_execution_capability",
        lambda value, *, action: value if value is cap else pytest.fail(action),
    )
    return cap


def test_persistence_dry_run_has_no_mutation_and_uses_exact_target(monkeypatch):
    built = batch()
    client = Client(built)
    proof = persist_wave1_gdelt_batch(
        client=client,
        batch=built,
        execution_capability=_capability_for_manifest(monkeypatch),
        dry_run=True,
    )
    assert proof.dry_run is True
    assert proof.complete is False
    assert len(client.calls) == 1
    assert client.calls[0][2].dry_run is True
    assert "UPDATE " not in client.calls[0][0]
    assert "DELETE " not in client.calls[0][0]
    assert "TRUNCATE " not in client.calls[0][0]
    assert "MERGE " not in client.calls[0][0]


def test_persistence_inserts_and_reads_back_all_three_tables(monkeypatch):
    built = batch()
    client = Client(built)
    proof = persist_wave1_gdelt_batch(
        client=client,
        batch=built,
        execution_capability=_capability_for_manifest(monkeypatch),
        dry_run=False,
    )
    assert proof.complete is True
    assert dict(proof.inserted_counts) == {
        EVENTS_TABLE: 1,
        MARKETS_TABLE: 3,
        GCAM_TABLE: 1,
    }
    assert len(proof.readback_digests) == 3
    assert len(client.calls) == 5


def test_persistence_insert_anti_join_is_an_equality_on_the_natural_key(monkeypatch):
    # Refused on staging 4 Sep 2026 (job 720888b8): BigQuery plans a correlated
    # NOT EXISTS over the staged UNION ALL as a LEFT ANTISEMI JOIN and requires
    # an equality of fields from both sides; IS NOT DISTINCT FROM is refused at
    # execution, after the dry run passed. Every natural key column is NOT NULL
    # in the schemas, so equality is the same predicate.
    built = batch()
    client = Client(built)
    persist_wave1_gdelt_batch(
        client=client,
        batch=built,
        execution_capability=_capability_for_manifest(monkeypatch),
        dry_run=True,
    )
    sql = client.calls[0][0]
    inserts = [line for line in sql.split("\n") if line.startswith("INSERT INTO")]
    assert len(inserts) == 3
    assert all("WHERE NOT EXISTS (SELECT 1 FROM" in line for line in inserts)
    assert all("IS NOT DISTINCT FROM" not in line.split("WHERE NOT EXISTS")[1] for line in inserts)
    assert "target.`GLOBALEVENTID` = staged.`GLOBALEVENTID`)" in inserts[0]
    assert (
        "target.`GLOBALEVENTID` = staged.`GLOBALEVENTID` AND target.`market` = staged.`market`)"
        in inserts[1]
    )
    assert (
        "target.`document_url` = staged.`document_url` "
        "AND target.`published_at` = staged.`published_at`)"
    ) in inserts[2]


def test_persistence_types_null_literals_by_schema_column(monkeypatch):
    # Refused on staging 4 Sep 2026 (attempt 8, the dry run this time): a column
    # that is NULL in every staged row types as INT64 in a UNION ALL of literals,
    # so a nullable STRING or FLOAT64 target column has no common supertype with
    # it under IS NOT DISTINCT FROM. Attempts 6 and 7 happened to carry a value
    # in every nullable column. Every NULL literal now names its schema type.
    built = batch(
        events=(event(actor2_country_code=None, source_url=None),),
        gcam_rows=(gcam(v10_2=None),),
    )
    client = Client(built)
    persist_wave1_gdelt_batch(
        client=client,
        batch=built,
        execution_capability=_capability_for_manifest(monkeypatch),
        dry_run=True,
    )
    sql = client.calls[0][0]
    assert "CAST(NULL AS STRING) AS `actor2_country_code`" in sql
    assert "CAST(NULL AS STRING) AS `source_url`" in sql
    assert "CAST(NULL AS FLOAT64) AS `v10_2`" in sql
    assert " NULL AS `" not in sql
    empty = batch(events=(event(),), gcam_rows=(gcam(),))
    empty_client = Client(empty)
    persist_wave1_gdelt_batch(
        client=empty_client,
        batch=empty,
        execution_capability=_capability_for_manifest(monkeypatch),
        dry_run=True,
    )
    assert " NULL AS `" not in empty_client.calls[0][0]


def test_market_receipt_is_provenance_of_the_first_write_not_immutable_content(monkeypatch):
    # Refused the tenth Wave 1 pilot on staging, 4 Sep 2026 (job 93e92ae8,
    # immutable_conflict on the market rows): the market receipt_id hashes the
    # manifest and the dry-run receipt, both new on every execution, so a
    # re-run of the same GLOBALEVENTID and market always conflicted while the
    # event and GCAM rows, which carry no run identity, matched. The receipt is
    # provenance of the first write: the conflict rule and the readback compare
    # the market on its key and evidence role, and a persisted row keeps its
    # first receipt.
    built = batch()
    client = Client(built)
    persist_wave1_gdelt_batch(
        client=client,
        batch=built,
        execution_capability=_capability_for_manifest(monkeypatch),
        dry_run=True,
    )
    sql = client.calls[0][0]
    markets_conflict = next(
        line for line in sql.split("\n") if line.startswith("SET markets_conflict")
    )
    assert "target.`evidence_role` IS NOT DISTINCT FROM staged.`evidence_role`" in markets_conflict
    assert "target.`receipt_id`" not in markets_conflict
    markets_insert = next(
        line for line in sql.split("\n") if line.startswith("INSERT INTO") and MARKETS_TABLE in line
    )
    assert "`receipt_id`" in markets_insert

    class FirstWriteClient(Client):
        def query(self, sql, *, location, job_config, retry, job_retry):
            job = super().query(
                sql, location=location, job_config=job_config, retry=retry, job_retry=job_retry
            )
            if not job_config.dry_run and MARKETS_TABLE in sql and not sql.startswith("DECLARE"):
                return Job(
                    tuple(
                        {**row, "receipt_id": "gdmr_" + "f" * 64}
                        for row in self.built.event_markets
                    )
                )
            return job

    proof = persist_wave1_gdelt_batch(
        client=FirstWriteClient(built),
        batch=built,
        execution_capability=_capability_for_manifest(monkeypatch),
        dry_run=False,
    )
    assert proof.complete is True


def test_persistence_conflict_fails_closed_before_readback(monkeypatch):
    built = batch()
    client = Client(built, conflict=True)
    with pytest.raises(GDELTImmutableConflict):
        persist_wave1_gdelt_batch(
            client=client,
            batch=built,
            execution_capability=_capability_for_manifest(monkeypatch),
            dry_run=False,
        )
    assert len(client.calls) == 2


def test_persistence_refuses_foreign_identity_before_query(monkeypatch):
    built = batch()
    client = Client(built)
    client._credentials = SimpleNamespace(
        service_account_email="other@ogilvy-trends-v2.iam.gserviceaccount.com",
        quota_project_id="ogilvy-trends-v2",
    )
    with pytest.raises(ValueError, match="target"):
        persist_wave1_gdelt_batch(
            client=client,
            batch=built,
            execution_capability=_capability_for_manifest(monkeypatch),
            dry_run=True,
        )
    assert client.calls == []
