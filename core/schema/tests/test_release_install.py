"""Closed warehouse prerequisite installation, without native requests."""
import copy
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import NotFound

from core.schema import release_install as ri
from core.setup import durable_effects_check as de
from core.setup.release import jobs_effects, jobs_only, jobs_run, lock, plan

ROOT = Path(__file__).resolve().parents[3]
NOW = dt.datetime(2026, 10, 13, 12, tzinfo=dt.timezone.utc)


@pytest.fixture(scope='module')
def inputs():
    return [(p.relative_to(ROOT).as_posix(), p.read_text(encoding='utf-8'))
            for p in (ROOT / 'core').rglob('*') if p.is_file() and p.suffix in ('.py', '.sql')]


@pytest.fixture
def packet(inputs):
    old = de.git_tree_files(jobs_effects.A80, cwd=ROOT)
    manifest = jobs_effects.build_jobs_manifest(inputs, old, release_id='rel-1111111-01',
                                               commit='1' * 40, tree='2' * 40,
                                               owner={'lane': 3, 'person': 'Albert Meintjes'})
    manifest['reviewed_by'] = 'Synthetic reviewer'
    for effect in manifest['effects']:
        for compat in effect['compat'].values():
            compat['verdict'] = 'pass'
    manifest['manifest_sha256'] = de.manifest_hash(manifest)
    return manifest, ri.statements(inputs)


class Client:
    def __init__(self, *, fail=None, corrupt=False):
        self.objects = {}
        self.calls = []
        self.fail = fail
        self.corrupt = corrupt

    def get_table(self, target):
        self.calls.append(('metadata', target))
        api = self.objects.get(target)
        if api is None:
            raise NotFound('synthetic absent object')
        return SimpleNamespace(to_api_repr=lambda: copy.deepcopy(api))

    get_routine = get_table

    def query(self, sql, *, location, job_config=None):
        dry = bool(job_config and job_config.dry_run)
        assert location == 'US'
        self.calls.append(('dry' if dry else 'apply', sql))
        if not dry:
            definition = ri.expected_definition(sql)
            target = ri.PROJECT + '.' + definition['target']
            if definition['kind'] == 'view':
                api = {'type': 'VIEW', 'view': {'query': definition['body']}, 'etag': 'synthetic-etag'}
                dataset, name = definition['target'].split('.')
                api['tableReference'] = {'projectId': ri.PROJECT, 'datasetId': dataset, 'tableId': name}
            else:
                api = {'routineType': definition['routineType'], 'language': 'SQL',
                       'arguments': definition['arguments'], 'definitionBody': definition['body'], 'etag': 'synthetic-etag'}
                dataset, name = definition['target'].split('.')
                api['routineReference'] = {'projectId': ri.PROJECT, 'datasetId': dataset, 'routineId': name}
            if self.corrupt:
                if definition['kind'] == 'view':
                    api['view']['query'] = 'SELECT 0'
                else:
                    api['arguments'] = []
            self.objects[target] = api
            if definition['target'] == self.fail:
                raise RuntimeError('synthetic failure after native mutation')
        return SimpleNamespace(result=lambda: [])


def test_fixed_alpha_targets_render_in_exact_dependency_order_and_lock_all_inputs(inputs, packet):
    manifest, specs = packet
    ri.validate_effects(manifest, specs)
    assert len(specs) == len(ri.TARGETS) == 17
    assert not any('forecast' in s['target'] for s in specs)
    assert tuple(s['target'] for s in specs) == ri.TARGETS
    assert all(s['statement'].startswith('CREATE OR REPLACE') for s in specs)
    assert set(ri.RENDER_INPUTS) <= set(lock.REPO_FILES)
    assert 'core/schema/release_install.py' in lock.REPO_FILES
    assert specs[-1]['definition']['arguments'] == specs[-2]['definition']['arguments'][:5]
    assert de.validate_manifest(manifest) == []


def test_scoped_alpha_has_no_forecast_migration_effect_or_installer_dependency(inputs, packet):
    manifest, specs = packet
    assert not any('forecast' in effect['target'] or effect['effect_id'].startswith('E-FORECAST') for effect in manifest['effects'])
    assert not any('forecast' in path for path in ri.RENDER_INPUTS)
    assert 'core/detect/forecast_rules.py' not in lock.REPO_FILES
    assert 'core/setup/tests/test_jobs_forecast_effects.py' not in lock.TEST_FILES
    assert not any('resolution_rule_version' in s['statement'] for s in specs)
    with pytest.raises(ValueError):
        jobs_effects.classify('ALTER TABLE `ogilvy-trends-v2.intelligence_42_agent.forecasts` ADD COLUMN IF NOT EXISTS resolution_rule_version STRING')


