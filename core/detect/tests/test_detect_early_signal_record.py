"""The early signal as a recorded observation (core/detect/early_signal.py record, hooked into stats.run_stats).

The flag goes to its own table, early_signal. The historical table digest is preserved after checking the current
method tokens and mapping those two metadata fields to their historical values. Numeric roundoff is checked
against the old base before restoring its bytes. State and rank pins remain. Today retains its historical
content pin outside the explicitly emptied held claims.
"""

import json
import importlib.metadata
from datetime import timedelta
import logging
import os
from pathlib import Path

import numpy as np
import pytest
from google.cloud import bigquery
from scipy import stats as st

from core.detect import early_signal as es, stats
from core.detect.tests import duck, early_identity as ident
from core.detect.tests.fixtures import D, at, day, run
from core.detect.tests.test_detect_stats import SW_PANEL, StatsClient, panel_world

B0_TABLES = "072ea11c14e4da253514b096cc2ad060a4fa003a3d77e80d559bef5ca13c1fb8"
B0_STATE = "4ee60d59e7d91e47b07c2fc218003db39b79dcb6cc35ddb32c23834d6fada4c5"
# The wave8/api lane changes the Today payload, so the base commit's digest of it (8b5b4670...) no longer holds in a tree
# that has that lane. This is the digest of the same fixture payload at wave8/api 068ee52, which has no early signal:
# the early signal merged on top of it leaves the payload as that lane built it.
# Derived from that historical payload by removing only the three held-claim fields. Current held records must
# first prove the safe empty-array and null contract. Every other field remains in the digest.
LANE_TODAY_HELD_PROJECTION_DIGEST = "d4554385faa95cec14f615bb34ce4cc09c3479c1ba86495cca57e7b43145ee4f"

