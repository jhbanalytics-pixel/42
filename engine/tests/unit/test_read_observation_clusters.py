import json
import re
from datetime import UTC, date, datetime

import pytest
from scripts.staging import read_observation_clusters as tool

CUTOFF = date(2026, 9, 7)
CAP = 50_000_000
SNAPSHOT_ENRICHED = tool.snapshot_table(CUTOFF, "enriched_content")
SNAPSHOT_RAW = tool.snapshot_table(CUTOFF, "raw_content")
DEV_ENRICHED = tool.producer_table("enriched_content")
DEV_RAW = tool.producer_table("raw_content")
CONTENT = {
    "title": "A headline nobody may paste",
    "text": "Body text that must never reach the receipt",
    "author_name": "Private Author Name",
    "author_handle": "private_author_handle",
}
URLS = {
    "shared": "https://example.test/private/shared-post",
    "news": "https://example.test/private/news-story",
    "forum": "https://example.test/private/forum-thread",
    "twice": "https://example.test/private/written-twice",
}


def _row(identifier, *, market, platform, source, url, hour, native_id=None, likes=0.0):
    return {
        "id": identifier,
        "source": source,
        "platform": platform,
        "market": market,
        "url": url,
        "published_at": datetime(2026, 9, 6, 9, tzinfo=UTC),
        "collected_at": datetime(2026, 9, 7, hour, tzinfo=UTC),
        "pipeline_run_id": f"run-{hour}",
        "query_term": "night commute",
        "likes": likes,
        "native_id": native_id,
        **CONTENT,
    }


def corpus():
    return [
        _row("u-dup", market="za", platform="reddit", source="reddit", url=URLS["twice"], hour=1),
        _row(
            "u-dup",
            market="za",
            platform="reddit",
            source="reddit",
            url=URLS["twice"],
            hour=5,
            likes=4.0,
        ),
        _row(
            "u-a",
            market="za",
            platform="youtube",
            source="youtube",
            url=URLS["shared"],
            hour=2,
            native_id="vid-1",
        ),
        _row(
            "u-b",
            market="za",
            platform="youtube",
            source="youtube",
            url=URLS["shared"],
            hour=6,
            native_id="vid-1",
            likes=9.0,
        ),
        {
            **_row("u-c", market="ng", platform="web", source="News24", url=URLS["news"], hour=3),
            "text": CONTENT["text"] + " about the news story",
        },
        {
            **_row(
                "u-d", market="ng", platform="reddit", source="reddit", url=URLS["forum"], hour=4
            ),
            "text": CONTENT["text"] + " about the forum thread",
        },
    ]


class FakeJob:
    def __init__(self, rows, processed, billed):
        self.rows = rows
        self.total_bytes_processed = processed
        self.total_bytes_billed = billed
        self.job_id = f"job-{id(self)}"

    def result(self):
        return list(self.rows)


