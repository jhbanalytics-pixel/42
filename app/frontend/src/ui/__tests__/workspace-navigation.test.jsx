import {afterAll, expect, mock, test} from 'bun:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

/* mock.module is process wide and it patches the live api.js module every
   later file already holds a binding to. bun walks the test directory in
   filesystem order, which is alphabetical on NTFS and is not on the ext4
   runner, so an unrestored stub here reached briefing-contract on Linux and
   never did on Windows. Snapshot the real exports before mocking and put
   them back when this file is done. */
const realApi = {...(await import('../../api.js'))};

mock.module('../../api.js', () => ({
  ...realApi,
  PASS_KEY: 'pulse-passcode',
  clearCache: () => {},
  storedValue: (_key, fallback = null) => fallback,
  useApi: () => [{state: 'loading'}, () => {}],
  apiGetFresh: () => Promise.reject(new Error('not called during static render')),
  apiPost: () => Promise.reject(new Error('not called during static render')),
}));

afterAll(() => { mock.module('../../api.js', () => realApi); });

const {loadSourceLab, SourceLabView} = await import('../../sourceLab.jsx');
const {HistoricalWorkspaceView} = await import('../../historicalWorkspace.jsx');
const {OgilvyShell} = await import('../OgilvyShell.jsx');
const appModule = await import('../../App.jsx');

const sourcePayload = {
  contract_version: '2.2.0',
  resource_version: 'source_lab_inventory_v2',
  client_scope_id: 'ogilvy_default',
  market_scope: ['za', 'ng', 'ke'],
  brand_config_id: 'ogilvy_42',
  audience_lens_ids: [],
  theme_id: 'ogilvy_intelligence',
  run_id: 'source_lab_2026-08-28',
  catalog: {expectation_version: 'catalog_v1', route_count: 1, checked_at: '2026-08-28T08:00:00Z', completeness_state: 'verified_snapshot'},
  funding: {balance: 250100, recent_deductions: 0, funding_math_status: 'unknown', optional_calls_enabled: false, observed_at: '2026-08-28T08:00:00Z'},
  sources: [{endpoint_id: 'ep_1', platform: 'Reddit', route_path: '/v1/reddit/search', official_capability: 'Searches public posts.', official_credits: 1, official_parameters: ['query'], cache_ttl_seconds: 3600, calls: null, credits: null, rows: null, integrity: null, geo_precision: null, unique_lift: null, docs_url: 'https://example.invalid/docs'}],
  credit_budget: {
    contract_version: 'credit_budget_v2', state: 'blocked', month_start: '2026-08-01', unit: 'vendor_credits',
    lanes: [
      {credential_lane: 'ogilvy_funded', funding_account: 'ogilvy_albert', activation_stage: 0, opening_balance: 250100, current_balance: 250100, month_opening_balance: 250100, monthly_cap: 25000, monthly_ledger_debit: 0, monthly_balance_delta: 0, monthly_effective_spend: 0, monthly_remaining: 25000, reserve_floor: 225000, reserve_remaining: 25100, stage_cap: 0, run_allowance: 0, attribution_state: 'complete', kill_state: 'not_tested', ledger_through: '2026-08-30T07:00:00Z', limitation: 'Paid calls remain disabled until the approved activation gate passes.'},
      {credential_lane: 'jhb_core', funding_account: 'jhb_analytics', activation_stage: null, opening_balance: null, current_balance: null, month_opening_balance: null, monthly_cap: null, monthly_ledger_debit: null, monthly_balance_delta: null, monthly_effective_spend: null, monthly_remaining: null, reserve_floor: null, reserve_remaining: null, stage_cap: null, run_allowance: null, attribution_state: 'unavailable', kill_state: 'not_applicable', ledger_through: null, limitation: 'This credential remains operationally separate and has no funded-lane budget authority.'},
    ],
    limitation: 'Enumeration is not enablement. A positive allowance does not authorize a source or a run.',
  },
};