# Numeric reference bytes from base 24fc597, whose full table and state digests remain pinned.
B0_TABLE_FLOATS = {'jump10|ZA|panel_fb_hub|p1': {'p_mid': 1.3973901313024162e-09, 'q': 8.384340787814497e-09}, 'jump11|ZA|panel_fb_hub|p1': {'p_mid': 6.150964729530607e-09, 'q': 2.8997405153501433e-08}, 'jump12|ZA|panel_fb_hub|p1': {'p_mid': 2.09432841049463e-09, 'q': 1.1518806257720464e-08}, 'jump13|ZA|panel_fb_hub|p1': {'p_mid': 7.065328778435973e-10, 'q': 4.663116993767742e-09}, 'jump14|ZA|panel_fb_hub|p1': {'p_mid': 1.3623249033774266e-10, 'q': 1.0322749538269624e-09}, 'jump15|ZA|panel_fb_hub|p1': {'p_mid': 1.7550603350499793e-11, 'q': 1.9305663685549772e-10}, 'jump16|ZA|panel_fb_hub|p1': {'p_mid': 9.869975503722037e-13, 'q': 1.628545958114136e-11}, 'jump17|ZA|panel_fb_hub|p1': {'p_mid': 1.4076476643094943e-10, 'q': 1.0322749538269624e-09}, 'jump18|ZA|panel_fb_hub|p1': {'p_mid': 5.053702254186224e-11, 'q': 4.764919268232725e-10}, 'jump19|ZA|panel_fb_hub|p1': {'p_mid': 8.830325929232362e-13, 'q': 1.628545958114136e-11}, 'jump1|ZA|panel_fb_hub|p1': {'p_mid': 5.2425076916226576e-06, 'q': 1.821081619195239e-05}, 'jump20|ZA|panel_fb_hub|p1': {'p_mid': 6.391942114401967e-12, 'q': 8.437363591010596e-11}, 'jump21|ZA|panel_fb_hub|p1': {'p_mid': 4.835913761938768e-13, 'q': 1.5958515414397936e-11}, 'jump22|ZA|panel_fb_hub|p1': {'p_mid': 2.8595781446015665e-14, 'q': 1.887321575437034e-12}, 'jump2|ZA|panel_fb_hub|p1': {'p_mid': 0.0006381368670448938, 'q': 0.0017548763843734578}, 'jump3|ZA|panel_fb_hub|p1': {'p_mid': 9.122080670949967e-05, 'q': 0.00026176405403595556}, 'jump4|ZA|panel_fb_hub|p1': {'p_mid': 3.806947454222036e-05, 'q': 0.0001142084236266611}, 'jump5|ZA|panel_fb_hub|p1': {'p_mid': 3.9320972382038735e-08, 'q': 1.7301227848097044e-07}, 'jump6|ZA|panel_fb_hub|p1': {'p_mid': 1.3311616010640726e-05, 'q': 4.392833283511439e-05}, 'jump7|ZA|panel_fb_hub|p1': {'p_mid': 3.0757400257701486e-09, 'q': 1.5615295515448446e-08}, 'jump8|ZA|panel_fb_hub|p1': {'p_mid': 9.894672251325554e-07, 'q': 3.628046492152703e-06}, 'jump9|ZA|panel_fb_hub|p1': {'p_mid': 3.8444190756079214e-07, 'q': 1.5858228686882675e-06}, 'jump|ZA|panel_fb_hub|p1': {'p_mid': 4.4100464102397494e-07, 'q': 1.7121356651519026e-06}, 'ramp|ZA|panel_fb_hub|p1': {'p_mid': 3.806947454222036e-05, 'q': 0.0001142084236266611}, 's0|ZA|panel_fb_hub|p1': {'p_mid': 0.42638690991030215, 'q': 0.5978599583689985}, 's10|ZA|panel_fb_hub|p1': {'p_mid': 0.35791735883376924, 'q': 0.5717351455993201}, 's11|ZA|panel_fb_hub|p1': {'p_mid': 0.21104294729960116, 'q': 0.4096716035815787}, 's12|ZA|panel_fb_hub|p1': {'p_mid': 0.28923527245584013, 'q': 0.5159331887050121}, 's13|ZA|panel_fb_hub|p1': {'p_mid': 0.010223840055974574, 'q': 0.026990937747772874}, 's14|ZA|panel_fb_hub|p1': {'p_mid': 0.7251245662737332, 'q': 0.825141747828731}, 's15|ZA|panel_fb_hub|p1': {'p_mid': 0.35791735883376924, 'q': 0.5717351455993201}, 's16|ZA|panel_fb_hub|p1': {'p_mid': 0.43480724245018076, 'q': 0.5978599583689985}, 's17|ZA|panel_fb_hub|p1': {'p_mid': 0.42638690991030215, 'q': 0.5978599583689985}, 's18|ZA|panel_fb_hub|p1': {'p_mid': 0.6000331841300246, 'q': 0.7200398209560295}, 's19|ZA|panel_fb_hub|p1': {'p_mid': 0.22971560522446405, 'q': 0.43317799842327503}, 's1|ZA|panel_fb_hub|p1': {'p_mid': 0.5196518611464302, 'q': 0.6471136384087621}, 's20|ZA|panel_fb_hub|p1': {'p_mid': 0.5137773017402026, 'q': 0.6471136384087621}, 's21|ZA|panel_fb_hub|p1': {'p_mid': 0.467456260470729, 'q': 0.6296349630830227}, 's22|ZA|panel_fb_hub|p1': {'p_mid': 0.35791735883376924, 'q': 0.5717351455993201}, 's23|ZA|panel_fb_hub|p1': {'p_mid': 0.42638690991030215, 'q': 0.5978599583689985}, 's24|ZA|panel_fb_hub|p1': {'p_mid': 0.7885787368244159, 'q': 0.8515615050198629}, 's25|ZA|panel_fb_hub|p1': {'p_mid': 0.842907523405603, 'q': 0.8692483835120282}, 's26|ZA|panel_fb_hub|p1': {'p_mid': 0.16057069216997097, 'q': 0.331529237296443}, 's27|ZA|panel_fb_hub|p1': {'p_mid': 0.101208508800776, 'q': 0.22265871936170722}, 's28|ZA|panel_fb_hub|p1': {'p_mid': 0.8627039347293599, 'q': 0.8759763029559654}, 's29|ZA|panel_fb_hub|p1': {'p_mid': 0.27228890887814183, 'q': 0.49919633294326}, 's2|ZA|panel_fb_hub|p1': {'p_mid': 0.06408119461531044, 'q': 0.15104853016466033}, 's30|ZA|panel_fb_hub|p1': {'p_mid': 0.42638690991030215, 'q': 0.5978599583689985}, 's31|ZA|panel_fb_hub|p1': {'p_mid': 0.7563248259678597, 'q': 0.8460582798962497}, 's32|ZA|panel_fb_hub|p1': {'p_mid': 0.21104294729960116, 'q': 0.4096716035815787}, 's33|ZA|panel_fb_hub|p1': {'p_mid': 0.5137773017402026, 'q': 0.6471136384087621}, 's34|ZA|panel_fb_hub|p1': {'p_mid': 0.799951716836841, 'q': 0.8515615050198629}, 's35|ZA|panel_fb_hub|p1': {'p_mid': 0.9854587761810906, 'q': 0.9854587761810906}, 's36|ZA|panel_fb_hub|p1': {'p_mid': 0.3638314562904765, 'q': 0.5717351455993201}, 's37|ZA|panel_fb_hub|p1': {'p_mid': 0.3443870418026718, 'q': 0.5717351455993201}, 's38|ZA|panel_fb_hub|p1': {'p_mid': 0.04243399795780369, 'q': 0.10372755056352015}, 's39|ZA|panel_fb_hub|p1': {'p_mid': 0.42638690991030215, 'q': 0.5978599583689985}, 's3|ZA|panel_fb_hub|p1': {'p_mid': 0.0985407244986103, 'q': 0.22265871936170722}, 's4|ZA|panel_fb_hub|p1': {'p_mid': 0.5603225193980118, 'q': 0.6848386348197922}, 's5|ZA|panel_fb_hub|p1': {'p_mid': 0.7024192508871457, 'q': 0.825141747828731}, 's6|ZA|panel_fb_hub|p1': {'p_mid': 0.7131383361067669, 'q': 0.825141747828731}, 's7|ZA|panel_fb_hub|p1': {'p_mid': 0.5160413760117911, 'q': 0.6471136384087621}, 's8|ZA|panel_fb_hub|p1': {'p_mid': 0.7713458831956698, 'q': 0.8484804715152369}, 's9|ZA|panel_fb_hub|p1': {'p_mid': 0.842907523405603, 'q': 0.8692483835120282}, 'slow|ZA|panel_fb_hub|p1': {'p_mid': 0.1607414483861542, 'q': 0.331529237296443}, 'step|ZA|panel_fb_hub|p1': {'p_mid': 0.04243399795780369, 'q': 0.10372755056352015}}
B0_STATE_FLOATS = {'jump': {'main_ratio': 4.270551949510036, 'q_min': 1.7121356651519026e-06, 'worth_pct': 0.6666666666666666, 'worth_raw': 0.30476015743890267}, 'jump1': {'main_ratio': 3.87071644258113, 'q_min': 1.821081619195239e-05, 'worth_pct': 0.08333333333333333, 'worth_raw': 0.2767946637444368}, 'jump10': {'main_ratio': 5.312149985975899, 'q_min': 8.384340787814497e-09, 'worth_pct': 0.625, 'worth_raw': 0.3038499291458252}, 'jump11': {'main_ratio': 4.964540253798196, 'q_min': 2.8997405153501433e-08, 'worth_pct': 0.5, 'worth_raw': 0.30202529297616176}, 'jump12': {'main_ratio': 5.151881395450959, 'q_min': 1.1518806257720464e-08, 'worth_pct': 0.3333333333333333, 'worth_raw': 0.30036184381696757}, 'jump13': {'main_ratio': 5.33922253710372, 'q_min': 4.663116993767742e-09, 'worth_pct': 0.9166666666666666, 'worth_raw': 0.3104374672786014}, 'jump14': {'main_ratio': 5.620234249582864, 'q_min': 1.0322749538269624e-09, 'worth_pct': 0.375, 'worth_raw': 0.30099968622873696}, 'jump15': {'main_ratio': 6.016863747386129, 'q_min': 1.9305663685549772e-10, 'worth_pct': 0.875, 'worth_raw': 0.3097582360458373}, 'jump16': {'main_ratio': 6.562067629734934, 'q_min': 1.628545958114136e-11, 'worth_pct': 0.5833333333333334, 'worth_raw': 0.3027841597321259}, 'jump17': {'main_ratio': 5.5315426692814995, 'q_min': 1.0322749538269624e-09, 'worth_pct': 0.4166666666666667, 'worth_raw': 0.30110725819151135}, 'jump18': {'main_ratio': 5.701743982182469, 'q_min': 4.764919268232725e-10, 'worth_pct': 0.75, 'worth_raw': 0.3063615839588836}, 'jump19': {'main_ratio': 6.463269387020293, 'q_min': 1.628545958114136e-11, 'worth_pct': 1.0, 'worth_raw': 0.31466994485171806}, 'jump2': {'main_ratio': 2.72885977416476, 'q_min': 0.0017548763843734578, 'worth_pct': 0.041666666666666664, 'worth_raw': 0.2742640267786615}, 'jump20': {'main_ratio': 6.0421466079844075, 'q_min': 8.437363591010596e-11, 'worth_pct': 0.7083333333333334, 'worth_raw': 0.3052783420549039}, 'jump21': {'main_ratio': 6.510154735585001, 'q_min': 1.5958515414397936e-11, 'worth_pct': 0.8333333333333334, 'worth_raw': 0.30866590072832284}, 'jump22': {'main_ratio': 7.02529281197858, 'q_min': 1.887321575437034e-12, 'worth_pct': 0.9583333333333334, 'worth_raw': 0.314558984368977}, 'jump3': {'main_ratio': 3.1487242886679305, 'q_min': 0.00026176405403595556, 'worth_pct': 0.20833333333333334, 'worth_raw': 0.28351851185350646}, 'jump4': {'main_ratio': 3.3189256015689, 'q_min': 0.0001142084236266611, 'worth_pct': 0.125, 'worth_raw': 0.27949943865843385}, 'jump5': {'main_ratio': 4.809071943812919, 'q_min': 1.7301227848097044e-07, 'worth_pct': 0.7916666666666666, 'worth_raw': 0.30742946671446686}, 'jump6': {'main_ratio': 3.4992549256512997, 'q_min': 4.392833283511439e-05, 'worth_pct': 0.25, 'worth_raw': 0.28552601502804736}, 'jump7': {'main_ratio': 5.278249694428814, 'q_min': 1.5615295515448446e-08, 'worth_pct': 0.2916666666666667, 'worth_raw': 0.28881424536402595}, 'jump8': {'main_ratio': 3.999730853172777, 'q_min': 3.628046492152703e-06, 'worth_pct': 0.4583333333333333, 'worth_raw': 0.3014784397365642}, 'jump9': {'main_ratio': 4.169932166073746, 'q_min': 1.5858228686882675e-06, 'worth_pct': 0.5416666666666666, 'worth_raw': 0.30261592746607585}, 'ramp': {'main_ratio': 3.3189256015689, 'q_min': 0.0001142084236266611, 'worth_pct': 0.16666666666666666, 'worth_raw': 0.2815663063275017}, 's13': {'main_ratio': 2.1873558765783114, 'q_min': 0.026990937747772874, 'worth_pct': 0.0, 'worth_raw': 0.25178172323209314}}

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def built():
    con = ident.connect()
    rows = ident.build(con)
    yield con, rows
    con.close()