class FakeClient:
    """Answers the tool's statements over an in memory corpus, recording every job."""

    def __init__(self, *, native=False, dev_rows=None, dry_bytes=None, dev_extra=()):
        self.native = native
        self.dev_extra = list(dev_extra)
        self.tables = {SNAPSHOT_ENRICHED: corpus(), SNAPSHOT_RAW: corpus()}
        self.tables[DEV_ENRICHED] = corpus() if dev_rows is None else dev_rows
        self.tables[DEV_RAW] = corpus()
        self.dry_bytes = dry_bytes or {}
        self.calls = []

    def columns(self, table):
        names = [*tool.pipeline.EVIDENCE_COLUMNS_BY_TABLE["enriched_content"]]
        if table.endswith("raw_content"):
            names = [*tool.pipeline.EVIDENCE_COLUMNS_BY_TABLE["raw_content"]]
        # The legacy capture removed the authority columns; native_id is present only
        # when this fake says so, and only on the enriched snapshot.
        names = [name for name in names if name not in tool.pipeline.WAVE1_AUTHORITY_COLUMNS]
        if self.native and table == SNAPSHOT_ENRICHED:
            names.append("native_id")
        if table == DEV_ENRICHED:
            names += self.dev_extra
        return names

    def query(self, sql, job_config):
        params = {
            item.name: getattr(item, "values", getattr(item, "value", None))
            for item in job_config.query_parameters
        }
        self.calls.append(
            {
                "sql": sql,
                "dry_run": job_config.dry_run,
                "cap": job_config.maximum_bytes_billed,
                "params": params,
            }
        )
        processed = next((value for key, value in self.dry_bytes.items() if key in sql), 1_000_000)
        if job_config.dry_run:
            return FakeJob([], processed, 0)
        return FakeJob(self._answer(sql, params), processed, processed)

    def _answer(self, sql, params):
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            dataset = tool.STAGING_DATASET if tool.STAGING_DATASET in sql else tool.DEV_DATASET
            return [
                {"table_name": name, "column_name": column}
                for name in params["tables"]
                for column in self.columns(f"{tool.PROJECT}.{dataset}.{name}")
            ]
        # Every data statement filters on the partition column, and every cluster and pair
        # statement relies on its tie break and on refusing blank keys; the fake answers
        # only a statement that carries them.
        assert "WHERE collected_at >= @window_start AND collected_at < @window_end" in sql
        table = re.search(r"FROM `([^`]+)`", sql).group(1)
        rows = [
            row
            for row in self.tables[table]
            if params["window_start"] <= row["collected_at"] < params["window_end"]
        ]
        if "id IN UNNEST(@ids)" in sql:
            wanted = re.findall(r"`([a-z_0-9]+)`", sql.split(" FROM ")[0])
            return [
                {column: row.get(column) for column in wanted}
                for row in rows
                if row["id"] in params["ids"]
            ]
        if "GROUP BY market, source" in sql:
            for clause in (
                "TRIM(source) != ''",
                "TRIM(url) != ''",
                "TRIM(text) != ''",
                "ORDER BY id LIMIT 1",
                "ORDER BY market, source LIMIT",
            ):
                assert clause in sql
            groups = {}
            for row in rows:
                if row["source"].strip() and row["url"].strip() and row["text"].strip():
                    groups.setdefault((row["market"], row["source"]), []).append(row)
            output = []
            for (market, source), members in sorted(groups.items()):
                pick = min(members, key=lambda row: row["id"])
                output.append(
                    {
                        "market": market,
                        "source": source,
                        "id": pick["id"],
                        "url_sha256": tool._sha256(pick["url"]),
                        "text_sha256": tool._sha256(pick["text"]),
                    }
                )
            return output
        if "HAVING COUNT(*) > 1" in sql:
            kind = "id"
            assert "ORDER BY row_count DESC, market, platform, id LIMIT" in sql
        else:
            kind = re.search(r"SHA256\((\w+)\)", sql)[1]
            for clause in (
                f"TRIM({kind}) != ''",
                "ORDER BY id_count DESC, row_count DESC, market, platform, key_sha256 LIMIT",
                f"ARRAY_AGG(DISTINCT id ORDER BY id LIMIT {tool.MEMBER_LIMIT})",
            ):
                assert clause in sql
        groups = {}
        for row in rows:
            if row.get(kind):
                groups.setdefault((row["market"], row["platform"], row[kind]), []).append(row)
        output = []
        for (market, platform, key), members in groups.items():
            ids = sorted({row["id"] for row in members})
            if (kind == "id" and len(members) > 1) or (kind != "id" and len(ids) > 1):
                output.append(
                    {
                        "market": market,
                        "platform": platform,
                        **({"id": key} if kind == "id" else {"key_sha256": tool._sha256(key)}),
                        "row_count": len(members),
                        "id_count": len(ids),
                        "day_count": 1,
                        "ids": ids[: tool.MEMBER_LIMIT],
                    }
                )
        output.sort(key=lambda item: (-item["id_count"], -item["row_count"], item["market"]))
        return output[: tool.CLUSTER_LIMIT]


