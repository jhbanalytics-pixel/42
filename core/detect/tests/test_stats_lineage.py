import hashlib
import json
from datetime import timedelta

import pytest

from core.detect import backtest, stats
from core.detect.tests import duck
from core.detect.tests.fixtures import D, at, day, run
from core.detect.tests.test_detect_series_weekday import database, desk_world, live_rows, switched
from core.detect.tests.test_detect_stats import StatsClient


@pytest.mark.parametrize('version', ['stats-1', 'stats-2-series', None, 'unknown-rule'])
def test_old_or_missing_calibration_cannot_enable_changed_math(version):
    con = database(desk_world())
    try:
        [row] = live_rows(con, [switched(version)])
        assert row['test'] == 'none'
        assert row['rule_version'] == 'daily-v1'
    finally:
        con.close()


@pytest.mark.parametrize('version,factor', [
    (stats.RULE_VERSION, 1.0), (stats.SERIES_RULE_VERSION, 17 / 9)])
def test_current_accepted_calibration_keeps_its_weekday_model(version, factor):
    con = database(desk_world())
    try:
        [row] = live_rows(con, [switched(version)])
        assert row['test'] == 'nb'
        assert row['weekday_factor'] == pytest.approx(factor)
        assert row['rule_version'] == version
    finally:
        con.close()


def test_mathematical_epochs_name_both_population_repairs():
    assert stats.RULE_VERSION == 'stats-3-distinct-item-prior-health'
    assert stats.SERIES_RULE_VERSION == 'stats-4-series-distinct-item-prior-health'


@pytest.mark.parametrize('version', ['stats-1', 'stats-2-series'])
def test_old_backtest_receipt_is_refused_before_counts_are_read(version):
    with pytest.raises(ValueError, match='rule version'):
        backtest.switch_rows({'rule_version': version})


@pytest.mark.parametrize('version', [stats.RULE_VERSION, stats.SERIES_RULE_VERSION])
@pytest.mark.parametrize('status,future', [('failed', False), ('replayed', False), ('ok', True)])
def test_unaccepted_or_future_current_switch_remains_off(version, status, future):
    con = database(desk_world())
    try:
        duck.load(con, 'core.test_switch', [switched(version, D + timedelta(days=1) if future else day(56))])
        duck.load(con, 'agent.runs', [{**run('backtest', day(57), run_id='accepted-fixture'),
                                     'finished_at': at(day(57)), 'status': status}])
        stats.run_stats(StatsClient(con), D, 'fixture', 'daily-v1', core='core', agent='agent')
        [row] = duck.query(con, 'SELECT * FROM {core}.series_test')
        assert row['test'] == 'none' and row['rule_version'] == 'daily-v1'
    finally:
        con.close()


@pytest.mark.parametrize('version', ['stats-1', 'stats-2-series', stats.SERIES_RULE_VERSION])
def test_saved_stale_or_unapproved_series_receipt_cannot_be_applied(tmp_path, monkeypatch, version):
    body = {'rule_version': version, 'as_of': str(D), 'thresholds': backtest.THRESHOLDS,
            'run_id': f'backtest-{D:%Y%m%d}-123456789abc'}
    raw = json.dumps(body).encode('utf-8')
    saved = tmp_path / (body['run_id'] + '.json')
    saved.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()

    def no_client(*args, **kwargs):
        raise AssertionError('a refused mathematical epoch must not construct a client')

    monkeypatch.setattr(backtest.bigquery, 'Client', no_client)
    with pytest.raises(ValueError, match='rule version'):
        backtest.reviewed(saved, D, digest, D)
    assert backtest.apply_file(saved, D, digest, today=D) == 1


def test_saved_current_primary_receipt_passes_local_admission(tmp_path):
    body = {'rule_version': stats.RULE_VERSION, 'as_of': str(D), 'thresholds': backtest.THRESHOLDS,
            'run_id': f'backtest-{D:%Y%m%d}-123456789abc'}
    raw = json.dumps(body).encode('utf-8')
    saved = tmp_path / (body['run_id'] + '.json')
    saved.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    assert backtest.reviewed(saved, D, digest, D) == (body, digest)