def early(con):
    return {r["series_id"].split("|")[0]: r for r in duck.query(con, "SELECT * FROM {core}.early_signal")}



def restore_historical_float_bytes(rows, references):
    assert rows.keys() == references.keys()
    for identity, expected in references.items():
        for key, value in expected.items():
            actual = rows[identity][key]
            assert isinstance(actual, float)
            assert actual == pytest.approx(value, rel=1e-14, abs=1e-15), (identity, key)
            rows[identity][key] = value


def write_historical_diagnostic(label, con, projected, expected):
    guard_log = os.environ.get("CORE_OFFLINE_GUARD_LOG")
    if not guard_log:
        return
    tables = {}
    for schema, table in con.execute(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_type = 'BASE TABLE' AND table_schema IN ('core', 'agent') ORDER BY 1, 2"
    ).fetchall():
        cur = con.execute(f'SELECT * FROM "{schema}"."{table}" ORDER BY ALL')
        columns = [column[0] for column in cur.description]
        tables[f"{schema}.{table}"] = [dict(zip(columns, row)) for row in cur.fetchall()]
    raw_tables = ident.dump(con)
    raw_state = tables["core.item_state"]
    raw_state_dump = ident.state_rows({row["item_id"]: row for row in raw_state})
    projected_state = projected if label == "state" else ident.state_rows({
        row["item_id"]: row for row in projected["core.item_state"]
    })
    projected_bytes = json.dumps(
        projected, sort_keys=True, separators=(",", ":") if label == "tables" else None,
        default=ident._plain,
    ).encode("utf-8")
    record = {
        "label": label,
        "raw_tables": tables,
        "raw_state": raw_state,
        "raw_state_dump": raw_state_dump,
        "raw_state_hash": ident.digest(json.dumps(
            raw_state_dump, sort_keys=True, default=ident._plain,
        ).encode("utf-8")),
        "raw_historical_table_dump": json.loads(raw_tables),
        "raw_historical_table_hash": ident.digest(raw_tables),
        "projected_dump": projected,
        "projected_hash": ident.digest(projected_bytes),
        "expected_hash": expected,
        "projected_state_dump": projected_state,
        "projected_state_hash": ident.digest(json.dumps(
            projected_state, sort_keys=True, default=ident._plain,
        ).encode("utf-8")),
        "expected_state_hash": B0_STATE,
        "versions": {name: importlib.metadata.version(name) for name in (
            "numpy", "scipy", "pandas", "duckdb", "sqlglot", "statsmodels", "pytest", "PyYAML",
        )},
    }
    directory = Path(guard_log).parent / "core-shard" / "diagnostics"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"detect-historical-{label}.json").write_text(
        json.dumps(record, sort_keys=True, indent=2, default=ident._plain), encoding="utf-8",
    )