test('Source Lab requests one exact 2.2 contract', async () => {
  const paths = [];
  await loadSourceLab((path) => { paths.push(path); return Promise.resolve(sourcePayload); });
  expect(paths).toEqual(['/api/v2/source-lab?contract_version=2.2.0']);
});

test('Source Lab renders loading, ready and empty-as-error without enablement claims', () => {
  expect(renderToStaticMarkup(<SourceLabView state="loading" />)).toContain('Loading Source Lab');
  const ready = renderToStaticMarkup(<SourceLabView state="ready" data={sourcePayload} />);
  expect(ready).toContain('Verified snapshot');
  expect(ready).toContain('Unknown, not zero');
  expect(ready).toContain('Open evidence');
  expect(ready).toContain('ogilvy_funded');
  expect(ready).toContain('jhb_core');
  expect(ready).toContain('Unmeasured');
  expect(ready).not.toMatch(/enabled route|safe pilot|active source/i);
  /* A verified snapshot documenting zero routes is a true empty, which is a
     fact about the estate, not a failure of the read. Folding it into the
     error alert told the strategist the snapshot could not be verified when it
     verified fine. Consistency still gates it: a catalog claiming routes while
     the list is empty is a broken snapshot and stays an error. */
  const empty = renderToStaticMarkup(
    <SourceLabView state="ready" data={{...sourcePayload,
      catalog: {...sourcePayload.catalog, route_count: 0}, sources: []}} />,
  );
  expect(empty).toContain('No sources are documented in this catalogue yet');
  expect(empty).not.toContain('could not verify');
  expect(empty).not.toContain('role="alert"');
  const inconsistent = renderToStaticMarkup(
    <SourceLabView state="ready" data={{...sourcePayload, sources: []}} />,
  );
  expect(inconsistent).toContain('Source Lab could not verify a complete snapshot');
});

test('Source Lab fetch failure keeps its retry path and shows no rows', () => {
  const markup = renderToStaticMarkup(
    <SourceLabView state="error" data={sourcePayload} error="workspace_unavailable" onRetry={() => {}} />,
  );
  expect(markup).toContain('role="alert"');
  expect(markup).toContain('workspace_unavailable');
  expect(markup).toContain('>Retry<');
  expect(markup).not.toContain('workspace-row');
  expect(markup).not.toContain('Reddit');
  expect(markup).not.toContain('250100');
});

test('Historical loading names its mode and claims nothing else', () => {
  const markup = renderToStaticMarkup(<HistoricalWorkspaceView state="loading" mode="analogue" />);
  expect(markup).toContain('aria-busy="true"');
  expect(markup).toContain('Loading analogue evidence');
  expect(markup).not.toMatch(/No eligible Historical evidence/);
});

test('a cold Historical mount with an investigation paints loading first', async () => {
  const {HistoricalWorkspace} = await import('../../historicalWorkspace.jsx');
  const markup = renderToStaticMarkup(
    <HistoricalWorkspace route={{investigationId: 'inv_case', mode: 'recurrence', error: null}} />,
  );
  expect(markup).toContain('aria-busy="true"');
  expect(markup).toContain('Loading recurrence evidence');
});

test('Source Lab renders unavailable funding values as Unknown rather than null', () => {
  const data = {
    ...sourcePayload,
    funding: {...sourcePayload.funding, balance: null, recent_deductions: null},
  };
  const markup = renderToStaticMarkup(<SourceLabView state="ready" data={data} />);
  expect(markup).toContain('Unknown');
  expect(markup).not.toContain('>null<');
});

test('Historical shows landing and unavailable states without fixture or diffusion content', () => {
  const landing = renderToStaticMarkup(<HistoricalWorkspaceView state="landing" />);
  expect(landing).toContain('Open an investigation from Build');
  const unavailable = renderToStaticMarkup(<HistoricalWorkspaceView state="error" mode="analogue" error={{code: 'workspace_unavailable'}} />);
  expect(unavailable).toContain('Historical evidence is unavailable');
  expect(unavailable).not.toMatch(/fixture|diffusion/i);
});

