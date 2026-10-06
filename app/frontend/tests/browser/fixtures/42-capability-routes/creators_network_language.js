/* The creator profile payload as creator.py assembles it: overview counts,
   the reach series and the post wall. The producer is unversioned and
   returns 404 for a handle with no rows in its window.

   The manifest row names three entries for this job, so the two beside the
   creator profile carry their own payloads: the network graph reads
   /api/desk and /api/voices for one market, and the scoped listen route
   reads /api/intel/mentions for the market its hash carries. Each entry
   declares the states its producer can supply and, for every other required
   state, the refusal names the producer symbol whose limit it claims.

   A failed read carries what these producers actually send. None of the four
   routes raises a coded detail: main.py answers an unhandled read with the
   framework's own body, so the page reads the status and nothing else, and
   the fixture says exactly that rather than inventing a code the product
   cannot produce. */
import {DESK_AMBER_FRESHNESS} from '../../../../src/ui/__tests__/fixtures/instrument-payloads.js';
import {buildScopedReadHash} from '../../../../src/router.js';

export const HANDLE = 'maker_one';
const ENDPOINT = '/api/creator/' + HANDLE;

export const LISTEN_TERM = 'repair';
export const LISTEN_MARKET = 'ZA';
/* The shell remembers another market before the scoped listen route opens, so
   the read market can only come from the hash. */
export const LISTEN_SHELL_MARKET = 'NG';
export const NETWORK_TOPIC = 'Repair culture';
export const NETWORK_MARKET = 'ZA';
export const NETWORK_OTHER_MARKET = 'NG';
export const LISTEN_QUOTE = 'Repair shop on the corner fixed the kettle element for twenty rand.';
export const UNQUOTED_MENTION = 'Repair meet-up notes with no link supplied by the producer.';

/* What the service sends when a read raises inside the handler. main.py
   registers no exception handler, so the framework's server error middleware
   answers, and it answers in plain text, not JSON: status 500, content type
   text/plain; charset=utf-8, body the bare sentence below. There is no code
   in it for the page to print, and no JSON for the page to parse. */
export const UNCODED_SERVER_FAILURE = Object.freeze({__status: 500, contentType: 'text/plain; charset=utf-8', text: 'Internal Server Error'});
export const STATUS_CODE = 'http_500';

function post(index){
  return {
    id: 'post_' + index, url: 'https://example.test/post_' + index, published_at: '2026-09-0' + index + 'T10:00:00Z',
    collected_at: '2026-09-04T10:00:00Z', text: 'Repair tutorial ' + index + ' shared with the community.',
    engagement: 4000 * index, platform: 'Instagram', market: 'za',
  };
}

export function profile(){
  return {
    handle: HANDLE, platform: 'instagram', market: 'za', markets: ['za'], reach: 12000, posts: 3,
    total_engagement: 12000, avg_engagement: 4000, top_engagement: 8000, platforms: ['Instagram'],
    topics: [{id: 'repair_culture', label: 'Repair culture'}],
    opportunity: 'Brief Repair culture through this handle; the audience already follows the repair sequence.',
    reach_series: [{date: '2026-08-20', reach: 2000}, {date: '2026-08-27', reach: 4000}, {date: '2026-09-03', reach: 6000}],
    wall: [post(1), post(2)],
  };
}

/* The desk rows the network graph pairs: each carries the recorded market,
   the topic identity and the handle references the brief listed. */
function deskTopics(handles, market){
  return [
    {id: 'repair_culture', region: market, topic: NETWORK_TOPIC, score: 82, momentum: 'rising', creators_list: handles},
    {id: 'thrift_flip', region: market, topic: 'Thrift flip', score: 61, momentum: 'building', creators_list: handles.slice(0, 1)},
  ];
}

/* The freshness block build_desk_payload puts on every desk answer, the
   empty one included, which is what the graph reads to date itself. */
export const DESK_FRESH = Object.freeze({stamp_utc: '2026-09-12T06:30:00Z', age_hours: 1, status: 'green'});
export const DESK_AMBER = DESK_AMBER_FRESHNESS;

export function deskPayload(handles, {market = NETWORK_MARKET, freshness = DESK_FRESH} = {}){
  return {
    topics: deskTopics(handles, market), lexicon: [], bridges: handles.length
      ? [{mk: market, h: HANDLE, from: NETWORK_TOPIC, to: 'Thrift flip'}] : [],
    freshness: {...freshness}, updated: '2026-09-12T06:30:00Z',
  };
}

export function voicesPayload({market = NETWORK_MARKET} = {}){
  return {
    market_label: market === NETWORK_MARKET ? 'South Africa' : 'Nigeria', region: market.toLowerCase(), trap: null,
    creators: [
      {handle: HANDLE, platform: 'instagram', market, reach: 12000, posts: 3, topics: ['repair_culture'], series: [{date: '2026-08-20', reach: 2000}]},
      {handle: 'fixer_two', platform: 'instagram', market, reach: 8000, posts: 5, topics: ['repair_culture'], series: [{date: '2026-08-20', reach: 1000}]},
    ],
  };
}

/* The other market the empty state offers. Its rows are the market's own, so
   the page that comes back after the switch is a read of Nigeria and not the
   South African answer served again. */
function otherMarketReads(){
  return {
    '/api/desk?region=ng': deskPayload([], {market: NETWORK_OTHER_MARKET}),
    '/api/voices?region=ng': voicesPayload({market: NETWORK_OTHER_MARKET}),
  };
}

