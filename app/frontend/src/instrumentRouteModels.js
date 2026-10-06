import {
  buildRibbonModel,
  recommendationAllowed,
} from 'ogilvy-intelligence-design-system';

import {readerWord, regionLabel} from './model.js';
import {platformLabel} from './ui/PlatformGlyph.jsx';

const SECTION_COPY = Object.freeze({
  whyNow: 'No authored Why Now is available.',
  rival: 'No authored rival is available.',
  contradiction: 'No authored contradiction is available.',
  transferLimit: 'No authored transfer limit is available.',
  culturalTension: 'No authored cultural tension is available.',
  possibleResponse: 'No authored possible response is available.',
  whatWouldChangeMyMind: 'No authored What Would Change My Mind is available.',
});

function section(value, fallback){
  return typeof value === 'string' && value.trim()
    ? {state: 'ready', text: value.trim()}
    : {state: 'unavailable', text: fallback};
}

function precedent(value){
  if (!value || typeof value !== 'object' || typeof value.match !== 'string' || !value.match.trim()){
    return {state: 'unavailable'};
  }
  return {
    state: 'ready',
    match: value.match.trim(),
    ...(typeof value.difference === 'string' && value.difference.trim()
      ? {difference: value.difference.trim()}
      : {}),
  };
}

export function packageBriefingModel(adapted){
  if (!adapted || adapted.state === 'unavailable') return null;
  const ribbonModel = buildRibbonModel({
    summary: adapted.evidenceSummary,
    series: adapted.ribbonSeries,
  });
  const possibleResponse = section(adapted.possibleResponse, SECTION_COPY.possibleResponse);
  return {
    state: adapted.evidenceSummary.state,
    signalId: adapted.id,
    title: adapted.title,
    whyNow: section(adapted.whyNow, SECTION_COPY.whyNow),
    rival: section(adapted.rival, SECTION_COPY.rival),
    contradiction: section(adapted.contradiction, SECTION_COPY.contradiction),
    precedent: precedent(adapted.precedent),
    transferLimit: section(adapted.transferLimit, SECTION_COPY.transferLimit),
    culturalTension: section(adapted.culturalTension, SECTION_COPY.culturalTension),
    possibleResponse,
    whatWouldChangeMyMind: section(adapted.whatWouldChangeMyMind, SECTION_COPY.whatWouldChangeMyMind),
    evidenceSummary: adapted.evidenceSummary,
    ribbonModel,
    audience: adapted.audienceBasis,
    recommendationAllowed: recommendationAllowed(adapted.evidenceSummary)
      && possibleResponse.state === 'ready'
      && ribbonModel.state === 'ready',
    limitations: [...new Set([
      ...(adapted.evidenceSummary.limitations || []),
      ...(ribbonModel.limitations || []),
      ...(adapted.audienceBasis.limitations || []),
    ])],
  };
}

/* The route state frame.

   Pulse, Discover and Compare each hold, withhold or fail, and every one of
   those branches renders the package StateView through this one mapping.
   Since 2.0.6 the package words its own held frames: the title, the sentence
   and the written next action of the loading, stale, unavailable and
   filtered-empty cards are its, and the stale sentence carries the age of the
   stamp the route passes. A route passes structure only: the state, the
   checked timestamp, a reason code, the missing field names, the run window
   and method the desk rows carry, and its controls. A fact the payload lacks
   is passed as nothing and reads "not supplied" inside Details. */

function runField(row, key){
  if (!row || typeof row !== 'object') return undefined;
  if (row[key] !== undefined && row[key] !== null) return row[key];
  return row.signal && typeof row.signal === 'object' ? row.signal[key] : undefined;
}