def read(client, cap=CAP):
    return tool.read_clusters(client, cutoff=CUTOFF, window_days=7, maximum_bytes_billed=cap)


def test_columns_are_read_first_and_native_id_is_used_only_where_present():
    client = FakeClient(native=False)
    receipt = read(client)
    assert "INFORMATION_SCHEMA.COLUMNS" in client.calls[0]["sql"]
    assert "INFORMATION_SCHEMA.COLUMNS" in client.calls[2]["sql"]
    assert receipt["cluster_kinds"] == ["same_id", "same_url"]
    assert "same_native_id" not in receipt["clusters"]
    assert not any("native_id" in call["sql"] for call in client.calls)
    assert receipt["columns"][SNAPSHOT_ENRICHED]["has_native_id"] is False

    native = FakeClient(native=True)
    receipt = read(native)
    assert receipt["cluster_kinds"] == ["same_id", "same_native_id", "same_url"]
    assert receipt["columns"][SNAPSHOT_ENRICHED]["has_native_id"] is True
    assert receipt["columns"][SNAPSHOT_ENRICHED]["authority_columns"] == ["native_id"]
    assert receipt["clusters"]["same_native_id"][0]["members"] == ["u-a", "u-b"]
    traced = [
        call["sql"]
        for call in native.calls
        if "id IN UNNEST(@ids)" in call["sql"] and not call["dry_run"]
    ]
    assert [("`native_id`" in sql) for sql in traced] == [True, False, False, False]
    # The snapshot carries a column its producer table lacks; identical data still matches.
    assert receipt["column_differences"]["enriched_content"]["only_in_snapshot"] == ["native_id"]
    for group in receipt["clusters"].values():
        for item in group:
            assert item["producer_matches_snapshot"] == {
                "enriched_content": True,
                "raw_content": True,
            }


def test_a_producer_table_that_gained_a_column_still_matches_identical_data():
    receipt = read(FakeClient(dev_extra=["endpoint"]))
    differences = receipt["column_differences"]["enriched_content"]
    assert differences["only_in_producer"] == ["endpoint"]
    assert differences["only_in_snapshot"] == []
    for group in receipt["clusters"].values():
        for item in group:
            assert item["producer_matches_snapshot"] == {
                "enriched_content": True,
                "raw_content": True,
            }
    assert receipt["distinct_source_pair"]["producer_matches_snapshot"]["enriched_content"]


def test_a_table_missing_a_required_column_is_refused():
    client = FakeClient()
    client.columns = lambda table: ["id", "market"]
    with pytest.raises(tool.ReadRefused, match="missing columns"):
        read(client)


def test_every_statement_is_dry_run_first_under_the_cap_and_filtered_on_collected_at():
    client = FakeClient()
    receipt = read(client)
    assert len(client.calls) == 2 * len(receipt["queries"])
    for dry, real in zip(client.calls[::2], client.calls[1::2], strict=True):
        assert dry["dry_run"] is True
        assert real["dry_run"] is False
        assert dry["sql"] == real["sql"]
        assert dry["cap"] == real["cap"] == CAP
        if "INFORMATION_SCHEMA" not in dry["sql"]:
            assert "collected_at >= @window_start AND collected_at < @window_end" in dry["sql"]
    assert all(entry["dry_run_bytes"] == 1_000_000 for entry in receipt["queries"])
    assert receipt["dry_run_bytes_total"] == 1_000_000 * len(receipt["queries"])
    assert receipt["maximum_bytes_billed"] == CAP
    assert receipt["window"] == {
        "start": "2026-09-01T00:00:00+00:00",
        "end": "2026-09-08T00:00:00+00:00",
    }


def test_a_dry_run_above_the_cap_refuses_before_the_statement_runs():
    client = FakeClient(dry_bytes={"SHA256(url)": CAP + 1})
    with pytest.raises(tool.ReadRefused, match="above the cap"):
        read(client)
    over = [call for call in client.calls if "SHA256(url)" in call["sql"]]
    assert [call["dry_run"] for call in over] == [True]


