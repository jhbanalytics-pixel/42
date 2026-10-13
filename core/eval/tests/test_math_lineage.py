from datetime import date

import pytest

from core.detect import forecasts, job, scorecard
from core.eval import forecast_score, quality_score

RESOLUTION = 'resolution-2-external-state-validity'
DETECTION = '+math-20261013-freshness-distinct-prior-health'


def row(epoch):
    return {'forecast_id': 'f', 'item_id': 'i', 'market': 'ZA', 'rule': 'ask_v1',
            'target': 'reach_rising', 'horizon': 7, 'issue_date': date(2026, 9, 1),
            'prob': 0.9, 'predicted_arrival': True, 'persistence_arrival': False,
            'observed_arrival': True, 'resolution_rule_version': epoch}


@pytest.mark.parametrize('epoch', [None, 'older-validity-rule'])
def test_old_resolved_outcome_cannot_score_or_promote(epoch):
    source = row(epoch)
    engine = forecasts.score([source], minimum_comparable_sample=1)['cohorts']['reach_rising/7']
    evaluated = forecast_score.cohort([source], query_id='fixture', minimum=1)
    pooled = forecast_score.pooled_skill([source], query_id='fixture', result_hash='fixture', row_count=1, minimum=1)
    assert engine['resolved'] == evaluated['n'] == pooled['n'] == 0
    assert not engine['promotion_eligible'] and not evaluated['promotion_eligible']
    assert pooled['value'] is None
    assert source['observed_arrival'] is True


def test_current_resolution_remains_scorable():
    source = row(RESOLUTION)
    assert forecasts.score([source], minimum_comparable_sample=1)['cohorts']['reach_rising/7']['resolved'] == 1
    assert forecast_score.cohort([source], query_id='fixture', minimum=1)['n'] == 1


@pytest.mark.parametrize('before,after', [('old-detect', 'new-detect'), (None, None),
                                         ('new-detect', 'mixed')])
def test_equal_locality_cannot_hide_detection_math_change(before, after):
    rows = [{'this_week': True, 'locality_basis': 'v1', 'days': 7, 'detection_rule_version': after},
            {'this_week': False, 'locality_basis': 'v1', 'days': 7, 'detection_rule_version': before}]
    assert scorecard.locality_regime(rows)['comparable_with_previous_week'] is False


def test_same_known_detection_math_can_compare():
    rows = [{'this_week': this, 'locality_basis': 'v1', 'days': 7,
             'detection_rule_version': 'new-detect'} for this in (True, False)]
    assert scorecard.locality_regime(rows)['comparable_with_previous_week'] is True


@pytest.mark.parametrize('old,current', [(None, 'new'), ('old', 'new')])
def test_weekly_delta_rejects_missing_or_changed_method(old, current):
    fingerprint = 'sha256:' + 'a' * 64
    previous = {'score': 50, 'counted': 'forecast_skill', 'questions': 10,
                'question_set_hash': fingerprint, 'method_fingerprint': old}
    replay = {'questions': 10, 'question_set_hash': fingerprint, 'method_fingerprint': current}
    delta, reason = quality_score._change(60, ['forecast_skill'], replay, previous)
    assert delta is None and 'method' in reason


def test_state_and_model_identify_changed_population():
    assert job.rule_version_for('v1').endswith(DETECTION)
    assert job.rule_version_for('v2').endswith(DETECTION)
    assert forecasts.RULE == 'logistic_v2-detect-math-20261013'


def test_current_view_masks_legacy_outcome_without_rewriting_history():
    from core.detect.tests import duck
    from core.detect.tests.test_detect_forecasts import Client

    con = duck.connect()
    try:
        legacy, current = row(None), {**row(RESOLUTION), 'forecast_id': 'current'}
        duck.load(con, 'agent.forecasts', [legacy, current])
        forecasts.apply_current_view(Client(con), core='core', agent='agent')
        actual = {r['forecast_id']: r for r in duck.query(con, 'SELECT * FROM {core}.v_forecasts_current')}
        assert actual['f']['observed_arrival'] is None
        assert actual['current']['observed_arrival'] is True
        [historical] = duck.query(con, "SELECT * FROM {agent}.forecasts WHERE forecast_id = 'f'")
        assert historical['observed_arrival'] is True
    finally:
        con.close()


def test_eval_sql_masks_legacy_and_keeps_current_resolution(warehouse):
    wh = warehouse()
    raw = [{**row(epoch), 'forecast_id': name, 'resolve_date': date(2026, 9, 8)}
           for name, epoch in [('legacy', None), ('old', 'older-validity-rule'), ('current', RESOLUTION)]]
    wh.insert('forecasts', raw)
    read, _, _ = forecast_score.closed_rows(wh.execute, date(2026, 10, 1))
    actual = {r['forecast_id']: r['observed_arrival'] for r in read}
    assert actual == {'legacy': None, 'old': None, 'current': True}


def test_nullable_resolution_epoch_migration_is_declared():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / 'schema' / 'agent.sql').read_text(encoding='utf-8')
    assert 'ADD COLUMN IF NOT EXISTS resolution_rule_version STRING' in text


def test_weekly_method_round_trip_withholds_changed_detection_epoch(warehouse, tmp_path):
    from datetime import timedelta
    from core.eval.tests.test_quality_score import NOW, WEEK, by_market, scorecard, score_file

    wh = warehouse()
    score_file(tmp_path, '2026-09-18', failed=['A'])
    score_file(tmp_path, '2026-09-25', failed=['A'])
    before = scorecard('ZA', 0.7, 30, week=WEEK - timedelta(days=7), comparable=True)
    after = scorecard('ZA', 0.8, 30, comparable=True)
    before['precision']['regime']['detection_rule_version'] = 'previous-math'
    before['precision']['regime']['previous_detection_rule_version'] = 'previous-math'
    wh.insert('engine_scorecard', [before])
    first = by_market(quality_score.run(wh.execute, WEEK - timedelta(days=7),
        scores=tmp_path, dry_run=False, now=NOW))['ZA']
    assert first['context']['method_fingerprint'].startswith('sha256:')
    wh.insert('engine_scorecard', [after])
    second = by_market(quality_score.run(wh.execute, WEEK, scores=tmp_path, dry_run=True, now=NOW))['ZA']
    assert second['score'] is not None and second['change'] is None
    assert 'method' in second['change_reason']


def test_resolver_never_appends_a_replacement_for_legacy_resolved_history():
    from core.detect.tests import duck
    from core.detect.tests.test_detect_forecasts import reach_world, resolve, rows

    con = duck.connect()
    try:
        _, ids = reach_world(con)
        [issued] = rows(con, 'f.forecast_id = @f', {'f': ids['up']})
        legacy = {**issued, 'observed_arrival': True, 'resolution_rule_version': None}
        duck.load(con, 'agent.forecasts', [legacy])
        resolve(con)
        history = rows(con, 'f.forecast_id = @f', {'f': ids['up']})
        assert len(history) == 2
        assert [r['observed_arrival'] for r in history] == [None, True]
        [current] = duck.query(con, 'SELECT * FROM {core}.v_forecasts_current WHERE forecast_id = @f',
                               {'f': ids['up']})
        assert current['observed_arrival'] is None
    finally:
        con.close()
