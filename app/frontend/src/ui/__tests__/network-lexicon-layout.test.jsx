import {expect, test} from 'bun:test';
import {buildGraph, networkReadiness} from '../../network.jsx';
import {wowParts} from '../../lexicon.jsx';
import {buildBriefingState} from '../../briefingContract.js';
import {buildExploreState} from '../../explore.jsx';
import {routeRunFacts} from '../../instrumentRouteModels.js';
import {DESK_AMBER_FRESHNESS} from './fixtures/instrument-payloads.js';

test('Network gives twelve creator targets space without changing their relationships', () => {
  const creators = Array.from({length: 12}, (_, index) => ({handle: `voice${index}`, market: 'ZA', platform: 'tiktok', reach: 1000 - index}));
  const topics = Array.from({length: 7}, (_, index) => ({id: `topic${index}`, region: 'ZA', topic: `Topic ${index}`, score: 1 - index / 10, creators_list: creators.map((row) => '@' + row.handle)}));
  const graph = buildGraph({topics}, {creators});
  const positions = graph.creators.map((row) => graph.creatorRow.get(row.graphKey) * graph.plotHeight / 100);
  expect(graph.edges).toHaveLength(84);
  for (let index = 1; index < positions.length; index += 1){
    expect(positions[index] - positions[index - 1]).toBeGreaterThanOrEqual(63.9);
  }
});

test('Network does not invent an edge for an unmatched creator', () => {
  const graph = buildGraph({topics: [{id: 'topic', region: 'ZA', score: 1, creators_list: ['@missing']}]}, {creators: [{handle: 'present', market: 'ZA', platform: 'tiktok', reach: 100}]});
  expect(graph.edges).toHaveLength(0);
  expect(graph.creators).toHaveLength(0);
});

const topic = (fields = {}) => ({id: 'repair', region: 'ZA', topic: 'Repair culture', score: .8, creators_list: ['@maker'], ...fields});
const creator = (fields = {}) => ({handle: 'maker', market: 'ZA', platform: 'tiktok', reach: 100, posts: 2, ...fields});

test('Network does not admit an object as a rendered topic label', () => {
  const graph = buildGraph({topics: [topic({topic: {text: 'Repair'}})]}, {creators: [creator()]});
  expect(graph.topics[0].topic).toBe('repair');
});

test('Network distinguishes invalid envelopes from an explicitly empty collection', () => {
  const missing = buildGraph({error: 'schema_problem'}, {creators: [creator()]});
  expect(missing.validInput).toBe(false);
  expect(missing.coverage.receivedTopics).toBeNull();
  expect(missing.coverage.matchedTopics).toBeNull();
  const empty = buildGraph({topics: []}, {creators: []});
  expect(empty.validInput).toBe(true);
  expect(empty.coverage.receivedTopics).toBe(0);
});

test('Network joins a matching creator beyond the former twelve-row cut', () => {
  const creators = Array.from({length: 12}, (_, i) => creator({handle: 'unlinked' + i, reach: 1000 - i})).concat(creator());
  const graph = buildGraph({topics: [topic()]}, {creators});
  expect(graph.edges).toHaveLength(1);
  expect(graph.creators[0].handle).toBe('maker');
});

test('Network joins a matching topic beyond the former seven-row cut', () => {
  const topics = Array.from({length: 7}, (_, i) => topic({id: 'unlinked' + i, score: 1, creators_list: []})).concat(topic());
  expect(buildGraph({topics}, {creators: [creator()]}).edges).toHaveLength(1);
});

test('Network refuses same-handle cross-market edges', () => {
  expect(buildGraph({topics: [topic()]}, {creators: [creator({market: 'NG'})]}).edges).toHaveLength(0);
});

test('Network rejects a response outside the requested market without relabelling it', () => {
  const graph = buildGraph({topics: [topic({region: 'NG'})]}, {creators: [creator({market: 'NG'})]}, 'ZA');
  expect(graph.validInput).toBe(false);
  expect(graph.reason).toBe('scope_mismatch');
  expect(graph.edges).toHaveLength(0);
  expect(buildGraph({topics: [topic({region: 'NG'})]}, {creators: [creator({market: 'NG'})]}, 'ALL').validInput).toBe(true);
});