@pytest.mark.parametrize('field,value', [('target', 'foreign.dataset.object'), ('statement_ref', 'SELECT 1'),
    ('statement_sha256', '0' * 64), ('effect_id', 'E-ARBITRARY'), ('apply_order', 999),
    ('native_readback', {'kind': 'warehouse_definition', 'target': 'view:foreign.object', 'expected_result_sha256': '0' * 64})])
def test_manifest_mutations_are_refused_before_client_creation(inputs, packet, monkeypatch, tmp_path, field, value):
    manifest, specs = packet
    effect = next(e for e in manifest['effects'] if e['apply_kind'] == ri.APPLY_KIND)
    effect[field] = value
    manifest['manifest_sha256'] = de.manifest_hash(manifest)
    monkeypatch.setattr(jobs_only, 'load_durable', lambda _: manifest)
    bound = {'target': '1' * 40, 'tree': '2' * 40, 'release_id': 'rel-1111111-01'}
    calls = []
    with pytest.raises(jobs_run.Stop):
        jobs_run.run_warehouse_install(bound, tmp_path, source_files=inputs,
            client_factory=lambda: calls.append('client'), quiet=lambda: calls.append('quiet'), now=lambda: NOW)
    assert calls == []


def test_unregistered_source_target_and_missing_effect_are_not_silently_dropped(inputs, packet):
    changed = [(p, text.replace('v_post_items_by_state_date', 'v_unregistered') if p.endswith('sql/views.sql') else text)
               for p, text in inputs]
    with pytest.raises(ValueError, match='fixed Alpha'):
        ri.statements(changed)
    manifest, specs = packet
    manifest['effects'] = [e for e in manifest['effects'] if e['effect_id'] != specs[0]['effect_id']]
    with pytest.raises(ValueError, match='one for one'):
        ri.validate_effects(manifest, specs)


def test_closed_install_rechecks_quiet_before_each_write_and_retains_metadata(packet, tmp_path):
    manifest, specs = packet
    client = Client()
    quiet_calls = []
    ri.install(client, specs, manifest, tmp_path, release_id='rel-1111111-01',
               quiet=lambda: quiet_calls.append(len(client.calls)), now=lambda: NOW)
    applied = [sql for kind, sql in client.calls if kind == 'apply']
    assert applied == [s['statement'] for s in specs]
    assert len(quiet_calls) == 1 + 2 * len(specs)
    receipts = [json.loads(p.read_text()) for p in sorted(tmp_path.glob('warehouse-install-*.json'))]
    assert [r['state'] for r in receipts] == ['before', 'attempted', 'after', 'completed'] * len(specs)
    assert all('api' in r for r in receipts if r['state'] in ('before', 'after'))
    assert all(r['result_sha256'] == s['readback_hash'] for r, s in zip(receipts[3::4], specs))


def test_partial_native_failure_stops_and_retry_reads_state_without_replaying_completed_writes(packet, tmp_path):
    manifest, specs = packet
    client = Client(fail=specs[2]['target'])
    with pytest.raises(RuntimeError):
        ri.install(client, specs, manifest, tmp_path, release_id='rel-1111111-01', quiet=lambda: None, now=lambda: NOW)
    before = {p.name: p.read_bytes() for p in tmp_path.glob('*.json')}
    assert len([c for c in client.calls if c[0] == 'apply']) == 3
    client.fail = None
    ri.install(client, specs, manifest, tmp_path, release_id='rel-1111111-01', quiet=lambda: None, now=lambda: NOW)
    assert len([c for c in client.calls if c[0] == 'apply']) == len(specs)
    assert all((tmp_path / name).read_bytes() == data for name, data in before.items())


def test_wrong_signature_after_apply_blocks_completion_and_records_failure(packet, tmp_path):
    manifest, specs = packet
    client = Client(corrupt=True)
    with pytest.raises(ValueError, match='signature'):
        ri.install(client, specs, manifest, tmp_path, release_id='rel-1111111-01', quiet=lambda: None, now=lambda: NOW)
    receipts = [json.loads(p.read_text()) for p in sorted(tmp_path.glob('*.json'))]
    assert receipts[-1]['state'] == 'failed'
    assert not any(r['state'] == 'completed' for r in receipts)


