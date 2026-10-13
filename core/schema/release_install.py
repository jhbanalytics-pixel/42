"""Closed source-derived warehouse installation for the reviewed jobs release."""
import ast
import json
import re
from pathlib import Path
from types import SimpleNamespace

from core.setup import durable_effects_check as de

PROJECT = 'ogilvy-trends-v2'
LOCATION = 'US'
CORE = 'intelligence_42_core'
AGENT = 'intelligence_42_agent'
APPLY_KIND = 'warehouse_install'
READBACK_KIND = 'warehouse_definition'
PREFIX = 'E-WAREHOUSE-'
TARGETS = tuple(CORE + '.' + n for n in ('tvf_post_items', 'v_good_runs', 'v_collection_health_current',
    'v_item_daily_current', 'v_series_test_current', 'v_coord_signals_current', 'v_item_state_current')) + (
    AGENT + '.v_briefs_current',) + tuple(CORE + '.' + n for n in ('v_item_counter_daily_current',
    'v_series_daily', 'tvf_series_signal', 'tvf_item_window', 'tvf_placebo_base', 'v_post_items_by_state_date',
    'v_item_market_scope')) + (AGENT + '.tvf_search_posts_scoped', AGENT + '.tvf_search_posts')
RENDER_INPUTS = ('core/detect/sqlrun.py',
                 'core/detect/sql/views.sql', 'core/detect/sql/locality_views.sql',
                 'core/detect/sql/counted_links.sql',
                 'core/understand/sql/tvf_search_posts_scoped.sql', 'core/understand/sql/tvf_search_posts.sql')


def normalized_sql(sql):
    tokens = re.findall(r"'(?:''|\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`[^`]*`|--[^\n]*|[\w]+|[^\s]", sql)
    tokens = [t for t in tokens if not t.startswith('--')]
    if tokens and tokens[-1] == ';':
        tokens.pop()
    if tokens and tokens[0] == '(' and tokens[-1] == ')':
        depth = 0
        for index, token in enumerate(tokens):
            depth += (token == '(') - (token == ')')
            if depth == 0:
                if index == len(tokens) - 1:
                    tokens = tokens[1:-1]
                break
    return ' '.join(tokens)


def _datatype(text):
    text = text.strip().upper()
    if text == 'ARRAY<STRING>':
        return {'typeKind': 'ARRAY', 'arrayElementType': {'typeKind': 'STRING'}}
    if text not in ('STRING', 'DATE', 'INT64', 'FLOAT64', 'BOOL'):
        raise ValueError('Unknown fixed routine argument type')
    return {'typeKind': text}


def expected_definition(statement):
    m = re.match(r'CREATE\s+(?:OR\s+REPLACE\s+)?(VIEW|TABLE\s+FUNCTION)\s+(?:IF\s+NOT\s+EXISTS\s+)?`?([\w.\-]+)`?\s*(.*)', statement, re.I | re.S)
    if not m:
        raise ValueError('Unknown warehouse statement header')
    kind, target, rest = m.groups()
    target = '.'.join(target.split('.')[-2:])
    if kind.upper() == 'VIEW':
        if not re.match(r'AS\s+', rest.lstrip(), re.I):
            raise ValueError('View has no SELECT body')
        return {'kind': 'view', 'target': target, 'body': normalized_sql(re.sub(r'^AS\s+', '', rest.lstrip(), count=1, flags=re.I))}
    signature, body = rest.split(')', 1)
    if not signature.startswith('(') or not re.match(r'AS\s+', body.lstrip(), re.I):
        raise ValueError('Routine has no fixed signature/body')
    args = []
    for arg in signature[1:].split(','):
        name, dtype = arg.strip().split(None, 1)
        args.append({'name': name, 'dataType': _datatype(dtype)})
    return {'kind': 'routine', 'target': target, 'routineType': 'TABLE_VALUED_FUNCTION',
            'language': 'SQL', 'arguments': args, 'body': normalized_sql(re.sub(r'^AS\s+', '', body.lstrip(), count=1, flags=re.I))}