def historical_table_digest(con):
    tables = json.loads(ident.dump(con))
    restore_historical_float_bytes(
        {row["series_id"]: row for row in tables["core.series_test"]}, B0_TABLE_FLOATS)
    assert stats.RULE_VERSION == "stats-3-distinct-item-prior-health"
    for table in ("core.series_test", "core.test_switch"):
        assert tables[table]
        for row in tables[table]:
            assert row["rule_version"] == stats.RULE_VERSION
            row["rule_version"] = "stats-1"
    digest = ident.digest(json.dumps(tables, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    if digest != B0_TABLES:
        write_historical_diagnostic("tables", con, tables, B0_TABLES)
    return digest


# Byte identity with the base commit, flag on


def test_every_table_detect_writes_except_early_signal_is_byte_identical_to_the_base_commit(built):
    con, _ = built
    assert historical_table_digest(con) == B0_TABLES


def test_state_worth_ranks_and_eligibility_are_byte_identical_to_the_base_commit(built):
    con, rows = built
    states = ident.state_rows(rows)
    restore_historical_float_bytes(states, B0_STATE_FLOATS)
    blob = json.dumps(states, sort_keys=True, default=ident._plain).encode("utf-8")
    if ident.digest(blob) != B0_STATE:
        write_historical_diagnostic("state", con, states, B0_STATE)
    assert ident.digest(blob) == B0_STATE
    assert sum(r["worth_pct"] is not None for r in rows.values()) >= 20       # the rank columns are populated


def test_the_today_payload_is_byte_identical_to_the_one_the_api_lane_built():
    from core.api import today
    from core.api.store import FixtureStore

    payload = today.build_today(FixtureStore(), "2026-09-30")
    held = [item for market in payload["markets"] for item in market["held_back"]["items"]]
    assert held
    for item in held:
        assert item["claims"] == []
        assert item["explanation"] is None
        assert item["explanation_claim_ids"] == []
        for field in ("claims", "explanation", "explanation_claim_ids"):
            item.pop(field)
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=ident._plain).encode("utf-8")
    assert ident.digest(blob) == LANE_TODAY_HELD_PROJECTION_DIGEST


def test_the_identity_run_is_not_vacuous_the_flag_fired_where_no_state_did(built):
    con, rows = built
    flags = early(con)
    tested = duck.query(con, "SELECT COUNT(*) n FROM {core}.series_test WHERE test = 'nb'")[0]["n"]
    assert tested == 66 and len(flags) == tested and all(r["rule_version"] == "early-1" for r in flags.values())
    assert flags["step"]["early"] is True and flags["ramp"]["early"] is True
    assert "step" not in rows and "slow" not in rows        # item_state has no row for either: no state, no rank
    assert rows["ramp"]["state"] == "spike"                  # the ramp is a Spike today and the flag does not change it
    assert sum(r["early"] for k, r in flags.items() if k.startswith("s") and k[1:].isdigit()) <= 2   # stable items


def test_with_the_flag_off_the_other_tables_are_the_same_bytes_as_with_it_on(built, monkeypatch):
    monkeypatch.setattr(es, "record", lambda *a, **k: 0)
    con = ident.connect()
    ident.build(con)
    assert ident.dump(con) == ident.dump(built[0])
    assert historical_table_digest(con) == B0_TABLES
    assert duck.query(con, "SELECT COUNT(*) n FROM {core}.early_signal")[0]["n"] == 0
    con.close()


# The recorded values come from the rows detect already wrote, recomputed independently


@pytest.mark.parametrize("item", ["step", "ramp", "s3", "jump"])
def test_the_recorded_cusum_matches_a_recomputation_from_the_raw_counts(built, item):
    con, _ = built
    row = duck.query(con, "SELECT * FROM {core}.series_test WHERE item_id = @i", {"i": item})[0]
    counts = duck.query(con, "SELECT metric_date d, posts FROM {core}.item_daily WHERE item_id = @i "
                             "AND metric_date BETWEEN @a AND @b ORDER BY metric_date",
                        {"i": item, "a": day(13), "b": D})
    assert len(counts) == 14
    n, s = 1 / row["alpha"], 0.0
    for c in counts:
        s = max(0.0, s + st.nbinom.logpmf(c["posts"], n, n / (n + 1.5 * row["mu"]))
                - st.nbinom.logpmf(c["posts"], n, n / (n + row["mu"])))
    got = early(con)[item]
    assert got["cusum"] == pytest.approx(s, abs=1e-9)
    assert got["early"] == (s >= 6.5) and got["y"] == row["y"] and got["mu"] == row["mu"]


def test_the_threshold_and_shape_recorded_are_the_ones_pinned_here(built):
    con, _ = built
    r = next(iter(early(con).values()))
    assert (r["kappa"], r["h"], r["window_days"]) == (1.5, 6.5, 14)
    assert list(es.COLUMNS) == [
        "metric_date", "series_id", "item_id", "market", "platform", "series", "protocol", "lane_class", "kind",
        "y", "mu", "alpha", "cusum", "early", "run_days", "days_used", "kappa", "h", "window_days",
        "baseline_mode", "baseline_source", "baseline_date", "run_id", "rule_version"]


# Nothing reads the table


def test_no_gate_state_card_payload_or_rank_reads_the_table_or_the_module():
    allowed = {"core/detect/early_signal.py", "core/detect/early_signal_backtest.py", "core/detect/stats.py",
               "core/detect/tests/early_identity.py", "core/detect/tests/test_detect_early_signal.py",
               "core/detect/tests/test_detect_early_signal_backtest.py",
               "core/detect/tests/test_detect_early_signal_record.py",
               "core/schema/early_signal.sql", "core/schema/tests/test_early_signal_schema.py",
               # Release B's source checks and durable manifest name the schema file and its effect, and read no table
               "core/setup/release/jobs_effects.py", "core/setup/release/jobs_source.py",
               "core/setup/tests/test_jobs_effects.py", "core/setup/tests/test_jobs_source.py",
               # and the jobs rollback names the table in its residue list and the packet lock names the schema file; neither reads a table
               "core/setup/release/jobs_only.py", "core/setup/release/lock.py", "core/setup/tests/test_jobs_readback.py"}
    named_apart = "core/schema/apply.py"
    hits = []
    for path in ROOT.rglob("*"):
        parts = path.relative_to(ROOT).parts
        if (not path.is_file() or ".git" in parts or "node_modules" in parts or path.suffix not in
                {".py", ".sql", ".js", ".jsx", ".ts", ".tsx", ".yaml", ".yml", ".json", ".html"}):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        rel = path.relative_to(ROOT).as_posix()
        if ("early_signal" in text or "cusum" in text) and rel not in allowed and rel != named_apart:
            hits.append(rel)
    assert hits == []


def test_the_setup_runner_names_the_table_only_in_its_file_list_and_its_docstring():
    """core/schema/apply.py runs DDL for every file it lists and reads no table, so it may name early_signal.sql in
    SQL_FILES and in the module docstring and nowhere else: a reader added to it would be a reader of the table."""
    src = (ROOT / "core/schema/apply.py").read_text(encoding="utf-8")
    _, docstring, code = src.split('"""', 2)
    assert "early_signal" in docstring
    assert code.lower().count("early_signal") == 1 and 'HERE / "early_signal.sql"' in code
    assert "cusum" not in src.lower()
    in_list = [line for line in code.splitlines() if "early_signal" in line]
    assert len(in_list) == 1 and in_list[0].startswith("SQL_FILES = (")


def test_stats_only_calls_record_after_the_series_test_rows_are_loaded():
    src = (ROOT / "core/detect/stats.py").read_text(encoding="utf-8")
    body = src.split("def run_stats", 1)[1]
    assert body.index("load_table_from_json") < body.index("early_signal.record")
    assert src.count("early_signal") == 2                  # the import and the call, nothing else


# Recording never stops the series test


@pytest.fixture
def con():
    c = duck.connect()
    c.execute("CREATE TABLE core.test_switch (market VARCHAR NOT NULL, platform VARCHAR NOT NULL, "
              "lane_class VARCHAR NOT NULL, switched_on DATE, backtest_run_id VARCHAR, rule_version VARCHAR)")
    c.execute(ident.early_signal_ddl())
    panel_world().load(c)
    duck.load(c, "core.test_switch", [SW_PANEL])
    duck.load(c, "agent.runs", [{**run("backtest", day(3), run_id=SW_PANEL["backtest_run_id"]),
                                 "finished_at": at(day(3))}])
    yield c
    c.close()


def test_run_stats_records_one_row_per_tested_series_and_returns_the_same_count(con):
    assert stats.run_stats(StatsClient(con), D, "stats-x", "r1", core="core", agent="agent") == 3
    got = duck.query(con, "SELECT * FROM {core}.early_signal ORDER BY item_id")
    assert [r["item_id"] for r in got] == ["p0", "p1", "p2"]
    assert {r["run_id"] for r in got} == {"stats-x"} and {r["metric_date"] for r in got} == {D}


def test_a_failing_record_logs_and_leaves_the_series_test_untouched(con, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("load failed")

    monkeypatch.setattr(es, "record", boom)
    with caplog.at_level(logging.ERROR, logger="core.detect.stats"):
        assert stats.run_stats(StatsClient(con), D, "stats-x", "r1", core="core", agent="agent") == 3
    assert len(duck.query(con, "SELECT * FROM {core}.series_test")) == 3
    assert "early signal" in caplog.text and "load failed" in caplog.text


def rows_of_the_day():
    """(tvf_series_signal rows, series_test rows, weekday totals, day) for the three-series panel world."""
    c = duck.connect()
    panel_world().load(c)
    signal = duck.query(c, stats.SIGNAL_SQL, {"d": D})
    totals = duck.query(c, stats.TOTALS_SQL, {"d": D})
    weeks = duck.query(c, stats.FIRST_WEEK_SQL, {"d": D})
    rows = stats.series_test_rows(signal, D, "x", "r1", [SW_PANEL], totals, weeks)
    c.close()
    return signal, rows, totals, D


class Capture:
    """A client that records loads and answers the earlier series_test read with the rows it is given."""
    project = "p"

    def __init__(self, earlier=None):
        self.loads, self.earlier = [], earlier

    def query(self, sql, job_config=None):
        if self.earlier is None:
            raise AssertionError("refit mode must not query")
        rows = self.earlier

        class Job:
            def result(self):
                return rows

        return Job()

    def get_table(self, ref):
        return bigquery.Table("p." + ref, schema=[])

    def load_table_from_json(self, rows, table, job_config=None):
        self.loads.append(rows)
        return duck._Job([])


def test_the_default_mode_is_the_refit_baseline_and_reads_nothing_extra():
    assert es.DEFAULT_MODE == "refit"
    signal, rows, totals, d = rows_of_the_day()
    c = Capture()
    assert es.record(c, d, "r", signal, rows, totals, "core") == 3
    assert {r["baseline_mode"] for r in c.loads[0]} == {"refit"} and {r["h"] for r in c.loads[0]} == {6.5}
    assert {r["baseline_source"] for r in c.loads[0]} == {"refit"} and {r["baseline_date"] for r in c.loads[0]} == {None}


def test_frozen_mode_scores_against_the_earlier_baseline_and_says_which_source_each_row_used():
    signal, rows, totals, d = rows_of_the_day()
    sids = [r["series_id"] for r in rows]
    earlier = [{"series_id": sids[0], "metric_date": d - timedelta(days=14), "mu": 1.0, "alpha": 0.1,
                "weekday_factor": 1.0},
               {"series_id": sids[1], "metric_date": d - timedelta(days=3), "mu": 2.0, "alpha": 0.1,
                "weekday_factor": 1.0}]
    c = Capture(earlier)
    assert es.record(c, d, "r", signal, rows, totals, "core", mode="frozen") == 3
    got = {r["series_id"]: r for r in c.loads[0]}
    assert (got[sids[0]]["baseline_source"], got[sids[0]]["baseline_date"], got[sids[0]]["mu"]) == (
        "frozen", (d - timedelta(days=14)).isoformat(), 1.0)
    assert (got[sids[1]]["baseline_source"], got[sids[1]]["mu"]) == ("earliest_in_window", 2.0)
    assert (got[sids[2]]["baseline_source"], got[sids[2]]["baseline_date"]) == ("refit", None)
    assert {r["baseline_mode"] for r in c.loads[0]} == {"frozen"}
    assert got[sids[0]]["h"] == 9.0 and got[sids[1]]["h"] == 9.0 and got[sids[2]]["h"] == 6.5   # refit fallback keeps its own limit
    assert got[sids[0]]["early"] is True                     # mu 1 against counts of 3 to 6 a day


def test_an_unknown_mode_is_refused():
    signal, rows, totals, d = rows_of_the_day()
    with pytest.raises(ValueError):
        es.record(Capture(), d, "r", signal, rows, totals, "core", mode="stale")


def test_no_tested_series_means_no_early_rows_and_no_second_load():
    from core.detect.tests.test_detect_states import SIGNAL, FakeClient

    client = FakeClient([SIGNAL])
    assert stats.run_stats(client, D, "st-1", "r9") == 1
    assert len(client.loads) == 1


@pytest.mark.parametrize("field", ["worth_raw", "worth_pct", "main_ratio", "q_min"])
def test_historical_numeric_projection_refuses_a_material_change(field):
    states = {item: dict(values) for item, values in B0_STATE_FLOATS.items()}
    item = next(item for item in states if field in states[item])
    states[item][field] += 1e-6
    with pytest.raises(AssertionError):
        restore_historical_float_bytes(states, B0_STATE_FLOATS)


def test_historical_numeric_projection_accepts_only_roundoff():
    states = {item: dict(values) for item, values in B0_STATE_FLOATS.items()}
    item = next(item for item in states if "worth_raw" in states[item])
    states[item]["worth_raw"] += 1e-16
    restore_historical_float_bytes(states, B0_STATE_FLOATS)
    assert states == B0_STATE_FLOATS


@pytest.mark.parametrize("field", ["state", "eligible", "untested"])
def test_historical_projection_keeps_nonnumeric_state_bytes_pinned(built, field):
    states = ident.state_rows(built[1])
    restore_historical_float_bytes(states, B0_STATE_FLOATS)
    identity = next(iter(states))
    states[identity][field] = "mutated"
    blob = json.dumps(states, sort_keys=True, default=ident._plain).encode("utf-8")
    assert ident.digest(blob) != B0_STATE


def test_historical_projection_refuses_a_changed_item_identity():
    states = {item: dict(values) for item, values in B0_STATE_FLOATS.items()}
    states["mutated"] = states.pop(next(iter(states)))
    with pytest.raises(AssertionError):
        restore_historical_float_bytes(states, B0_STATE_FLOATS)


def test_historical_table_failure_writes_complete_diagnostics_and_still_fails(built, monkeypatch, tmp_path):
    monkeypatch.setenv("CORE_OFFLINE_GUARD_LOG", str(tmp_path / "offline-guard.jsonl"))
    con, _ = built
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute("UPDATE core.item_state SET state = 'diagnostic-mutation' WHERE item_id = 'jump'")
        with pytest.raises(AssertionError):
            test_every_table_detect_writes_except_early_signal_is_byte_identical_to_the_base_commit(built)
        artifact = tmp_path / "core-shard" / "diagnostics" / "detect-historical-tables.json"
        record = json.loads(artifact.read_text(encoding="utf-8"))
        raw_tables = record["raw_tables"]
        for table, rows in raw_tables.items():
            assert len(rows) == con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert any(row["state"] == "diagnostic-mutation" for row in record["raw_state"])
        assert record["projected_hash"] != record["expected_hash"] == B0_TABLES
        assert record["projected_state_hash"] != record["expected_state_hash"] == B0_STATE
        assert record["versions"]["numpy"] == np.__version__
        assert record["versions"]["duckdb"] == importlib.metadata.version("duckdb")
    finally:
        con.execute("ROLLBACK")


def test_healthy_historical_checks_write_no_diagnostic(built, monkeypatch, tmp_path):
    monkeypatch.setenv("CORE_OFFLINE_GUARD_LOG", str(tmp_path / "offline-guard.jsonl"))
    test_every_table_detect_writes_except_early_signal_is_byte_identical_to_the_base_commit(built)
    test_state_worth_ranks_and_eligibility_are_byte_identical_to_the_base_commit(built)
    assert not (tmp_path / "core-shard" / "diagnostics").exists()
