import {expect, test} from 'bun:test';
import {apiFailureDetails} from '../../redesignContract.js';

const compareSource = await Bun.file(new URL('../../compare.jsx', import.meta.url)).text();
const graphCss = await Bun.file(new URL('../../styles/graphs.css', import.meta.url)).text();
const packageCss = await Bun.file(new URL('../../../node_modules/ogilvy-intelligence-design-system/dist/style.css', import.meta.url)).text();
const tokens = await Bun.file(new URL('../../tokens.css', import.meta.url)).text();
const appCss = await Bun.file(new URL('../../app.css', import.meta.url)).text();
const appSource = await Bun.file(new URL('../../App.jsx', import.meta.url)).text();
const partsSource = await Bun.file(new URL('../../parts.jsx', import.meta.url)).text();

test('Compare renders the package comparison and keeps no retired card grammar', () => {
  /* The retired Compare compared curated topics and creators in two cards with
     a fixed centre column. Both the grammar and the source it compared are
     gone: Compare now reads the released run through the comparison strip. */
  expect(compareSource).toContain('ComparisonInstrument');
  expect(compareSource).toContain('adaptSignalToInstrumentModel');
  expect(compareSource).toContain('admitExploreSignals');
  for (const retired of ['cmp-metric-row', 'cmp-signal-dot', 'cmp-versus-link', 'cmp-wrap', 'CompareCreators', "'/api/voices?region='"]){
    expect(compareSource).not.toContain(retired);
  }
  expect(compareSource).not.toContain("boxShadow: '0 0");
});

/* The law is that at 390 the comparison signal head and its fields each sit in
   one column, so neither is squeezed into a second track. 2.0.1 still keeps it,
   in two rules instead of one: the head is single column at every width now, and
   the field collapses inside the 390 block. Asserting the two facts rather than
   the old combined rule string. */
test('package comparison fields stack at the 390 pixel route boundary', () => {
  const opens = packageCss.indexOf('@media(max-width:390px)');
  const next = packageCss.indexOf('@media', opens + 1);
  const block = packageCss.slice(opens, next === -1 ? packageCss.length : next);

  expect(packageCss).toContain('@media(max-width:390px)');
  expect(packageCss).toContain('.comparison-instrument__signal-head{grid-template-columns:minmax(0,1fr)');
  expect(block).toContain('.comparison-instrument__field{grid-template-columns:minmax(0,1fr)}');
});

test('Compare states its own unavailable and insufficient cases honestly', () => {
  expect(compareSource).toContain('data-compare-state="error"');
  expect(compareSource).toContain('data-compare-state="loading"');
  expect(compareSource).toContain('data-compare-state="insufficient"');
  /* Compare words its own insufficient frame from the ruled table, because
     only it knows the admitted count. */
  expect(compareSource).toContain('RULED_COPY.insufficient(signals.length)');
  expect(compareSource).toContain('RULED_COPY.noDiscovery');
});

test('The product uses the Ogilvy dossier type stack globally', () => {
  expect(tokens).toContain('--serif: "Ogilvy Serif", OgilvySerif, Georgia, "Times New Roman", serif;');
  expect(tokens).toContain('--sans: "Ogilvy Sans", OgilvySans, Arial, Helvetica, sans-serif;');
  expect(tokens).toContain('--mono: Consolas, "Courier New", monospace;');
  expect(tokens).not.toContain('Newsreader');
  expect(tokens).not.toContain('Hanken Grotesk');
  expect(tokens).not.toContain('IBM Plex Mono');
});

test('Operational passcode configuration is not exposed as user-facing error copy', () => {
  expect(apiFailureDetails(503, {detail: 'Passcode gate not configured'})).toEqual({
    message: 'The intelligence source is unavailable in this session.',
  });
});

test('workspace error envelopes preserve their bounded code and message', () => {
  expect(apiFailureDetails(409, {detail: {
    code: 'workspace_snapshot_stale',
    message: 'Workspace snapshot is stale.',
  }})).toEqual({
    code: 'workspace_snapshot_stale',
    message: 'Workspace snapshot is stale.',
  });
});

test('The shell imports route helpers without preloading the research and chart bundle', () => {
  expect(appSource).toContain("from './workbenchRoute.js'");
  expect(appSource).not.toContain("from './researchLib.jsx'");
  /* The shell and the state frame come from the package's one entry; the
     research and chart bundles stay behind their lazy routes. */
  expect(appSource).toMatch(/import \{InstrumentShell, StateView\} from 'ogilvy-intelligence-design-system'/);
  expect(appSource).not.toContain("from './ui/OgilvyShell.jsx'");
  expect(appSource).not.toContain("from './ui/index.js'");
  expect(partsSource).not.toContain("import Chart from 'chart.js/auto'");
  expect(partsSource).toContain("import('chart.js/auto')");
  expect(partsSource).toContain('useChartConstructor');
});