test('Network rejects separately recorded links outside the requested market', () => {
  const graph = buildGraph({topics: [topic()], bridges: [{h: '@maker', mk: 'NG', from: 'Music', to: 'Food'}]}, {creators: [creator()]}, 'ZA');
  expect(graph.validInput).toBe(false);
});

test('Network gives same topic and handle in different markets distinct identities and coordinates', () => {
  const graph = buildGraph({topics: [topic(), topic({region: 'NG'})]}, {creators: [creator(), creator({market: 'NG'})]});
  expect(graph.edges).toHaveLength(2);
  expect(new Set(graph.edges.map(edge => edge.key)).size).toBe(2);
  expect(new Set(graph.creators.map(row => graph.creatorRow.get(row.graphKey))).size).toBe(2);
  for (const edge of graph.edges){
    expect(graph.topics.find(row => row.graphKey === edge.topicKey).region).toBe(graph.creators.find(row => row.graphKey === edge.creatorKey).market);
  }
});

test.each([
  [creator(), creator({platform: 'instagram'})],
  [creator(), creator({platform: null})],
  [creator(), creator({reach: 101})],
])('Network withholds an ambiguous voice identity', (...creators) => {
  expect(buildGraph({topics: [topic()]}, {creators}).edges).toHaveLength(0);
});

test.each([null, '', 'ALL', 'unknown'])('Network refuses an absent or unknown record market', market => {
  expect(buildGraph({topics: [topic({region: market})]}, {creators: [creator({market})]}).edges).toHaveLength(0);
});

test('Network normalizes supplied handle whitespace and casing before joining', () => {
  const graph = buildGraph({topics: [topic({creators_list: [' @MaKeR', '@maker']})]}, {creators: [creator({handle: ' @Maker '})]});
  expect(graph.edges).toHaveLength(1);
});

test('Network deduplicates identical records without adding their measurements', () => {
  const graph = buildGraph({topics: [topic(), topic()]}, {creators: [creator(), creator()]});
  expect(graph.edges).toHaveLength(1);
  expect(graph.creators).toHaveLength(1);
  expect(graph.creators[0].reach).toBe(100);
});

test('Network keeps distinct Unicode identities in deterministic order', () => {
  const creators = [creator({handle: 'caf\u00e9'}), creator({handle: 'cafe\u0301'})];
  const topics = [topic({creators_list: creators.map(row => row.handle)})];
  const first = buildGraph({topics}, {creators});
  const reversed = buildGraph({topics}, {creators: [...creators].reverse()});
  expect(first.creators).toHaveLength(2);
  expect(first.edges).toEqual(reversed.edges);
});

test('Network caps a connected graph with stable ordering and resolvable displayed endpoints', () => {
  const creators = Array.from({length: 20}, (_, i) => creator({handle: 'voice' + i}));
  const topics = Array.from({length: 10}, (_, i) => topic({id: 'topic' + i, creators_list: creators.map(row => row.handle)}));
  const graph = buildGraph({topics}, {creators});
  const reversed = buildGraph({topics: [...topics].reverse()}, {creators: [...creators].reverse()});
  expect(graph.topics).toHaveLength(7);
  expect(graph.creators).toHaveLength(12);
  expect(graph.edges).toEqual(reversed.edges);
  expect(graph.coverage.matchedTopics).toBe(10);
  expect(graph.coverage.matchedCreators).toBe(20);
  expect(graph.coverage.limited).toBe(true);
  for (const edge of graph.edges){
    expect(graph.topicRow.has(edge.topicKey)).toBe(true);
    expect(graph.creatorRow.has(edge.creatorKey)).toBe(true);
  }
});

test('Network does not convert separately windowed bridge labels into graph edges', () => {
  const graph = buildGraph({topics: [topic({creators_list: []})], bridges: [{creator: '@maker', topics: ['Repair culture'], note: '14-day observation'}]}, {creators: [creator()]});
  expect(graph.edges).toHaveLength(0);
});

/* The graph dates itself from the desk freshness stamp, and the stamp only
   decides a read that has nothing to draw. The three desk routes reach their
   stale branch the same way, so the readiness of a populated graph is held
   against theirs on the same payload rather than asserted alone.

   bq.py sets FRESHNESS_AMBER_HOURS at 3.0 and the desk answer carries the
   block on every payload, so a daily pipeline is amber for most of the day.
   A stamp that switched a drawn graph off would blank the route for those
   hours, which is the regression these cases stand against. */
