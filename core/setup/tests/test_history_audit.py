"""Unit tests for history_audit.py. No cloud access: a fake BigQuery client records every query, in the pattern
of core/setup/tests/test_inventory.py. Run from the 42 repo root:

    py -3.13 -m pytest -q core/setup/tests/test_history_audit.py
"""
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

import pytest
from google.api_core.exceptions import NotFound

from core.setup import history_audit as ha

HERE = Path(ha.__file__).resolve().parent

GB = 1024 ** 3


class FakeJob:
    def __init__(self, rows, size):
        self.rows = rows
        self.total_bytes_processed = size
        self.total_bytes_billed = size

    def result(self):
        return self.rows


class FakeClient:
    """Answers every real query with rows from answers (matched on a substring of the SQL), else one count row."""

    def __init__(self, answers=None, estimates=None, missing=()):
        self.answers = answers or {}
        self.estimates = estimates or {}
        self.missing = missing
        self.calls = []

    def query(self, sql, job_config=None, location=None):
        dry = bool(job_config is not None and job_config.dry_run)
        self.calls.append({"sql": sql, "dry": dry, "config": job_config, "location": location})
        if any(m in sql for m in self.missing):
            raise NotFound("Not found: Table ogilvy-trends-v2:intelligence_42_core.x")
        size = next((b for key, b in self.estimates.items() if key in sql), 1000)
        if dry:
            return FakeJob([], size)
        rows = next((r for key, r in self.answers.items() if key in sql), [{"total_rows": 3, "market": "ZA"}])
        return FakeJob(rows, size)

    def real(self):
        return [c for c in self.calls if not c["dry"]]


def run(tmp_path, client, *extra):
    out = tmp_path / "history-audit.md"
    assert ha.main(["--out", str(out), *extra], client=client) == 0
    return out.read_text(encoding="utf-8")


def test_every_planned_statement_is_one_read_only_select_on_the_project():
    qs = ha.queries()
    assert len(qs) >= 60
    assert len({q.name for q in qs}) == len(qs)
    for q in qs:
        ha.check_statement(q)
        assert q.sql.startswith(("SELECT ", "WITH ")) and ";" not in q.sql
        assert not ha.DML.search(q.sql)
        assert "ogilvy-trends-v2" in q.sql


def test_statement_guard_refuses_writes_stars_content_and_other_projects():
    bad = ["".join(["DEL", "ETE FROM t WHERE TRUE"]), "SELECT 1; SELECT 2", "SELECT * FROM `ogilvy-trends-v2`.d.t",
           "SELECT p.text FROM `ogilvy-trends-v2`.d.posts p", "SELECT p.url FROM `ogilvy-trends-v2`.d.posts p",
           "SELECT COUNT(*) AS n FROM `other-project`.d.t", "SELECT m.label FROM `ogilvy-trends-v2`.d.cultural_map m",
           "WITH a AS (SELECT 1 AS x) SELECT a.x FROM a JOIN `ogilvy-trends-v2`.d.t b ON TRUE WHERE "
           + "".join(["MER", "GE"]) + " = 1"]
    for sql in bad:
        with pytest.raises(ValueError):
            ha.check_statement(ha.Query("bad", "", sql))
    ha.check_statement(ha.Query("ok", "", "SELECT m.label != 'x' AS c FROM `ogilvy-trends-v2`.d.m m",
                                reads_content=True))


def test_only_label_keyword_and_centroid_queries_read_them_and_return_numbers(tmp_path):
    readers = {q.name for q in ha.queries() if q.reads_content}
    assert readers == {"label_churn", "cluster_transitions"}
    for q in ha.queries():
        if q.reads_content:
            head = q.sql.rsplit("SELECT", 1)[1].split(" FROM ")[0]
            assert not ha.CONTENT_INNER.search(head), q.name


def test_every_real_query_follows_its_own_dry_run_and_is_capped(tmp_path):
    client = FakeClient()
    run(tmp_path, client)
    assert client.calls
    for i, call in enumerate(client.calls):
        assert call["location"] == "US"
        if call["dry"]:
            assert call["config"].use_query_cache is False
            continue
        before = client.calls[i - 1]
        assert before["dry"] and before["sql"] == call["sql"]
        assert call["config"].maximum_bytes_billed == 2 * GB
    assert len(client.real()) * 2 == len(client.calls)


def test_since_is_a_bound_date_parameter(tmp_path):
    client = FakeClient()
    run(tmp_path, client, "--since", "2026-09-20")
    bound = [c for c in client.real() if "@since" in c["sql"]]
    assert bound
    for c in bound:
        (p,) = [p for p in c["config"].query_parameters if p.name == "since"]
        assert p.type_ == "DATE" and p.value == dt.date(2026, 9, 20)