def statements(files):
    """The fixed Alpha prerequisite set, rendered from the release tree, with no manifest SQL input."""
    texts = dict(files)
    if 'core/understand/sql/tvf_search_posts_scoped.sql' not in texts:
        return []
    missing = [p for p in RENDER_INPUTS if p not in texts]
    if missing:
        raise ValueError('Missing warehouse renderer source: ' + ', '.join(missing))
    names = ('render', 'split', '_code', '_strip_leading_comments')
    functions = [n for n in ast.parse(texts['core/detect/sqlrun.py']).body
                 if isinstance(n, ast.FunctionDef) and n.name in names]
    if {n.name for n in functions} != set(names):
        raise ValueError('Warehouse renderer contract changed')
    env = {'re': re, 'CORE': CORE, 'AGENT': AGENT,
           'counted_links_body': lambda dates: texts['core/detect/sql/counted_links.sql'].replace('{link_dates}', dates)}
    exec(compile(ast.Module(body=functions, type_ignores=[]), 'bound-sqlrun', 'exec'), env)
    render, split, strip = (env[n] for n in ('render', 'split', '_strip_leading_comments'))
    locality = [strip(s) for s in split(render(texts['core/detect/sql/locality_views.sql']))]
    resolver = [s for s in locality if re.match(r'CREATE OR REPLACE TABLE FUNCTION intelligence_42_core.tvf_post_items\(', s)]
    if len(resolver) != 1:
        raise ValueError('Expected one counted-link resolver')
    result = resolver + [strip(s) for s in split(render(texts['core/detect/sql/views.sql']))]
    for path in RENDER_INPUTS[-2:]:
        pieces = [strip(s) for s in split(texts[path])]
        if len(pieces) != 1 or 'CREATE TABLE FUNCTION IF NOT EXISTS' not in pieces[0]:
            raise ValueError('Semantic routine contract changed')
        result.append(pieces[0].replace('CREATE TABLE FUNCTION IF NOT EXISTS', 'CREATE OR REPLACE TABLE FUNCTION', 1))
    specs = []
    for index, sql in enumerate(result, 1):
        if re.search(r'\{(?:core|agent|counted_links|counted_state_links|resolution_rule_version)\}', sql):
            raise ValueError('Unrendered warehouse placeholder')
        definition = expected_definition(sql)
        specs.append({'effect_id': PREFIX + str(index).zfill(2), 'statement': sql, 'target': definition['target'],
                      'definition': definition, 'readback_hash': de.sha_text(de.canonical(definition))})
    targets = [s['target'] for s in specs]
    if tuple(targets) != TARGETS:
        raise ValueError('Warehouse target set/order is not the fixed Alpha contract')
    if len(targets) != len(set(targets)):
        raise ValueError('Duplicate warehouse target')
    if targets.index(CORE + '.v_post_items_by_state_date') >= targets.index(CORE + '.v_item_market_scope'):
        raise ValueError('Dated-link order changed')
    if targets[-2:] != [AGENT + '.tvf_search_posts_scoped', AGENT + '.tvf_search_posts']:
        raise ValueError('Semantic dependency order changed')
    return specs


def validate_effects(manifest, specs):
    actual = [e for e in manifest['effects'] if e.get('apply_kind') == APPLY_KIND]
    if len(actual) != len(specs):
        raise ValueError('Warehouse effects are not one for one with bound source')
    for effect, spec in zip(actual, specs):
        expected = {'kind': 'view', 'change': 'replace_view', 'apply_phase': 'before_candidate',
                    'effect_id': spec['effect_id'], 'target': spec['target'], 'statement_ref': spec['statement'],
                    'statement_sha256': de.sha_text(spec['statement']),
                    'native_readback': {'kind': READBACK_KIND, 'target': spec['definition']['kind'] + ':' + spec['target'],
                                        'expected_result_sha256': spec['readback_hash']}}
        if any(effect.get(k) != v for k, v in expected.items()):
            raise ValueError('Warehouse effect body, identity, target or readback differs from source')
    orders = [e['apply_order'] for e in actual]
    schema_end = max((e['apply_order'] for e in manifest['effects'] if e.get('apply_kind') == 'schema'), default=0)
    if orders != list(range(schema_end + 1, schema_end + len(specs) + 1)):
        raise ValueError('Warehouse apply order differs from dependency order')