/* A failed Historical read left the reader with Try again and nothing else:
   no route back to the investigation they came from. The landing already
   offers Back to Build; the failure offers the same way out beside the
   re-read, and it is a button that navigates, not a new route. */
test('a failed Historical read offers the way back to Build beside the re-read', () => {
  const unavailable = renderToStaticMarkup(<HistoricalWorkspaceView state="error" mode="analogue" error={{code: 'workspace_unavailable'}} onRetry={() => {}} />);
  expect(unavailable).toContain('>Try again</button>');
  expect(unavailable).toContain('>Back to Build</button>');
  expect(unavailable.indexOf('>Try again</button>')).toBeLessThan(unavailable.indexOf('>Back to Build</button>'));
});

test('utility navigation renders approved anchors outside primary jobs', () => {
  const markup = renderToStaticMarkup(
    <OgilvyShell route="source-lab" setRoute={() => {}} region="ZA" setRegion={() => {}}>
      <main>Workspace</main>
    </OgilvyShell>,
  );
  expect(markup).toContain('href="#/source-lab"');
  expect(markup).toContain('href="#/historical"');
  expect(markup.indexOf('Source Lab')).toBeLessThan(markup.indexOf('Historical'));
});

test('workspace CSS guards 390px overflow and 48px evidence targets', async () => {
  const css = await Bun.file(new URL('../../styles/workspaces.css', import.meta.url)).text();
  expect(css).toContain('min-width: 0');
  /* break-word rather than anywhere: the guard still stops a long token
     overflowing at 390, and it stops the browser breaking a word that had room
     on the next line, which is what round 10 measured on three routes. */
  expect(css).toContain('overflow-wrap: break-word');
  expect(css).toMatch(/min-height:\s*48px/);
  expect(css).toMatch(/@media \(max-width: 600px\)/);
  expect(css).toMatch(/@media \(prefers-reduced-motion: reduce\)/);
});

test('package route IDs map to existing host hashes and explicit market arrays', () => {
  expect(appModule.instrumentRouteForHostRoute?.('pulse')).toBe('briefing');
  expect(appModule.instrumentRouteForHostRoute?.('explore')).toBe('discover');
  expect(appModule.instrumentRouteForHostRoute?.('compare')).toBe('compare');
  expect(appModule.instrumentRouteForHostRoute?.('console')).toBe('build');
  /* A utility workspace is no job: the rail marks nothing current on it. */
  expect(appModule.instrumentRouteForHostRoute?.('source-lab')).toBeNull();
  expect(appModule.instrumentRouteForHostRoute?.('fieldwork')).toBe('fieldwork');
  expect(appModule.hostRouteForInstrumentRoute?.('/briefing')).toBe('pulse');
  expect(appModule.hostRouteForInstrumentRoute?.('/discover')).toBe('explore');
  expect(appModule.hostRouteForInstrumentRoute?.('/build')).toBe('console');
  expect(appModule.hostRouteForInstrumentRoute?.('/fieldwork')).toBe('fieldwork');
  expect(appModule.marketScopeForRegion?.('ZA')).toEqual(['ZA']);
  expect(appModule.marketScopeForRegion?.('ALL')).toEqual(['ZA', 'NG', 'KE']);
});

/* Round 5, task 32. The rail carries the five jobs and nothing else. App
   builds no utility links for the shell, and the legacy OgilvyShell above,
   which still lists the utility navigation, is mounted by no route. The
   workspaces outside the jobs open by hash alone. */
/* Quiet register, 23 Sept 2026: App hands the shell only the page the
   reader is on, as its one utility entry, never the utility navigation. */