def test_a_query_over_the_cap_is_refused_and_the_rest_still_runs(tmp_path):
    key = "FROM `ogilvy-trends-v2`.intelligence_42_core.posts ps GROUP BY"
    client = FakeClient(estimates={key: 3 * GB})
    md = run(tmp_path, client)
    assert not [c for c in client.real() if key in c["sql"]]
    assert [c for c in client.calls if c["dry"] and key in c["sql"]]
    assert "## post_dates" in md and "Not shown: refused" in md
    assert "## waves" in md


def test_the_run_stops_before_passing_its_total_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(ha, "TOTAL_BYTES", 5000)
    client = FakeClient()
    md = run(tmp_path, client)
    assert len(client.real()) == 5
    assert "budget" in md


def test_a_missing_table_is_recorded_and_the_rest_still_runs(tmp_path):
    client = FakeClient(missing=("intelligence_42_core.early_signal t",))
    md = run(tmp_path, client)
    assert "## span_early_signal" in md and "Not shown: not found" in md
    assert "## span_test_switch" in md


def test_rows_with_text_shaped_values_are_withheld_whole(tmp_path):
    leak = [{"market": "ZA", "posts": 4}, {"market": "ZA", "posts": "a caption someone wrote, with @someone"}]
    long_code = [{"series": "x" * 200, "days": 1}]
    odd = [{"platform": "tiktok <script>", "posts": 1}]
    client = FakeClient(answers={"po.lane, po.platform": leak, "MIN(h.day) AS first_day, MAX(h.day) AS last_day, "
                                 "COUNT(DISTINCT h.day) AS days, COUNT(DISTINCT IF(h.valid": long_code,
                                 "ps.creator_tier_at_post, 'none')": odd})
    md = run(tmp_path, client)
    assert "caption" not in md and "someone" not in md and "script" not in md and "x" * 200 not in md
    assert md.count("Not shown: withheld") == 4  # the long series code matches health_by_series and protocol_starts


def test_safe_values_pass_and_render(tmp_path):
    rows = [{"market": "KE", "series": "panel_culture_desk", "lane_class": "panel", "first_day": dt.date(2026, 10, 12),
             "valid_days": 4, "mean_located_share": 0.12, "v2_token": True, "protocol_hash": "0a1b2c3d4e"}]
    client = FakeClient(answers={"SUBSTR(TO_HEX(SHA256(h.protocol)), 1, 10)": rows})
    md = run(tmp_path, client)
    assert "| KE | panel_culture_desk | panel | 2026-10-12 | 4 | 0.12 | True | 0a1b2c3d4e |" in md


def test_protocols_are_never_returned_as_written():
    for q in ha.queries():
        head = q.sql.rsplit("SELECT", 1)[1].split(" FROM ")[0]
        # the final SELECT may feed h.protocol to a hash, a count or a test; it never outputs it as written
        assert not re.search(r"(^|,)\s*(DISTINCT\s+)?\w*\.?protocol\s*(,|AS\b|$)", head), q.name


def test_plan_prints_every_query_and_needs_no_client(capsys, monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("plan must not build a client")
    monkeypatch.setattr(ha.bigquery, "Client", no_network)
    assert ha.main(["--plan"]) == 0
    out = capsys.readouterr().out
    for q in ha.queries():
        assert q.name in out
    assert "dry run" in out.lower()


def test_plan_runs_as_a_script_without_network():
    root = Path(ha.inv.__file__).resolve().parents[2]
    proc = subprocess.run([sys.executable, "-m", "core.setup.history_audit", "--plan"], capture_output=True,
                          encoding="utf-8", cwd=root, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    assert "v_collection_health_current" in proc.stdout


def test_no_banned_literals_or_dashes_in_the_audit_code():
    words = ["".join(["gen", "z"]), "".join(["gen", " z"]), "".join(["google", "_trends"]),
             "".join(["google", " trends"])]
    banned = re.compile("|".join(words), re.IGNORECASE)
    for path in (HERE / "history_audit.py", Path(__file__)):
        text = path.read_text(encoding="utf-8")
        assert not banned.search(text), path
        assert chr(0x2014) not in text and chr(0x2013) not in text, path
        assert not re.search(r"(?<![\s\[\"'(])-{2}|-{2}(?![a-z])", text), path


@pytest.mark.parametrize("value", [["a", "b"], {"text": "x"}, ("x",), b"raw"])
def test_arrays_structs_and_bytes_are_withheld_even_in_a_category_column(value):
    with pytest.raises(ha.Withheld):
        ha.guard_rows([{"market": value}])