@pytest.mark.parametrize("cap", [0, -1, 1.5, None])
def test_a_cap_that_is_not_a_positive_integer_is_refused(cap):
    with pytest.raises(tool.ReadRefused):
        tool.Reader(FakeClient(), maximum_bytes_billed=cap)


def test_the_default_cap_is_small():
    assert tool.DEFAULT_MAXIMUM_BYTES_BILLED <= 1024**3


def test_clusters_and_the_distinct_source_pair_are_chosen_from_the_corpus():
    receipt = read(FakeClient())
    [by_id] = receipt["clusters"]["same_id"]
    assert (by_id["market"], by_id["platform"], by_id["id"]) == ("za", "reddit", "u-dup")
    assert by_id["row_count"] == 2
    assert by_id["members"] == ["u-dup"]
    [by_url] = receipt["clusters"]["same_url"]
    assert by_url["members"] == ["u-a", "u-b"]
    assert by_url["key_sha256"] == tool._sha256(URLS["shared"])
    pair = receipt["distinct_source_pair"]
    assert pair["market"] == "ng"
    assert pair["members"] == ["u-c", "u-d"]
    sources = {entry["source"] for entry in pair["trace"]}
    assert sources == {"News24", "reddit"}
    assert len({entry["url_sha256"] for entry in pair["trace"]}) == 2
    assert pair["origin_independence"] == "unknown"
    assert "not an origin" in pair["origin_independence_note"]
    assert "separate_origin_pair" not in receipt


def _candidate(market, source, identifier, url, text):
    return {
        "market": market,
        "source": source,
        "id": identifier,
        "url_sha256": url * 64,
        "text_sha256": text * 64,
    }


def test_the_pair_requires_distinct_ids_url_digests_and_text_digests():
    rows = [
        _candidate("ke", "rss", "k1", "a", "a"),
        _candidate("ng", "a", "n1", "a", "a"),
        _candidate("ng", "b", "n1", "b", "b"),
        _candidate("ng", "c", "n2", "a", "c"),
        _candidate("ng", "d", "n3", "d", "a"),
        _candidate("ng", "e", "n4", "e", "e"),
    ]
    pair = tool.choose_distinct_source_pair(rows)
    assert pair["market"] == "ng"
    assert pair["members"] == ["n1", "n4"]
    assert pair["origin_independence"] == "unknown"
    assert tool.choose_distinct_source_pair(rows[:1]) is None
    assert tool.choose_distinct_source_pair(rows[:4]) is None


def test_a_cluster_larger_than_the_member_limit_says_its_verdict_covers_a_sample(monkeypatch):
    monkeypatch.setattr(tool, "MEMBER_LIMIT", 1)
    receipt = read(FakeClient())
    [by_url] = receipt["clusters"]["same_url"]
    assert by_url["members"] == ["u-a"]
    assert by_url["id_count"] == 2
    assert by_url["member_count"] == 2
    assert by_url["traced_member_count"] == 1
    assert by_url["admission_scope"] == "sampled_members"
    monkeypatch.undo()
    full = read(FakeClient())
    assert full["clusters"]["same_url"][0]["admission_scope"] == "all_members"


def test_each_trace_runs_from_producer_to_snapshot_in_both_lanes():
    receipt = read(FakeClient())
    [by_url] = receipt["clusters"]["same_url"]
    tables = {entry["table"] for entry in by_url["trace"]}
    assert tables == {SNAPSHOT_ENRICHED, SNAPSHOT_RAW, DEV_ENRICHED, DEV_RAW}
    assert all(set(entry) == set(tool.TRACE_FIELDS) for entry in by_url["trace"])
    assert by_url["producer_matches_snapshot"] == {"enriched_content": True, "raw_content": True}
    changed = corpus()
    changed[2]["likes"] = 99.0
    receipt = read(FakeClient(dev_rows=changed))
    [by_url] = receipt["clusters"]["same_url"]
    assert by_url["producer_matches_snapshot"] == {"enriched_content": False, "raw_content": True}