const AMBER_DATED = {
  read: true,
  validInput: true,
  dated: Boolean(DESK_AMBER_FRESHNESS.status === 'amber' && routeRunFacts({freshness: DESK_AMBER_FRESHNESS}).checkedAt),
};

test('the amber desk stamp this suite uses can date a run', () => {
  expect(DESK_AMBER_FRESHNESS.status).toBe('amber');
  expect(AMBER_DATED.dated).toBe(true);
});

test('Network draws a populated graph under an amber desk stamp rather than blanking it', () => {
  const state = networkReadiness({...AMBER_DATED, edges: 3});
  expect(state.drawable).toBe(true);
  expect(state.stale).toBe(false);
  expect(state.ready).toBe(true);
});

test('Network reaches its stale branch only once the run has nothing to draw', () => {
  const empty = networkReadiness({...AMBER_DATED, edges: 0});
  expect(empty.drawable).toBe(false);
  expect(empty.stale).toBe(true);
  expect(empty.ready).toBe(false);
});

test('Network and the three desk routes agree on the same amber payload', () => {
  const freshness = DESK_AMBER_FRESHNESS;
  const topics = [{id: 'repair', region: 'ZA', topic: 'Repair culture', score: 0.8, creators_list: ['@maker']}];
  const graph = buildGraph({topics, freshness}, {creators: [creator()]}, 'ZA');
  expect(graph.edges.length).toBeGreaterThan(0);
  const network = networkReadiness({read: true, validInput: graph.validInput, edges: graph.edges.length, dated: AMBER_DATED.dated});
  /* Briefing reads the same desk topic rows this graph is drawn from, so a
     run that carries records is read by both and dated by neither. */
  expect(buildBriefingState({topics, freshness}).state).toBe('ready');
  expect(network.stale).toBe(false);
  /* A run that carries nothing is dated by the stamp on every route. Explore
     reads discovered signals rather than desk topic rows, so it is held
     against this payload only where the empty list means the same on both. */
  const barren = buildGraph({topics: [], freshness}, {creators: []}, 'ZA');
  expect(barren.edges).toHaveLength(0);
  expect(buildBriefingState({topics: [], freshness}).state).toBe('stale');
  expect(buildExploreState({topics: [], freshness}).state).toBe('stale');
  expect(networkReadiness({read: true, validInput: barren.validInput, edges: 0, dated: AMBER_DATED.dated}).stale).toBe(true);
});

test('Network does not call an undated amber run stale', () => {
  const state = networkReadiness({read: true, validInput: true, edges: 0, dated: false});
  expect(state.stale).toBe(false);
  expect(state.ready).toBe(true);
});

test('Network is neither ready nor stale while a read is outstanding or the records are unverified', () => {
  expect(networkReadiness({read: false, validInput: true, edges: 3, dated: true})).toEqual({drawable: false, stale: false, ready: false});
  /* Records the graph could not verify are not drawn whatever they count,
     so an unverified read is not drawable even with links in hand. */
  expect(networkReadiness({read: true, validInput: false, edges: 3, dated: true})).toEqual({drawable: false, stale: false, ready: false});
});

test('declining Lexicon text uses the theme-aware readable red', () => {
  expect(wowParts(-12)).toEqual({text: '▼ 12%', color: 'var(--accent-text)'});
});

test.each([NaN, Infinity, -Infinity, null, '12'])('Lexicon does not print an unmeasured change %s', (value) => {
  expect(wowParts(value)).toBeNull();
});

/* An amber status with no stamp and nothing to draw is held, not a measured
   empty, and not stale either: the route frame shows it as unavailable with
   checked_at_missing, the branch the desk routes reach on the same run. */
test('Network holds an undated amber run that has nothing to draw, without calling it stale', () => {
  expect(networkReadiness({read: true, validInput: true, edges: 0, dated: false, amber: true}))
    .toEqual({drawable: false, stale: false, ready: false});
  expect(networkReadiness({read: true, validInput: true, edges: 3, dated: false, amber: true}))
    .toEqual({drawable: true, stale: false, ready: true});
  expect(networkReadiness({read: true, validInput: true, edges: 0, dated: false, amber: false}))
    .toEqual({drawable: false, stale: false, ready: true});
});
