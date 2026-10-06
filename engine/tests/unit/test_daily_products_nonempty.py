import json
import re
from collections import Counter
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pandas as pd
from src.analysis.gemini_client import GeminiClient
from src.analysis.open_intelligence.daily_products import run_legacy_products

from tests.unit.test_generate_briefs import _FAT_SAMPLE, _mock_brief_response
from tests.unit.test_seed_graph import _row

DAY = date(2026, 9, 19)
CUTOFF = datetime(2026, 9, 20, tzinfo=UTC)


class Row(dict):
    def __getattr__(self, name):
        return self[name]


def captured_rows():
    rows = []
    for market in ("za", "ng", "ke"):
        for topic in ("music_amapiano", "food_rituals_braai"):
            for index, sample in enumerate(_FAT_SAMPLE * 3):
                rows.append(
                    _row(
                        **sample,
                        id=f"{market}:{topic}:{index}",
                        market=market,
                        topic_groups=[topic],
                        collected_at=f"{DAY}T12:00:00+00:00",
                        published_at=f"{DAY}T11:00:00+00:00",
                        source="socialcrawl",
                        author_handle=f"creator{index}",
                        author_handle_norm=f"creator{index}",
                        genz_score=0.9,
                        slang_score=0.9,
                        slang_terms="newdancewave",
                        regional_score=0.9,
                        engagement_weighted=1000.0,
                        creator_watchlist_score=0.8,
                        search_velocity_score=0.5,
                        engagement_total=1000,
                        views=1000,
                        likes=50,
                        comments=10,
                        shares=4,
                    )
                )
    return pd.DataFrame(rows)


class Replies:
    def __init__(self):
        self.calls = 0

    def generate_content(self, *, config, **kwargs):
        self.calls += 1
        properties = config.response_schema["properties"]
        if "seeds" in properties:
            parsed = {
                "seeds": [
                    {
                        "behaviour": "Shared dance rituals",
                        "the_shift": "New group routines",
                        "evidence": ["za/music_amapiano", "ng/music_amapiano"],
                        "why_hidden": "Small groups",
                        "timing": "now",
                        "markets": ["za", "ng"],
                        "brand_opportunity": "Support creators",
                        "signal_strength": "strong",
                        "activation": {
                            "tool": "Lyria",
                            "angle": "group dance",
                            "prompt": "Original rhythm",
                        },
                    }
                ]
            }
        elif "summary_text" in properties:
            parsed = {
                "summary_text": "Music and food rituals are growing across the markets.",
                "through_line": "Shared everyday rituals.",
                "call_to_action": "Explore music_amapiano and food_rituals_braai.",
                "key_topics": [],
                "rising_topics": ["music_amapiano"],
            }
        else:
            parsed = _mock_brief_response().parsed
        return SimpleNamespace(
            text=json.dumps(parsed),
            parsed=parsed,
            usage_metadata=SimpleNamespace(
                prompt_token_count=100, candidates_token_count=300, thoughts_token_count=10
            ),
        )


def _struct_columns(text):
    return [part.strip().removeprefix("target.").strip("`") for part in text.split(",")]


def _fingerprint(row, columns):
    import hashlib

    raw = json.dumps([str(row.get(column)) for column in columns]).encode()
    return int(hashlib.sha256(raw).hexdigest()[:15], 16)


def _conjuncts(text):
    parts, depth, start, index = [], 0, 0, 0
    while index < len(text):
        char = text[index]
        depth += char == "("
        depth -= char == ")"
        if depth == 0 and re.match(r"\s+AND\s+", text[index:]):
            parts.append(text[start:index].strip())
            index += len(re.match(r"\s+AND\s+", text[index:]).group(0))
            start = index
            continue
        index += 1
    parts.append(text[start:].strip())
    return parts


def _param(value):
    return getattr(value, "struct_values", value)


def _matches(where, row, params):
    for part in _conjuncts(where):
        if part == "TRUE":
            continue
        if found := re.fullmatch(r"target\.(\w+) = @(\w+)", part):
            if str(row.get(found.group(1))) != str(params[found.group(2)]):
                return False
            continue
        if found := re.fullmatch(r"target\.(\w+) IN UNNEST\(@(\w+)\)", part):
            if row.get(found.group(1)) not in params[found.group(2)]:
                return False
            continue
        if found := re.fullmatch(
            r"EXISTS \(SELECT 1 FROM UNNEST\(@(\w+)\) AS k WHERE (.+)\)", part, re.S
        ):
            pairs = [
                re.fullmatch(r"k\.(\w+) = target\.(\w+)", item).groups()
                for item in _conjuncts(found.group(2))
            ]
            if not any(
                all(_param(key)[left] == row.get(right) for left, right in pairs)
                for key in params[found.group(1)]
            ):
                return False
            continue
        if found := re.fullmatch(
            r"FARM_FINGERPRINT\(TO_JSON_STRING\(STRUCT\((.+)\)\)\) IN UNNEST\(@(\w+)\)", part
        ):
            columns = _struct_columns(found.group(1))
            if _fingerprint(row, columns) not in params[found.group(2)]:
                return False
            continue
        raise AssertionError("unsupported predicate: " + part)
    return True