function mention(index){
  return {
    date: '2026-09-1' + index + 'T08:00:00Z', title: null, content: LISTEN_QUOTE + ' (' + index + ')',
    host: 'reddit.com', url: 'https://example.test/mention_' + index, category: 'reddit',
    sentiment: index === 1 ? 'positive' : 'negative', has_text: true, market: null,
  };
}

/* listen_feed carries url straight from the row, and the column is nullable,
   so a quote with no link is a shape the producer really sends. */
function unlinkedMention(){
  return {
    date: '2026-09-13T08:00:00Z', title: null, content: UNQUOTED_MENTION,
    host: 'reddit.com', url: null, category: 'reddit', sentiment: null, has_text: true, market: null,
  };
}

export function mentionsPayload(rows){
  return {results: rows.map((row) => (row === 'unlinked' ? unlinkedMention() : mention(row))), cursor: null, has_more: false};
}

/* Every entry of this capability, in the order the manifest job names them.
   A state under states is driven in the browser; a state under reasons is one
   this entry's producer cannot supply, and the refusal names the producer
   symbol that carries the limit so the claim can be read against the file. */
export const ENTRIES = Object.freeze([
  Object.freeze({
    name: 'network',
    route: '#/network',
    producer: 'src/api/creator.py build_voices with src/api/desk.py topics',
    job: 'Read the observed relationships between the topic briefs and the voice records of one market.',
    states: ['populated', 'loading', 'empty', 'stale', 'failed'],
    fixtures: (state) => ({
      /* The populated read carries the amber stamp on purpose. bq.py sets
         FRESHNESS_AMBER_HOURS at 3.0 and the desk answer carries the block
         on every payload, so a daily pipeline serves amber for most of the
         day; a graph that has links has to be drawn under it, and a stamp
         that blanked the route would fail here rather than pass. */
      populated: () => ({'/api/desk': deskPayload(['@' + HANDLE, '@fixer_two'], {freshness: DESK_AMBER}), '/api/voices': voicesPayload()}),
      loading: () => ({'/api/desk': deskPayload(['@' + HANDLE, '@fixer_two']), '/api/voices': {__hold: true}}),
      empty: () => ({'/api/desk': deskPayload([]), '/api/voices': voicesPayload(), ...otherMarketReads()}),
      /* The stale read is the empty one with a date on it, which is the
         branch the stamp decides on this route as on the three desk routes:
         the run carried no pairing, and the stamp is the only thing that can
         date the wait. */
      stale: () => ({'/api/desk': deskPayload([], {freshness: DESK_AMBER}), '/api/voices': voicesPayload()}),
      failed: () => ({'/api/desk': deskPayload(['@' + HANDLE]), '/api/voices': UNCODED_SERVER_FAILURE}),
    }[state]()),
    reasons: {
      thin: {
        symbol: 'build_dynamic_discovery',
        reason: 'The graph draws the matches the two reads produce and prints the received and excluded counts beside them; neither the topic rows nor the voice rows carry an evidence floor for a relationship. The evidence_state the same desk answer sends under build_dynamic_discovery belongs to a discovered signal and not to a topic and voice pairing, so no thin state of this graph can be produced.',
      },
      contradictory: {
        symbol: 'build_voices',
        reason: 'The graph pairs one desk run with one build_voices board for the same market; a board that answers for another market is refused as unverified rather than drawn as a disagreement.',
      },
      held: {
        symbol: 'build_voices',
        reason: 'build_voices returns the reach ranked rows it read and holds nothing back for a decision, so no human decision stands between the two reads and the graph and there is nothing to hold.',
      },
    },
  }),
  Object.freeze({
    name: 'listen',
    route: buildScopedReadHash('listen', LISTEN_TERM, LISTEN_MARKET),
    shellMarket: LISTEN_SHELL_MARKET,
    producer: 'src/api/bq.py listen_feed',
    job: 'Read the recorded mentions of one market as language evidence, with each quote carrying its age and its source.',
    states: ['populated', 'loading', 'empty', 'failed'],
    fixtures: (state) => ({
      populated: () => ({'/api/intel/mentions': mentionsPayload([1, 2, 'unlinked'])}),
      loading: () => ({'/api/intel/mentions': {__hold: true}}),
      empty: () => ({'/api/intel/mentions': mentionsPayload([])}),
      failed: () => ({'/api/intel/mentions': UNCODED_SERVER_FAILURE}),
    }[state]()),
    reasons: {
      thin: {
        symbol: 'listen_feed',
        reason: 'listen_feed returns the rows it read with no floor under them, so a short page is a measured count and not a declared thin state.',
      },
      stale: {
        symbol: 'listen_feed',
        reason: 'listen_feed selects a seven day window on every read and stamps each row with its own age, but sends no freshness block for the page, so the feed cannot date itself as stale.',
      },
      contradictory: {
        symbol: 'listen_feed',
        reason: 'listen_feed quotes recorded posts one at a time and makes no claim that two of them could contradict.',
      },
      held: {
        symbol: 'listen_feed',
        reason: 'A row listen_feed reads is published or absent; no human decision holds a recorded mention back.',
      },
    },
  }),
]);

export default {
  capability_id: 'creators_network_language',
  route: '#/creator/' + HANDLE,
  entries: ENTRIES,
  fixtures: (state) => ({[ENDPOINT]: {
    populated: () => profile(),
    loading: () => ({__hold: true}),
    failed: () => UNCODED_SERVER_FAILURE,
  }[state]()}),
  gaps: {
    empty: 'creator.py returns 404 for a handle with no rows in its window instead of a measured zero, so the route cannot show an empty profile.',
    thin: 'creator.py carries no evidence floor for a contributor, so no thin state can be produced.',
    stale: 'creator.py states a rolling 30 day window but no freshness stamp or age, so no stale state can be produced.',
  },
};
