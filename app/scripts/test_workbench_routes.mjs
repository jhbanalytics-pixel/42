/* Workbench URL compatibility smoke (Phase 0). */
import {
  parseWorkbenchRoute,
  buildWorkbenchHash,
  buildWorkbenchPath,
  resolveResearchRouteSelection,
  resolveResearchPersonaSelection,
} from '../frontend/src/workbenchRoute.js';

const cases = [
  {
    hash: '#/research?artifact=ra_abc123',
    expect: {route: 'research', work: 'brief', investigationId: null, artifactId: 'ra_abc123', personaId: null, markets: null, topics: null, clientLensId: null},
  },
  {
    hash: '#/console?work=brief&artifact=ra_abc123',
    expect: {route: 'console', work: 'brief', investigationId: null, artifactId: 'ra_abc123', personaId: null, markets: null, topics: null, clientLensId: null},
  },
  {
    hash: '#/research?persona_id=digital_architect_genz&markets=ng,ke',
    expect: {route: 'research', work: 'brief', investigationId: null, artifactId: null, personaId: 'digital_architect_genz', markets: ['ng', 'ke'], topics: null, clientLensId: null},
  },
  {
    hash: '#/console?mode=research',
    expect: {route: 'console', work: 'brief', investigationId: null, artifactId: null, personaId: null, markets: null, topics: null, clientLensId: null},
  },
  {
    hash: '#/console?work=brief&persona_id=digital_architect_genz',
    expect: {route: 'console', work: 'brief', investigationId: null, artifactId: null, personaId: 'digital_architect_genz', markets: null, topics: null, clientLensId: null},
  },
  {
    hash: '#/console?research=ra_legacy',
    expect: {route: 'console', work: 'brief', investigationId: null, artifactId: 'ra_legacy', personaId: null, markets: null, topics: null, clientLensId: null},
  },
  {
    hash: '#/console?artifact=ra_legacy2',
    expect: {route: 'console', work: 'brief', investigationId: null, artifactId: 'ra_legacy2', personaId: null, markets: null, topics: null, clientLensId: null},
  },
  {
    /* The contract's canonical Client Read hash: the artifact preview inside
       the investigation, never a work state of its own. */
    hash: '#/console?work=brief&investigation=inv_abc123&artifact=ra_abc123',
    expect: {route: 'console', work: 'brief', investigationId: 'inv_abc123', artifactId: 'ra_abc123', personaId: null, markets: null, topics: null, clientLensId: null},
  },
  {
    /* An invented work value is not a route. It must not become one. */
    hash: '#/console?work=read&investigation=inv_abc123',
    expect: {route: 'console', work: 'landing', investigationId: 'inv_abc123', artifactId: null, personaId: null, markets: null, topics: null, clientLensId: null},
  },
  {
    hash: '#/console?work=ask',
    expect: {route: 'console', work: 'ask', investigationId: null, artifactId: null, personaId: null, markets: null, topics: null, clientLensId: null},
  },
  {
    hash: '#/console',
    expect: {route: 'console', work: 'landing', investigationId: null, artifactId: null, personaId: null, markets: null, topics: null, clientLensId: null},
  },
  {
    /* A console opened under the authorized lens in app/configs/client_lenses.yaml
       reopens under that lens, so the field carries the id rather than null. */
    hash: '#/console?work=ask&lens=bsa_pulse_lens',
    expect: {route: 'console', work: 'ask', investigationId: null, artifactId: null, personaId: null, markets: null, topics: null, clientLensId: 'bsa_pulse_lens'},
  },
];

function eq(a, b){
  return JSON.stringify(a) === JSON.stringify(b);
}

let failed = 0;
for (const c of cases){
  const got = parseWorkbenchRoute(c.hash);
  if (!eq(got, c.expect)){
    console.error('FAIL parse:', c.hash);
    console.error('  got   ', got);
    console.error('  expect', c.expect);
    failed++;
  }
}