class Warehouse:
    project = "ogilvy-trends-v2"
    location = "us-central1"

    def __init__(self, io):
        self.io = io

    def query(self, sql, *, job_config=None, **kwargs):
        self.io.queries.append(sql)
        params = (
            {
                p.name: p.values if hasattr(p, "values") else p.value
                for p in job_config.query_parameters
                if hasattr(p, "value") or hasattr(p, "values")
            }
            if job_config
            else {}
        )
        rows = self._rows(sql, params)
        return SimpleNamespace(
            result=lambda **kwargs: [Row(row) for row in rows],
            to_dataframe=lambda: pd.DataFrame(rows),
            total_bytes_billed=10,
        )

    # A small evaluator for the predicates the product boundary sends, applied as written:
    # a widened, narrowed or unscoped predicate deletes what BigQuery would delete.
    def _delete(self, statement, params):
        table = statement.split("`", 2)[1].rsplit(".", 1)[1]
        where = statement.split("\nWHERE ", 1)[1]
        if self.io.before_delete is not None:
            self.io.before_delete(table)
        return table, [row for row in self.io.rows.get(table, []) if _matches(where, row, params)]

    def _apply(self, table, deleted):
        stored = self.io.rows.get(table, [])
        self.io.rows[table] = [row for row in stored if not any(row is gone for gone in deleted)]
        self.io.deletes.append((table, len(deleted)))

    # The script runs as BigQuery runs one: outside a transaction each statement commits as
    # it finishes, inside one nothing lands before COMMIT TRANSACTION, a failed statement
    # discards the open transaction, and a transaction the script leaves open never commits.
    def _script(self, sql, params):
        pending, row_count = None, None
        for statement in [part.strip() for part in sql.split(";\n") if part.strip()]:
            statement = statement.removesuffix(";")
            if statement == "BEGIN TRANSACTION":
                if pending is not None:
                    raise ValueError("transaction already open")
                pending = []
            elif statement == "COMMIT TRANSACTION":
                if pending is None:
                    raise ValueError("no transaction to commit")
                for table, deleted in pending:
                    self._apply(table, deleted)
                pending = None
            elif statement.startswith("DELETE FROM"):
                table, deleted = self._delete(statement, params)
                row_count = len(deleted)
                if pending is None:
                    self._apply(table, deleted)
                else:
                    pending.append((table, deleted))
            elif asserted := re.fullmatch(r"ASSERT @@row_count = @(\w+) AS '(\w+)'", statement):
                if row_count != params[asserted.group(1)]:
                    raise ValueError("assertion failed: " + asserted.group(2))
            else:
                raise AssertionError("unmodelled script statement: " + statement)
        return []

    def _fingerprinted(self, sql, params):
        table = sql.split("FROM `", 1)[1].split("`", 1)[0].rsplit(".", 1)[1]
        columns = _struct_columns(sql.split("STRUCT(", 1)[1].split(")", 1)[0])
        where = sql.split("\nWHERE ", 1)[1]
        return [
            {**row, "_fingerprint": _fingerprint(row, columns)}
            for row in self.io.rows.get(table, [])
            if _matches(where, row, params)
        ]

    def _rows(self, sql, params):
        if sql.startswith("BEGIN TRANSACTION;") or sql.startswith("DELETE FROM"):
            return self._script(sql, params)
        if sql.startswith("SELECT FARM_FINGERPRINT("):
            return self._fingerprinted(sql, params)
        if ".trend_scores`" in sql:
            rows = self.io.rows.get("trend_scores", [])
            if "AS topic_group" in sql:
                return [{**row, "topic_group": row["query_group"]} for row in rows]
            return rows
        if ".seed_graph`" in sql:
            return [] if "AVG(row_count)" in sql else self.io.rows.get("seed_graph", [])
        if ".v_seed_first_seen`" in sql:
            return self.io.read_rows("v_seed_first_seen", market=params.get("market"))
        if ".seed_candidates`" in sql:
            rows = [
                row
                for row in self.io.rows.get("seed_candidates", [])
                if not params.get("market") or row["market"] == params["market"]
            ]
            if "GROUP BY market" in sql:
                return [
                    {"market": market, "n": count}
                    for market, count in Counter(row["market"] for row in rows).items()
                ]
            if "COUNT(*)" in sql:
                return [{"n": len(rows)}]
            return rows
        # The legacy existence reads answer from the stored rows, as a real warehouse does.
        if ".daily_summary`" in sql and "COUNT(*)" in sql:
            rows = self.io.rows.get("daily_summary", [])
            if "COALESCE(summary_text" in sql:
                rows = [
                    row
                    for row in rows
                    if row.get("summary_text")
                    and row.get("through_line")
                    and row.get("call_to_action")
                ]
            return [{"n": len(rows)}]
        if ".seed_insights`" in sql and "COUNT(*)" in sql:
            return [{"n": len(self.io.rows.get("seed_insights", []))}]
        if ".trend_analysis`" in sql and "SELECT DISTINCT market, query_group" in sql:
            return [
                {"market": row["market"], "topic_group": row["query_group"]}
                for row in self.io.rows.get("trend_analysis", [])
            ]
        if ".trend_analysis`" in sql and "@prior_date" in sql:
            return [
                row
                for row in self.io.rows.get("trend_analysis", [])
                if str(row["trend_date"]) == str(params.get("prior_date"))
            ]
        if ".raw_content`" in sql:
            return []
        if ".enriched_content`" in sql:
            rows = self.io.enriched.to_dict("records")
            rows = [
                row for row in rows if not params.get("market") or row["market"] == params["market"]
            ]
            rows = [
                row
                for row in rows
                if not params.get("topic_group") or params["topic_group"] in row["topic_groups"]
            ]
            if "SELECT title, text" in sql:
                return rows[: params.get("lim", 100)]
            if "MAX(CAST(engagement_total" in sql:
                keys = {(row["market"], topic) for row in rows for topic in row["topic_groups"]}
                return [
                    {"market": market, "topic_group": topic, "peak": 1000} for market, topic in keys
                ]
            if "GROUP BY market, topic_group, platform" in sql:
                counts = Counter(
                    (row["market"], topic, row["platform"])
                    for row in rows
                    for topic in row["topic_groups"]
                )
                return [
                    {"market": market, "topic_group": topic, "platform": platform, "n": count}
                    for (market, topic, platform), count in counts.items()
                ]
            if "author_handle" in sql or "AVG(" in sql or "COUNT(" in sql or "source" in sql:
                return []
        raise AssertionError("unimplemented fake warehouse query: " + sql)


