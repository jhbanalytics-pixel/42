import pytest

from core.detect import job, scorecard
from core.eval import quality_score

DETECTION = '+math-20261013-freshness-distinct-prior-health'


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


def test_state_identifies_changed_population():
    assert job.rule_version_for('v1').endswith(DETECTION)
    assert job.rule_version_for('v2').endswith(DETECTION)


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