test('App hands the shell at most the current page and mounts no legacy utility navigation', async () => {
  expect(appModule.utilityLinksForHostRoute).toBeUndefined();
  const source = await Bun.file(new URL('../../App.jsx', import.meta.url)).text();
  expect(source).toContain('utilityLinks={railPlace.currentPage ? [railPlace.currentPage] : undefined}');
  expect(source).not.toContain('OgilvyShell');
  for (const route of ['source-lab', 'historical', 'method', 'seeds', 'listen']){
    expect(appModule.resolveHostRoute?.(route)).toBe(route);
    expect(appModule.instrumentRouteForHostRoute?.(route)).toBeNull();
  }
});

/* Round 2, task 39. The package toggles one chip at a time and the desk
   answers one market or all three, so the host reads each click as a move
   between those two shapes. From a single market a second chip returns the
   scope to ALL. From ALL, where every chip is checked, a click unchecks one
   and the two left over cannot be served, so the click narrows to the chip
   the reader touched. Unchecking the only checked chip stays as it is; the
   package refuses the empty set before it reaches the host. */
const MARKET_TRANSITIONS = [
  ['ALL', ['ZA'], 'ZA'],
  ['ALL', ['NG'], 'NG'],
  ['ALL', ['KE'], 'KE'],
  ['ALL', ['NG', 'KE'], 'ZA'],
  ['ALL', ['ZA', 'KE'], 'NG'],
  ['ALL', ['ZA', 'NG'], 'KE'],
  ['ALL', ['ZA', 'NG', 'KE'], 'ALL'],
  ['ZA', ['ZA', 'NG'], 'ALL'],
  ['ZA', ['ZA', 'KE'], 'ALL'],
  ['NG', ['ZA', 'NG'], 'ALL'],
  ['NG', ['NG', 'KE'], 'ALL'],
  ['KE', ['ZA', 'KE'], 'ALL'],
  ['KE', ['NG', 'KE'], 'ALL'],
  ['ZA', [], 'ZA'],
  ['NG', [], 'NG'],
  ['KE', [], 'KE'],
  ['ZA', ['ZA'], 'ZA'],
  ['NG', ['NG'], 'NG'],
  ['KE', ['KE'], 'KE'],
  ['ZA', ['ZA', 'NG', 'KE'], 'ALL'],
];

test('every package market click lands on one market or all three', () => {
  for (const [current, scope, expected] of MARKET_TRANSITIONS){
    expect([current, scope.join(','), appModule.regionForMarketScope?.(current, scope)]).toEqual([current, scope.join(','), expected]);
  }
  /* Every reachable click has a row: three from ALL, two from each single
     market, one uncheck on each. */
  const reachable = MARKET_TRANSITIONS.filter(([current, scope]) => (
    (current === 'ALL' && scope.length === 2) || (current !== 'ALL' && (scope.length === 2 || scope.length === 0))
  ));
  expect(reachable).toHaveLength(12);
});

/* Albert, 5 October 2026. Communities reads one market, so the header holds
   one chip there and the chip the reader touches is the market the page
   moves to. Unchecking the only chip leaves the page where it is. */
test('a header chip on Communities moves the page to that market', () => {
  const moves = [
    ['ZA', ['ZA', 'NG'], 'NG'],
    ['ZA', ['ZA', 'KE'], 'KE'],
    ['NG', ['ZA', 'NG'], 'ZA'],
    ['NG', ['NG', 'KE'], 'KE'],
    ['KE', ['ZA', 'KE'], 'ZA'],
    ['KE', ['NG', 'KE'], 'NG'],
    ['ZA', [], 'ZA'],
    ['NG', [], 'NG'],
    ['KE', ['KE'], 'KE'],
  ];
  for (const [current, scope, expected] of moves){
    expect([current, scope.join(','), appModule.communitiesMarketForScope?.(current, scope)]).toEqual([current, scope.join(','), expected]);
  }
});