def definition(client, target):
    """Read only the fixed typed view/routine metadata shape."""
    m = re.fullmatch(r'(view|routine):(intelligence_42_(?:core|agent))\.(\w+)', target)
    if not m:
        raise ValueError('Unknown warehouse metadata target')
    kind, dataset, name = m.groups()
    if dataset + '.' + name not in TARGETS:
        raise ValueError('Warehouse metadata target outside the fixed Alpha contract')
    full = PROJECT + '.' + dataset + '.' + name
    obj = client.get_table(full) if kind == 'view' else client.get_routine(full)
    api = obj.to_api_repr()
    return definition_from_api(kind, dataset + '.' + name, api), api


def definition_from_api(kind, target, api):
    dataset, name = target.split('.')
    reference = api.get('tableReference' if kind == 'view' else 'routineReference', {})
    if (reference.get('projectId'), reference.get('datasetId'), reference.get('tableId' if kind == 'view' else 'routineId')) != (
            PROJECT, dataset, name):
        raise ValueError('Warehouse API metadata identity differs from its fixed target')
    result = {'kind': kind, 'target': target}
    if kind == 'view':
        if api.get('type') != 'VIEW' or not api.get('view', {}).get('query'):
            raise ValueError('Warehouse view metadata is not a view')
        result['body'] = normalized_sql(api['view']['query'])
    else:
        result.update({'routineType': api.get('routineType'), 'language': api.get('language'),
                       'arguments': [{'name': a.get('name'), 'dataType': a.get('dataType')} for a in api.get('arguments', [])],
                       'body': normalized_sql(api.get('definitionBody', ''))})
    return result


def _record(evidence, body):
    folder = Path(evidence)
    folder.mkdir(parents=True, exist_ok=True)
    for number in range(1, 10000):
        path = folder / f'warehouse-install-{number:04d}.json'
        try:
            with path.open('x', encoding='utf-8') as output:
                output.write(json.dumps(body, indent=2, sort_keys=True) + '\n')
            return path
        except FileExistsError:
            continue
    raise ValueError('Warehouse receipt sequence exhausted')


def require_receipts(manifest, evidence, release_id):
    effects = [e for e in manifest['effects'] if e.get('apply_kind') == APPLY_KIND]
    if not effects:
        return
    folder = Path(evidence)
    records = []
    for path in sorted(folder.glob('warehouse-install-*.json')):
        if path.is_symlink():
            raise ValueError('Warehouse install receipt is a symlink')
        records.append(json.loads(path.read_text(encoding='utf-8')))
    for effect in effects:
        held = [r for r in records if r.get('effect_id') == effect['effect_id']]
        identity = {'release_id': release_id, 'target': effect['target'],
                    'statement_sha256': effect['statement_sha256'], 'manifest_sha256': manifest['manifest_sha256']}
        if not held or any(any(r.get(k) != v for k, v in identity.items()) for r in held):
            raise ValueError('Warehouse install receipt identity is missing or differs')
        last = held[-1]
        if last.get('state') != 'completed' or last.get('result_sha256') != effect['native_readback']['expected_result_sha256']:
            raise ValueError('Warehouse install latest effect attempt did not complete')
        metadata = next((r for r in reversed(held[:-1]) if r.get('state') in ('before', 'after')), None)
        expected = expected_definition(effect['statement_ref'])
        if metadata is None or metadata.get('definition') != expected or metadata.get('api') is None:
            raise ValueError('Warehouse completion has no matching native definition/signature receipt')
        if definition_from_api(expected['kind'], effect['target'], metadata['api']) != expected:
            raise ValueError('Warehouse receipt API metadata differs from its definition/signature')
        if not last.get('already_matching') and not any(r.get('state') == 'attempted' for r in held[:-1]):
            raise ValueError('Warehouse completion has no recorded attempted write')
    if any(r.get('state') == 'failed' and r.get('phase') == 'preflight' for r in records):
        raise ValueError('Warehouse installation preflight failed in this run')