def test_quiet_refusal_prevents_ddl(packet, tmp_path):
    manifest, specs = packet
    client = Client()
    def refused():
        raise jobs_run.Stop('CHAIN_ACTIVE', 'synthetic active execution')
    with pytest.raises(jobs_run.Stop):
        ri.install(client, specs, manifest, tmp_path, release_id='rel-1111111-01', quiet=refused, now=lambda: NOW)
    assert client.calls == []


def test_new_readbacks_are_required_by_schema_receipt_before_jobs_update(packet):
    manifest, specs = packet
    bound = {'release_id': 'rel-1111111-01', 'durableManifestSha256': 'a' * 64}
    receipt = jobs_only.schema_receipt_body(bound, manifest)
    hashes = {e['effect_id']: e['result_sha256'] for e in receipt['effects']}
    assert all(hashes[s['effect_id']] == s['readback_hash'] for s in specs)
    class BQ:
        def definition(self, target):
            return next(s['definition'] for s in specs if target == s['definition']['kind'] + ':' + s['target'])
    only = copy.deepcopy(manifest)
    only['effects'] = [e for e in only['effects'] if e['apply_kind'] == ri.APPLY_KIND]
    only['manifest_sha256'] = de.manifest_hash(only)
    assert de.run_readbacks(only, BQ())['ok'] is True


def test_warehouse_action_requires_console_and_fresh_action_bound_token_before_transport(tmp_path):
    calls = []
    assert jobs_run.main(['warehouse-install', '--bindings', str(tmp_path/'missing'), '--evidence', str(tmp_path)],
                         console=lambda: False, warehouse_factory=lambda: calls.append('client')) == 1
    assert calls == []
    bound = {'release_id': 'rel-1111111-01', 'target': '1'*40, 'tree': '2'*40, 'durableManifestSha256': 'a'*64}
    with pytest.raises(jobs_run.Stop) as error:
        jobs_run.consume_warehouse_token(bound, tmp_path, lambda: NOW.timestamp())
    assert error.value.code == 'WAREHOUSE_TOKEN'
    body = {'schema_version': 1, 'action': 'warehouse-install', 'release_id': bound['release_id'],
            'target': bound['target'], 'tree': bound['tree'], 'durable_manifest_sha256': bound['durableManifestSha256'],
            'token': '1'*32}
    p = tmp_path/jobs_run.WAREHOUSE_TOKEN_NAME
    p.write_text(json.dumps(body))
    stamp = p.stat().st_mtime
    with pytest.raises(jobs_run.Stop):
        jobs_run.consume_warehouse_token(bound, tmp_path, lambda: stamp+61)
    jobs_run.consume_warehouse_token(bound, tmp_path, lambda: stamp)
    assert not p.exists()
    assert 'warehouse-install' in plan.JOBS_RUN_ACTIONS


def test_metadata_normalization_preserves_literals_and_ignores_only_sql_formatting():
    assert ri.normalized_sql("(SELECT x+1, 'two  spaces--kept' AS label -- comment\n FROM t)") == ri.normalized_sql(
        "SELECT x + 1,'two  spaces--kept' AS label FROM t")
    assert ri.normalized_sql("SELECT 'two  spaces'") != ri.normalized_sql("SELECT 'two spaces'")


def test_schema_wrapper_gates_each_actual_source_ddl_and_refuses_free_sql(monkeypatch, tmp_path):
    from core.schema import apply
    from core.setup.release import jobs_source

    sql = 'ALTER TABLE `ogilvy-trends-v2.intelligence_42_core.post_items` ADD COLUMN IF NOT EXISTS link_market STRING'
    monkeypatch.setattr(jobs_source, 'schema_statements', lambda _: [sql])
    calls = []
    class SchemaClient:
        def query(self, statement, **kwargs):
            calls.append(('query', statement, bool(kwargs.get('job_config') and kwargs['job_config'].dry_run)))
            return SimpleNamespace(result=lambda: [])
    def apply_run(client, statements, apply):
        from google.cloud import bigquery
        assert apply is True and statements == [sql]
        client.query(sql, location='US', job_config=bigquery.QueryJobConfig(dry_run=True)).result()
        client.query(sql, location='US').result()
        with pytest.raises(ValueError, match='unregistered'):
            client.query('DELETE FROM intelligence_42_core.post_items', location='US')
        return 0
    monkeypatch.setattr(apply, 'run', apply_run)
    manifest = {'effects': [], 'manifest_sha256': 'a'*64}
    ri.apply_schema(SchemaClient(), [], manifest, tmp_path, release_id='rel-1111111-01',
                    quiet=lambda: calls.append(('quiet',)), now=lambda: NOW)
    assert [c[0] for c in calls] == ['query', 'quiet', 'query']
    assert len(list(tmp_path.glob('warehouse-install-*.json'))) == 2