function canonicalDate(value){
  const match = typeof value === 'string' && value.match(/^(\d{4}-\d{2}-\d{2})(?:$|T)/);
  if (!match) return null;
  const parsed = new Date(`${match[1]}T00:00:00Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === match[1] ? match[1] : null;
}

/* A stamp already in the contract's form passes through unchanged, so the
   value the route shows is the value the desk sent. Any other parseable
   form, such as the API's +00:00 offset, is written in that form. */
export function canonicalTimestamp(value){
  if (typeof value !== 'string' || !value.trim()) return null;
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/.test(value)) return value;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

/* Every row of one run repeats the run's facts, so a fact is claimed only
   when every row agrees. Two values are two runs, and no window or method is
   stated for two runs. */
function agreedRunField(rows, key){
  const values = new Set(rows
    .map((row) => runField(row, key))
    .filter((value) => typeof value === 'string' && value.trim())
    .map((value) => value.trim()));
  return values.size === 1 ? [...values][0] : null;
}

export function routeRunFacts({topics, freshness} = {}){
  const rows = Array.isArray(topics) ? topics : [];
  const start = canonicalDate(agreedRunField(rows, 'observation_start'));
  const end = canonicalDate(agreedRunField(rows, 'observation_end'));
  const method = agreedRunField(rows, 'observation_method');
  const runId = agreedRunField(rows, 'run_id');
  const stamp = freshness && typeof freshness === 'object'
    ? freshness.checkedAt || freshness.checked_at || freshness.stamp_utc
    : null;
  return {
    window: start && end && start <= end ? {start, end} : null,
    method: method ? {label: runId ? `Run ${runId}` : 'Observation method', value: method} : null,
    checkedAt: canonicalTimestamp(stamp),
  };
}

/* The rows the held frame once laid under its body where the loading frame
   laid its band. 2.0.6 retired the band and the count is inert in the
   package; the routes still name it and the builder still bounds it so the
   loading payload validates. */
export const HELD_FRAME_ROWS = Object.freeze({stale: 2});

/* The package accepts one to five rows. A route that names a count keeps it;
   any other count is three. */
function reservedRows(value){
  return Number.isInteger(value) && value >= 1 && value <= 5 ? value : 3;
}

/* The two frames the package cannot word from the desk payload. Its error
   contract asks for an incident receipt and a retry boundary the desk never
   sends, so a failed read renders as unavailable and carries the ruled error
   copy from here. Its no_discovery contract asks for the run's validated
   authority and its completed source families, which the desk payload does
   not carry either, so a run that admitted nothing renders as empty with the
   ruled no discovery copy, and Compare words its own insufficient frame
   because only it knows the admitted count. */
export const RULED_COPY = Object.freeze({
  error: Object.freeze({title: 'Could not read the run', body: 'The completed run could not be read just now.'}),
  noDiscovery: Object.freeze({
    title: 'No signals this run',
    body: 'The run finished without a signal strong enough to show.',
    nextAction: 'Open Discover',
  }),
  insufficient: (count) => Object.freeze({
    title: 'Not enough to compare',
    body: `The completed run admitted ${count} signal${count === 1 ? '' : 's'}. Comparison needs two.`,
    nextAction: 'Open Discover',
  }),
});

/* The status line an empty state prints under its sentence, and every phrase
   a reader can meet on it.

   Round 17 read a market code on that line across ten cells on listen and
   network, and the routes were passing raw slots straight through: the market
   code, an upper-cased sentiment constant, the category the desk wrote and
   counts shouted in capitals. The sweep saw one of them, because the fixture
   it read had no sentiment and no category filter on, and a shouted phrase
   with a space in it clears every token shape the rule tests while still
   reading as machine output to a person.

   Two things are deliberately not changed here. The line keeps its case,
   because `.es-status` sets no case of its own and prints these phrases in
   the sentence case they are written in, and the fix section 10 asks for is
   the words rather than the case. And the line keeps every reading it
   carried, because section 11 says a state that withholds content still
   shows the window it read and the scope it read over.

   A category is host data rather than a fixed list, so `readerPhrase` names
   the ones the product already names and turns anything else into words. The
   fallback is the part that matters: a category the desk invents tomorrow
   prints in words rather than in code. */
export const EMPTY_STATE_FACTS = Object.freeze({
  market: (market, label) => (String(market) === 'ALL'
    ? 'Across all markets'
    : 'In ' + (label || regionLabel(market))),
  sentiment: (tone) => (tone ? readerPhrase(tone) + ' mentions only' : null),
  category: (category) => (category ? 'From ' + readerPhrase(category) : null),
  mentionsLoaded: (count) => (count ? count + ' mentions loaded' : 'No mentions loaded'),
  linksDrawn: (count) => (count ? count + ' links drawn' : 'No links drawn'),
  topicsOnBoard: (count) => (count === null ? null : count + ' topics on the board'),
  voicesOnBoard: (count) => (count === null ? null : count + ' voices on the board'),
  window: (days) => 'Over the last ' + days + ' days',
});

/* The words for one host-written token. A name the product already carries
   wins, a snake_case or hyphenated token becomes its own words, and a plain
   word is sentence cased so it reads beside the phrases around it. */
export function readerPhrase(token){
  const raw = String(token ?? '').trim();
  if (!raw) return '';
  /* A lower case token reads as readerWord reads it, so a category such as
     "newsletter" or "search_volume" is never named as a platform it merely
     contains. A token the host cased takes a platform name only on that
     platform's exact key, the same rule. */
  if (raw === raw.toLowerCase()) return readerWord(raw);
  const named = platformLabel(raw);
  const key = raw.toLowerCase().replace(/[\s-]+/g, '_');
  if (named && named.toLowerCase().replace(/\s+/g, '_') === key) return named;
  const words = raw.replace(/[_-]+/g, ' ').trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/* The listen line: the market, the two filters where the reader has set them,
   and the count. A filter that is off prints nothing rather than printing the
   word for its own absence. */
export function listenEmptyFacts({market, sentiment, category, loaded = 0}){
  return [
    EMPTY_STATE_FACTS.market(market),
    EMPTY_STATE_FACTS.sentiment(sentiment),
    EMPTY_STATE_FACTS.category(category),
    EMPTY_STATE_FACTS.mentionsLoaded(loaded),
  ].filter(Boolean);
}

/* The network line: the market, the edge count the state is named for, the
   two counts that say why there is nothing to draw, and the window they were
   read over. A count the desk did not send prints nothing. */
export function networkEmptyFacts({market, marketLabel, topics, voices, days = null}){
  return [
    EMPTY_STATE_FACTS.market(market, marketLabel),
    EMPTY_STATE_FACTS.linksDrawn(0),
    EMPTY_STATE_FACTS.topicsOnBoard(Number.isInteger(topics) ? topics : null),
    EMPTY_STATE_FACTS.voicesOnBoard(Number.isInteger(voices) ? voices : null),
    Number.isInteger(days) && days > 0 ? EMPTY_STATE_FACTS.window(days) : null,
  ].filter(Boolean);
}

/* The ruled next action of the error frame. A route that owns a re-read hands
   it over; one that does not reloads the document, the one re-read every
   route can make. */
export function tryAgainAction(onRetry){
  return {
    id: 'try-again',
    label: 'Try again',
    onClick: typeof onRetry === 'function' ? onRetry : () => { globalThis.location?.reload(); },
  };
}

function reasonCode(value, fallback){
  return typeof value === 'string' && value.trim() ? value.trim() : fallback;
}

/* A route that writes its own title or sentence keeps it; one that writes
   neither takes the package's. */
function hostCopy(state){
  return {
    ...(state.title !== undefined ? {title: state.title} : {}),
    ...(state.body !== undefined ? {body: state.body} : {}),
  };
}

const ROUTE_STATE_VARIANTS = Object.freeze({
  loading: (state) => ({
    state: 'loading',
    ...hostCopy(state),
    task: state.task,
    body: state.body === undefined ? null : state.body,
    reservedRows: reservedRows(state.reservedRows),
  }),
  error: (state) => ({
    state: 'unavailable',
    ...RULED_COPY.error,
    ...hostCopy(state),
    /* The code the caller's read recorded; a caller that hands over none
       gets unstated, not a code no read produced. */
    reason: reasonCode(state.code, 'unstated'),
    missing: ['completed_observation'],
  }),
  /* Stale is dated by the freshness stamp. Without a stamp the run cannot be
     dated, and an undated hold is unavailable, not stale. */
  stale: (state, facts) => {
    const checkedAt = canonicalTimestamp(state.checkedAt) || facts.checkedAt;
    return checkedAt
      ? {state: 'stale', ...hostCopy(state), checkedAt, reason: reasonCode(state.reason, 'freshness_amber')}
      : {state: 'unavailable', ...hostCopy(state), reason: 'checked_at_missing', missing: ['checked_at']};
  },
  unavailable: (state) => ({
    state: 'unavailable',
    ...hostCopy(state),
    reason: reasonCode(state.reason, 'closed_evidence_missing'),
    missing: Array.isArray(state.missing) ? state.missing : [],
  }),
  empty: (state) => ({
    state: 'empty',
    ...hostCopy(state),
    ...(state.nextAction !== undefined ? {nextAction: state.nextAction} : {}),
  }),
});

export function routeStateView(state, facts = routeRunFacts()){
  if (!state || typeof state !== 'object') return {state: 'unavailable', actions: []};
  const variant = ROUTE_STATE_VARIANTS[state.state];
  const mapped = variant ? variant(state, facts) : {state: state.state, ...hostCopy(state)};
  return {
    ...mapped,
    actions: Array.isArray(state.actions) ? state.actions : [],
    ...(facts.window ? {window: facts.window} : {}),
    ...(facts.method ? {method: facts.method} : {}),
  };
}