const roundTrip = buildWorkbenchHash({
  work: 'brief',
  artifactId: 'ra_xyz',
  personaId: 'digital_architect_genz',
  markets: ['ng', 'ke'],
});
const parsed = parseWorkbenchRoute(roundTrip);
if (parsed.work !== 'brief' || parsed.artifactId !== 'ra_xyz' || parsed.personaId !== 'digital_architect_genz' || !eq(parsed.markets, ['ng', 'ke'])){
  console.error('FAIL share URL round-trip:', roundTrip, parsed);
  failed++;
}

const seedsPath = buildWorkbenchPath({work: 'brief', markets: ['ng', 'ke']});
const seedsRoute = parseWorkbenchRoute(seedsPath);
if (!seedsPath.includes('console?work=brief') || seedsPath.includes('persona_id=')
  || seedsRoute.personaId !== null || !eq(seedsRoute.markets, ['ng', 'ke'])){
  console.error('FAIL audience-neutral seeds deep link:', seedsPath, seedsRoute);
  failed++;
}

const personas = [{
  id: 'strategy_director',
  product_frame: 'Cultural strategy',
  default_markets: ['za', 'ng', 'ke'],
}];
const neutralSelection = resolveResearchRouteSelection(
  {personaId: null, markets: ['ng']},
  personas,
);
if (!eq(neutralSelection, {personaId: '', productFrame: '', markets: ['ng']})){
  console.error('FAIL neutral research selection:', neutralSelection);
  failed++;
}
const explicitSelection = resolveResearchRouteSelection(
  {personaId: 'strategy_director', markets: ['ng']},
  personas,
);
if (!eq(explicitSelection, {
  personaId: 'strategy_director',
  productFrame: 'Cultural strategy',
  markets: ['ng'],
})){
  console.error('FAIL explicit research selection:', explicitSelection);
  failed++;
}
const invalidSelection = resolveResearchRouteSelection(
  {personaId: 'unknown_role', markets: ['ke']},
  personas,
);
if (!eq(invalidSelection, {personaId: '', productFrame: '', markets: ['ke']})){
  console.error('FAIL invalid research selection:', invalidSelection);
  failed++;
}
const retainedMarkets = resolveResearchPersonaSelection(
  'strategy_director',
  personas,
  ['ng'],
);
if (!eq(retainedMarkets, {
  personaId: 'strategy_director',
  productFrame: 'Cultural strategy',
  markets: ['ng'],
})){
  console.error('FAIL persona market preservation:', retainedMarkets);
  failed++;
}
const canonical = buildWorkbenchHash({work: 'brief', investigationId: 'inv_abc123', artifactId: 'ra_abc123'});
if (canonical !== '#/console?work=brief&investigation=inv_abc123&artifact=ra_abc123'){
  // Canonical Console parameter order is exactly work, investigation, artifact.
  console.error('FAIL canonical client read hash build:', canonical);
  failed++;
}
const canonicalBack = parseWorkbenchRoute(canonical);
if (canonicalBack.investigationId !== 'inv_abc123' || canonicalBack.artifactId !== 'ra_abc123'){
  console.error('FAIL canonical client read round trip:', canonicalBack);
  failed++;
}
const lensHash = buildWorkbenchHash({work: 'ask', clientLensId: 'bsa_pulse_lens'});
const lensBack = parseWorkbenchRoute(lensHash);
if (lensHash !== '#/console?work=ask&lens=bsa_pulse_lens' || lensBack.clientLensId !== 'bsa_pulse_lens' || lensBack.work !== 'ask'){
  console.error('FAIL client lens round trip:', lensHash, lensBack);
  failed++;
}
if (buildWorkbenchHash({work: 'read', investigationId: 'inv_abc123'}).includes('work=read')){
  console.error('FAIL a read work state must not be buildable');
  failed++;
}
if (failed){
  console.error(`FAIL: ${failed} workbench route smoke case(s)`);
  process.exit(1);
}
console.log(`OK: ${cases.length + 10} workbench route and research-selection checks passed`);