@pytest.mark.parametrize('mutation', ['api', 'identity', 'missing_after', 'latest_failure'])
def test_completed_install_receipts_cannot_be_replayed_with_missing_or_inconsistent_metadata(packet, tmp_path, mutation):
    manifest, specs = packet
    client = Client()
    ri.install(client, specs, manifest, tmp_path, release_id='rel-1111111-01', quiet=lambda: None, now=lambda: NOW)
    ri.require_receipts(manifest, tmp_path, 'rel-1111111-01')
    paths = sorted(tmp_path.glob('*.json'))
    if mutation == 'missing_after':
        paths[2].unlink()
    else:
        selected = paths[2] if mutation == 'api' else paths[3]
        data = json.loads(selected.read_text())
        if mutation == 'api':
            data['api']['definitionBody'] = 'SELECT 0'
        elif mutation == 'identity':
            data['manifest_sha256'] = '0'*64
        else:
            data['state'] = 'failed'
        selected.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        ri.require_receipts(manifest, tmp_path, 'rel-1111111-01')


def test_jobs_update_rechecks_native_definitions_even_with_complete_receipts(packet, monkeypatch):
    manifest, specs = packet
    monkeypatch.setattr(jobs_only, 'load_durable', lambda _: manifest)
    class BQ:
        def definition(self, target):
            expected = next(s['definition'] for s in specs if target == s['definition']['kind'] + ':' + s['target'])
            return {**expected, 'body': 'SELECT 0'}
    with pytest.raises(jobs_run.Stop) as error:
        jobs_only.check_warehouse_definitions({}, BQ())
    assert error.value.code == 'WAREHOUSE_READBACK'


@pytest.mark.parametrize('missing', ['review', 'compatibility'])
def test_missing_real_manifest_review_or_compatibility_stops_before_client(inputs, packet, monkeypatch, tmp_path, missing):
    manifest, _ = packet
    if missing == 'review':
        manifest['reviewed_by'] = ''
    else:
        manifest['effects'][0]['compat']['a80_readers']['verdict'] = 'pending'
    manifest['manifest_sha256'] = de.manifest_hash(manifest)
    monkeypatch.setattr(jobs_only, 'load_durable', lambda _: manifest)
    calls = []
    bound = {'target': '1'*40, 'tree': '2'*40, 'release_id': 'rel-1111111-01'}
    with pytest.raises(jobs_run.Stop):
        jobs_run.run_warehouse_install(bound, tmp_path, source_files=inputs, client_factory=lambda: calls.append('client'),
                                        quiet=lambda: calls.append('quiet'), now=lambda: NOW)
    assert calls == []


def test_missing_schema_effect_cannot_hide_behind_renumbered_warehouse_effects(inputs, packet, monkeypatch, tmp_path):
    manifest, _ = packet
    removed = next(e for e in manifest['effects'] if e['effect_id'] == 'E-POSTITEMS-LINKED')
    manifest['effects'].remove(removed)
    for effect in manifest['effects']:
        if effect['apply_order'] > removed['apply_order']:
            effect['apply_order'] -= 1
    manifest['manifest_sha256'] = de.manifest_hash(manifest)
    monkeypatch.setattr(jobs_only, 'load_durable', lambda _: manifest)
    calls = []
    bound = {'target': '1'*40, 'tree': '2'*40, 'release_id': 'rel-1111111-01'}
    with pytest.raises(jobs_run.Stop) as error:
        jobs_run.run_warehouse_install(bound, tmp_path, source_files=inputs, client_factory=lambda: calls.append('client'),
                                        quiet=lambda: calls.append('quiet'), now=lambda: NOW)
    assert error.value.code == 'WAREHOUSE_MANIFEST'
    assert calls == []