class BoundaryIO:
    dataset = "trends_v2_staging"

    def __init__(self):
        self.enriched = captured_rows()
        self.rows = {}
        self.queries = []
        self.deletes = []
        self.before_delete = None
        self.client = Warehouse(self)
        self.models = Replies()

    def sink(self, table):
        def write(rows):
            self.rows.setdefault(table, []).extend(rows)
            return len(rows)

        return write

    def query_runner(self, sql, **kwargs):
        self.queries.append(sql)
        return pd.DataFrame()

    def read_rows(self, table, **kwargs):
        if table == "v_seed_first_seen":
            found = {(row["market"], row["term"]) for row in self.rows.get("seed_graph", [])}
            return [
                {"market": market, "term": term, "first_seen_event_date": DAY}
                for market, term in found
                if not kwargs.get("market") or market == kwargs["market"]
            ]
        rows = self.rows.get(table, [])
        if kwargs.get("identifiers"):
            rows = [row for row in rows if row["brief_id"] in kwargs["identifiers"]]
        return list(rows)

    def outcome(self, table):
        return {"table": table, "count": len(self.read_rows(table))}


def test_real_nonempty_producers_populate_seven_legacy_routes_from_captured_rows(monkeypatch):
    from src.analysis import (
        generate_briefs,
        generate_daily_summary,
        generate_seed_intelligence,
        pan_african,
        seed_candidates,
        seed_graph,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("global I/O escaped")

    for module in (
        generate_briefs,
        generate_daily_summary,
        generate_seed_intelligence,
        pan_african,
        seed_candidates,
        seed_graph,
    ):
        for name in ("get_client", "get_dataset", "insert_dataframe", "merge_dataframe"):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, forbidden)
    io = BoundaryIO()
    outputs = run_legacy_products(
        io,
        enriched=io.enriched,
        trend_date=DAY,
        cutoff=CUTOFF,
        scored_at=CUTOFF,
        gemini_client=GeminiClient(
            project=io.client.project, client=SimpleNamespace(models=io.models)
        ),
    )
    assert set(outputs) == {
        "seed_insights",
        "seed_candidates",
        "trend_analysis",
        "daily_summary",
        "v_seed_first_seen",
        "creator_briefs",
        "pan_african_stories",
    }
    assert len(io.rows["trend_analysis"]) == 6
    assert len(io.rows["creator_briefs"]) == 6
    assert len(io.rows["daily_summary"]) == 1
    assert len(io.rows["seed_insights"]) == 1
    assert io.rows["seed_candidates"]
    assert io.rows["pan_african_stories"]
    assert io.read_rows("v_seed_first_seen")
    assert {row["brief_id"] for row in io.rows["creator_briefs"]} == {
        row["analysis_id"] for row in io.rows["trend_analysis"]
    }
    assert io.models.calls >= 8
