"""Benchmark failed reads and the parameters saved beside a result, on a fake client."""
import csv
import json

from google.cloud import bigquery

from ops.runners import benchmark_moments as bm


def run_main(tmp_path, monkeypatch, answers, extra=(), moments="BB,2026-10-05,NG,BBNaija finale,bbnaija,\n"):
    path = tmp_path / "moments.csv"
    path.write_text("id,date,market,moment,any_re,and_re\n" + moments, encoding="utf-8")
    seen = []

    class Client:
        def query(self, sql, job_config=None):
            seen.append(job_config)
            rows = answers.pop(0)

            class Result:
                def result(self):
                    if isinstance(rows, Exception):
                        raise rows
                    return rows
            return Result()

    monkeypatch.setattr(bigquery, "Client", lambda **kwargs: Client())
    folder = tmp_path / "output"
    monkeypatch.setattr(bm.os.path, "expanduser", lambda p: str(folder))
    code = bm.main(["benchmark_moments.py", str(path), *extra])
    saved = sorted(folder.glob("*.csv"))
    return code, saved, folder, seen


def read_rows(path):
    with path.open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def test_a_failed_posts_read_is_unknown_not_a_confident_not_collected(tmp_path, monkeypatch, capsys):
    answers = [RuntimeError("quota"), [], [], [], [], []]
    code, [saved], _folder, _seen = run_main(tmp_path, monkeypatch, answers)
    out = capsys.readouterr().out
    [row] = read_rows(saved)
    assert code == 1
    assert row["posts_all"] == "" and row["posts_market"] == "" and row["located"] == ""
    assert row["read_failures"] == "posts"
    assert row["verdict_confidence"].startswith("unconfirmed")
    assert "NOT COLLECTED (unconfirmed: posts read failed)" in out
    assert "1 unconfirmed" in out


def test_a_failed_search_read_does_not_unconfirm_a_verdict_it_could_not_raise(tmp_path, monkeypatch, capsys):
    posts = [{"id": "BB", "posts_market": 4, "posts_all": 4, "located": 3}]
    code, [saved], _folder, _seen = run_main(tmp_path, monkeypatch, [posts, [], [], RuntimeError("x"), [], []])
    [row] = read_rows(saved)
    assert row["verdict"] == "POSTS ONLY" and row["verdict_confidence"] == "confirmed"
    assert row["read_failures"] == "search"


def test_a_failed_briefs_read_leaves_every_lower_verdict_unconfirmed(tmp_path, monkeypatch, capsys):
    posts = [{"id": "BB", "posts_market": 4, "posts_all": 4}]
    code, [saved], _folder, _seen = run_main(tmp_path, monkeypatch, [posts, [], [], [], [], RuntimeError("x")])
    [row] = read_rows(saved)
    assert row["verdict"] == "POSTS ONLY"
    assert row["verdict_confidence"] == "unconfirmed: briefs read failed"


def test_the_cutoff_and_every_window_are_recorded_beside_the_result(tmp_path, monkeypatch):
    code, [saved], _folder, seen = run_main(tmp_path, monkeypatch, [[], [], [], [], [], []],
                                            extra=["--obs-end", "2026-10-07"])
    assert code == 0
    [params_file] = _folder.glob("*-params.json")
    assert params_file.name.startswith(saved.stem)
    params = json.loads(params_file.read_text(encoding="utf-8"))
    assert params["obs_end"] == "2026-10-07" and params["obs_end_source"] == "argument"
    assert params["post_window_days"] == {"before": 2, "after": 3}
    assert params["brief_window_days"] == {"before": 0, "after": 3}
    assert params["cluster_window_days"] == {"before": 1, "after": 4}
    assert params["located"] == {"min_confidence": 0.7, "market_floor_posts": 8}
    assert params["excluded_lanes"] == ["agent_live", "placebo"] and params["excluded_lane_classes"] == ["legacy"]
    assert params["max_bytes_billed"] == bm.MAX_BYTES
    assert "headline" in params["match_rule"] and "cluster identity" in params["match_rule"]
    assert params["moments_file"].endswith("moments.csv") and params["moments_sha256"]
    assert len(params["moments_sha256"]) == 64
    bound = [p for p in seen if p is not None]
    obs_end = {q.name: getattr(q, "value", None) for q in bound[0].query_parameters}["obs_end"]
    assert str(obs_end) == "2026-10-07"


def test_without_a_cutoff_argument_the_params_say_the_cutoff_came_from_the_clock(tmp_path, monkeypatch):
    code, [saved], folder, _seen = run_main(tmp_path, monkeypatch, [[], [], [], [], [], []])
    [params_file] = folder.glob("*-params.json")
    params = json.loads(params_file.read_text(encoding="utf-8"))
    assert params["obs_end_source"] == "clock, capped at the moment window end plus 4 days"


def test_the_windows_recorded_with_a_result_are_the_ones_in_the_sql_and_the_brief_reader():
    import re
    from datetime import date, timedelta

    def intervals(sql):
        return [int(n) for n in re.findall(r"INTERVAL (\d+) DAY", sql)]

    assert intervals(bm.POSTS_SQL) == [bm.POST_BEFORE, bm.POST_AFTER]
    assert intervals(bm.CLUSTERS_SQL) == [bm.CLUSTER_BEFORE, bm.CLUSTER_AFTER]
    assert intervals(bm.SEARCH_SQL) == [bm.SEARCH_BEFORE, bm.SEARCH_AFTER]
    assert intervals(bm.GDELT_SQL) == [bm.GDELT_BEFORE, bm.GDELT_AFTER]
    assert intervals(bm.MAP_SQL) == [bm.MAP_BEFORE]
    assert f"geo_confidence >= {bm.LOCATED_MIN_CONFIDENCE}" in bm.POSTS_SQL
    assert all(f"'{lane}'" in bm.POSTS_SQL for lane in bm.EXCLUDED_LANES)
    assert all(f"'{lane_class}'" in bm.POSTS_SQL for lane_class in bm.EXCLUDED_LANE_CLASSES)
    moment = {"id": "x", "market": "NG", "d": date(2026, 10, 5), "any_re": "bbnaija", "and_re": ""}
    item = {"item_id": "i", "title": "BBNaija finale", "evidence": []}
    for offset in range(0, bm.BRIEF_AFTER + 2):
        day = (moment["d"] + timedelta(days=offset)).isoformat()
        cards, _held = bm.brief_hits(moment, {(day, "NG"): {"cards": [item]}})
        assert bool(cards) == (offset <= bm.BRIEF_AFTER)