def apply_schema(client, files, manifest, evidence, *, release_id, quiet, now):
    """Wrap the unchanged schema apply function with the same per-DDL release gate."""
    from core.schema import apply
    from core.setup.release import jobs_source
    schema = jobs_source.schema_statements(files)
    allowed = set(schema)
    effects = {e['statement_sha256']: e for e in manifest['effects'] if e.get('apply_kind') == 'schema'}
    if any(e['statement_ref'] not in allowed for e in effects.values()):
        raise ValueError('Schema effect is not a statement of the bound apply source')

    class GuardedClient:
        def __getattr__(self, name):
            if name not in ('get_table', 'get_dataset'):
                raise AttributeError(name)
            return getattr(client, name)

        def query(self, sql, job_config=None, location=None):
            if location != LOCATION:
                raise ValueError('Schema query location differs from the closed release')
            dry = bool(job_config and job_config.dry_run)
            if sql in allowed:
                if not dry:
                    sha = de.sha_text(sql)
                    effect = effects.get(sha)
                    base = {'schema_version': 1, 'release_id': release_id, 'manifest_sha256': manifest['manifest_sha256'],
                            'effect_id': effect['effect_id'] if effect else 'SCHEMA-UNCHANGED-' + sha,
                            'target': apply.statement_name(sql), 'statement_sha256': sha}
                    try:
                        quiet()
                        _record(evidence, {**base, 'state': 'schema-attempted', 'at': now().isoformat()})
                        job = client.query(sql, job_config=job_config, location=location)
                        job.result()
                        _record(evidence, {**base, 'state': 'schema-completed', 'at': now().isoformat()})
                    except Exception as error:
                        _record(evidence, {**base, 'state': 'failed', 'at': now().isoformat(), 'error_type': type(error).__name__})
                        raise
                    return SimpleNamespace(result=lambda: [])
                return client.query(sql, job_config=job_config, location=location)
            listings = {f'SELECT table_name, table_type FROM `{PROJECT}.{dataset}.INFORMATION_SCHEMA.TABLES` ORDER BY table_name'
                        for dataset in (CORE, AGENT)}
            if sql not in listings:
                raise ValueError('Schema apply attempted an unregistered query')
            return client.query(sql, job_config=job_config, location=location)

    result = apply.run(GuardedClient(), schema, apply=True)
    if result != 0:
        raise ValueError('Guarded schema apply did not complete its readbacks')


def install(client, specs, manifest, evidence, *, release_id, quiet, now):
    """Apply only validated source-derived prerequisites and stop on the first failed boundary."""
    validate_effects(manifest, specs)
    from google.api_core.exceptions import NotFound
    from google.cloud import bigquery

    preflight = ('SELECT pi.post_id, pi.item_id, pi.via, pi.linked_on, pi.link_market, '
                 'e.post_id, e.item_id, e.ended_on, l.post_id, l.item_id, l.linked_on, l.link_market '
                 f'FROM {CORE}.post_items pi CROSS JOIN {CORE}.post_item_end e '
                 f'CROSS JOIN {CORE}.post_item_lineage l LIMIT 0')
    try:
        quiet()
        client.query(preflight, location=LOCATION,
                     job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)).result()
    except Exception as error:
        _record(evidence, {'schema_version': 1, 'release_id': release_id, 'state': 'failed',
                           'phase': 'preflight', 'at': now().isoformat(), 'error_type': type(error).__name__})
        raise
    for spec in specs:
        base = {'schema_version': 1, 'release_id': release_id, 'effect_id': spec['effect_id'],
                'target': spec['target'], 'statement_sha256': de.sha_text(spec['statement']),
                'manifest_sha256': manifest['manifest_sha256']}
        try:
            quiet()
            typed = spec['definition']['kind'] + ':' + spec['target']
            try:
                before, raw = definition(client, typed)
            except NotFound:
                before, raw = None, None
            _record(evidence, {**base, 'state': 'before', 'at': now().isoformat(), 'definition': before, 'api': raw})
            if before == spec['definition']:
                _record(evidence, {**base, 'state': 'completed', 'at': now().isoformat(),
                                   'already_matching': True, 'result_sha256': spec['readback_hash']})
                continue
            client.query(spec['statement'], location=LOCATION,
                         job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)).result()
            quiet()
            _record(evidence, {**base, 'state': 'attempted', 'at': now().isoformat()})
            client.query(spec['statement'], location=LOCATION).result()
            after, api = definition(client, typed)
            _record(evidence, {**base, 'state': 'after', 'at': now().isoformat(), 'definition': after, 'api': api})
            if after != spec['definition']:
                raise ValueError('Warehouse definition/signature differs after apply')
            _record(evidence, {**base, 'state': 'completed', 'at': now().isoformat(),
                               'result_sha256': spec['readback_hash']})
        except Exception as error:
            _record(evidence, {**base, 'state': 'failed', 'at': now().isoformat(), 'error_type': type(error).__name__})
            raise