def test_the_receipt_holds_no_content(tmp_path):
    receipt = read(FakeClient(native=True))
    path = tmp_path / "receipt.json"
    tool.write_receipt(receipt, path)
    raw = path.read_text(encoding="utf-8")
    for value in [*CONTENT.values(), *URLS.values(), "vid-1", "night commute"]:
        assert value not in raw
    keys = set()

    def walk(value):
        if isinstance(value, dict):
            keys.update(value)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(json.loads(raw))
    assert not keys & {"text", "title", "author_name", "author_handle", "url"}


def test_the_receipt_is_written_once_and_an_existing_path_is_refused(tmp_path):
    path = tmp_path / "receipt.json"
    tool.write_receipt({"a": 1}, path)
    with pytest.raises(FileExistsError):
        tool.write_receipt({"a": 2}, path)
    assert json.loads(path.read_text()) == {"a": 1}
    with pytest.raises(SystemExit, match="already exists"):
        tool.main(["--receipt", str(path)])


def test_identity_results_and_admission_verdicts():
    receipt = read(FakeClient())
    [by_id] = receipt["clusters"]["same_id"]
    assert by_id["identity"]["state"] == "refused"
    assert "claims two content digests" in by_id["identity"]["reason"]
    assert by_id["admission"] == "refuses_whole_admission"

    [by_url] = receipt["clusters"]["same_url"]
    assert by_url["identity"] == {
        "state": "grouped",
        "record_count": 2,
        "observation_count": 2,
        "independent_origin_count": 0,
        "unknown_origin_count": 2,
        "origin_authority_projection": {
            "state": "not_projected",
            "projected_records": 0,
            "unprojected_records": 2,
        },
    }
    assert by_url["admission"] == "admits_separate_observations"

    pair = receipt["distinct_source_pair"]
    assert pair["identity"]["observation_count"] == 2
    assert pair["identity"]["unknown_origin_count"] == 2
    assert pair["identity"]["independent_origin_count"] == 0
    assert pair["admission"] == "admits_separate_observations"


def test_a_byte_identical_double_write_collapses_in_the_kernel_but_admission_refuses_it():
    entry = {
        "table": SNAPSHOT_ENRICHED,
        "id": "u-dup",
        "market": "za",
        "platform": "reddit",
        "row_sha256": "a" * 64,
    }
    identity = tool.identity_result([entry, dict(entry)])
    assert identity["state"] == "grouped"
    assert identity["observation_count"] == 1
    assert tool.admission_verdict([entry, dict(entry)]) == "refuses_whole_admission"
    assert tool.admission_verdict([entry]) == "admits_one_observation"


def test_each_cluster_reports_its_obs1_count_beside_its_row_ids():
    """The re-collection finding, measured: two row ids of one post share one obs1."""
    receipt = read(FakeClient())
    assert receipt["contract_version"] == "observation_cluster_read_v2"
    [by_url] = receipt["clusters"]["same_url"]
    for entry in by_url["trace"]:
        assert entry["observation_id"].startswith("obs1_")
        assert entry["observation_key_kind"] == "url"
    assert by_url["observation_ids"] == {
        "scheme": "obs1",
        "distinct_row_ids": 2,
        "distinct_observation_ids": 1,
        "underived_rows": 0,
        "key_kinds": {"by_url": 2},
    }
    pair = receipt["distinct_source_pair"]
    assert pair["observation_ids"]["distinct_row_ids"] == 2
    assert pair["observation_ids"]["distinct_observation_ids"] == 2
    [by_id] = receipt["clusters"]["same_id"]
    assert by_id["observation_ids"]["distinct_row_ids"] == 1
    assert by_id["observation_ids"]["distinct_observation_ids"] == 1

    native = read(FakeClient(native=True))
    [by_native] = native["clusters"]["same_native_id"]
    assert by_native["observation_ids"]["key_kinds"] == {"by_native": 2}
    assert by_native["observation_ids"]["distinct_observation_ids"] == 1
