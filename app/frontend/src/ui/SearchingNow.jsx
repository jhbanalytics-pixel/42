import {REGION_NAME} from '../model.js';
import '../styles/searching-now.css';

const MARKETS = ['ZA', 'NG', 'KE'];
const FIELDS = new Set(['term', 'market', 'source', 'rank', 'refreshed_at']);
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;
const UTC_TIMESTAMP = /^(\d{4}-\d{2}-\d{2})T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?Z$/;

/* Each Google source the API may send: whether it is the live list or a
   daily one, the word a row shows, the full name in its title, and the clause
   the header uses for its freshness. Live sorts before daily. */
const SOURCES = {
  google_trending: {live: true, word: 'live', title: 'Google live trending searches', clause: 'Live trending', order: 0},
  google_bq: {live: false, word: 'daily', title: 'Google daily top and rising terms', clause: 'Daily top terms', order: 1},
  google_rss: {live: false, word: 'daily', title: 'Google Trends daily feed', clause: 'Daily feed', order: 2},
};

/* A term that names its own market reads as local: country name or demonym. */
const LOCAL_WORDS = {
  ZA: /\bsouth africa(?:n|ns)?\b/i,
  NG: /\bnigeria(?:n|ns)?\b/i,
  KE: /\bkenya(?:n|ns)?\b/i,
};
const MATCH = / vs\.? /i;

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const SAST_OFFSET = 2 * 60 * 60 * 1000;
const MINUTE = 60 * 1000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

function validDate(value){
  if (!DATE_ONLY.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
}

function validRefresh(value){
  if (typeof value !== 'string') return false;
  if (DATE_ONLY.test(value)) return validDate(value);
  const match = UTC_TIMESTAMP.exec(value);
  return Boolean(match && validDate(match[1]) && Number.isFinite(Date.parse(value)));
}

function validSignal(signal){
  if (!signal || typeof signal !== 'object' || Array.isArray(signal)) return false;
  const fields = Object.keys(signal);
  return fields.length === FIELDS.size
    && fields.every((field) => FIELDS.has(field))
    && typeof signal.term === 'string'
    && Boolean(signal.term.trim())
    && MARKETS.includes(signal.market)
    && Object.prototype.hasOwnProperty.call(SOURCES, signal.source)
    && (signal.rank === null || (Number.isInteger(signal.rank) && signal.rank > 0))
    && validRefresh(signal.refreshed_at);
}

/* Rank first (unranked last), live before daily, then the term. */
function compareSignals(left, right){
  if (left.rank === null && right.rank !== null) return 1;
  if (left.rank !== null && right.rank === null) return -1;
  if (left.rank !== right.rank) return left.rank - right.rank;
  const order = SOURCES[left.source].order - SOURCES[right.source].order;
  if (order) return order;
  for (const field of ['term', 'refreshed_at']){
    if (left[field] < right[field]) return -1;
    if (left[field] > right[field]) return 1;
  }
  return 0;
}

/* Calendar parts of an instant in South African time (no daylight saving),
   or of a plain day as sent. */
function dayParts(value){
  if (DATE_ONLY.test(value)){
    const [year, month, day] = value.split('-').map(Number);
    return {year, month: month - 1, day};
  }
  const sast = new Date(Date.parse(value) + SAST_OFFSET);
  return {year: sast.getUTCFullYear(), month: sast.getUTCMonth(), day: sast.getUTCDate()};
}

function dayWords(value, now){
  const {year, month, day} = dayParts(value);
  const thisYear = new Date(now + SAST_OFFSET).getUTCFullYear();
  return `${day} ${MONTHS[month]}` + (year === thisYear ? '' : ` ${year}`);
}

function sastDay(ms){
  return Math.floor((ms + SAST_OFFSET) / DAY);
}

/* Today in South African time as a plain day, YYYY-MM-DD. */
function sastToday(now){
  return new Date(now + SAST_OFFSET).toISOString().slice(0, 10);
}

/* How old a refresh reads. A timestamp reads as relative time against now;
   a plain day reads as that day. Exported so tests can hold the clock still. */
export function freshnessWords(value, now = Date.now()){
  if (DATE_ONLY.test(value)) return dayWords(value, now);
  const at = Date.parse(value);
  const age = now - at;
  if (age < MINUTE) return 'just now';
  if (age < HOUR) return `${Math.floor(age / MINUTE)} min ago`;
  if (age < DAY) return `${Math.floor(age / HOUR)} h ago`;
  if (sastDay(now) - sastDay(at) === 1) return 'yesterday';
  return dayWords(value, now);
}

function refreshMs(value){
  return Date.parse(DATE_ONLY.test(value) ? `${value}T00:00:00Z` : value);
}

/* The one red on the block: a live row refreshed today. The API sends
   refreshed_at as a plain day (the SAST day of the fetch), so a plain day is
   fresh when it is today in South African time; a timestamp, still accepted,
   is fresh within the last hour. Exported so tests can hold the clock still. */
export function isFresh(signal, now = Date.now()){
  if (!SOURCES[signal.source].live) return false;
  if (DATE_ONLY.test(signal.refreshed_at)) return signal.refreshed_at === sastToday(now);
  const age = now - Date.parse(signal.refreshed_at);
  return age >= -MINUTE && age < HOUR;
}

/* "Live trending, 3 h ago · Daily top terms, 1 Oct": the newest refresh per
   source among the rows shown; a source with no rows says nothing. */
function freshnessLine(rows, now){
  const newest = new Map();
  for (const row of rows){
    const seen = newest.get(row.source);
    if (!seen || refreshMs(row.refreshed_at) > refreshMs(seen)) newest.set(row.source, row.refreshed_at);
  }
  return Object.keys(SOURCES)
    .filter((source) => newest.has(source))
    .map((source) => `${SOURCES[source].clause}, ${freshnessWords(newest.get(source), now)}`);
}

/* Fixtures ("x vs y") and everything else; nothing is left out, so the two
   counts always add up to the rows shown. */
function termGroups(signals){
  return [
    {key: 'matches', label: 'Matches', signals: signals.filter((signal) => MATCH.test(signal.term))},
    {key: 'other', label: 'Other searches', signals: signals.filter((signal) => !MATCH.test(signal.term))},
  ].filter((group) => group.signals.length);
}

function Row({signal, index, now}){
  const source = SOURCES[signal.source];
  const top = signal.rank === 1;
  const mark = 'searching-now__mark'
    + (source.live ? ' searching-now__mark--live' : '')
    + (isFresh(signal, now) ? ' searching-now__mark--fresh' : '');
  return (
    <li className={'searching-now__row' + (top ? ' searching-now__row--top' : '')} data-search-source={source.word}
      style={{'--sn-i': index}}>
      <span className="searching-now__rank">
        {signal.rank === null
          ? <span className="sr-only">unranked</span>
          : <><span className="sr-only">Rank </span>{signal.rank}</>}
      </span>
      <span className="searching-now__term">
        {signal.term}
        {LOCAL_WORDS[signal.market].test(signal.term) && <span className="searching-now__local"> local</span>}
      </span>
      <span className="searching-now__meta">
        <span className="searching-now__source" title={source.title}>
          <span className={mark} aria-hidden="true" />
          {source.word}
          <span className="sr-only">, {source.title}, </span>
        </span>
        <time dateTime={signal.refreshed_at}>{freshnessWords(signal.refreshed_at, now)}</time>
      </span>
    </li>
  );
}

/* Searching now: a ranked league table of what a market types into Google,
   set as type with no boxes. Rank, term, then which Google list and how
   fresh; tied ranks repeat so ties read as ties. nameMarket is false where a
   heading above already names the market. now is injectable for tests. */
export function SearchingNow({signals, market, nameMarket = true, now}){
  if (market !== 'ALL' && !MARKETS.includes(market)) return null;

  const rows = (Array.isArray(signals) ? signals : [])
    .filter(validSignal)
    .filter((signal) => market === 'ALL' || signal.market === market);
  if (!rows.length) return null;

  const clock = Number.isFinite(now) ? now : Date.now();
  const markets = MARKETS
    .map((code) => ({market: code, signals: rows.filter((signal) => signal.market === code).sort(compareSignals)}))
    .filter((group) => group.signals.length);
  const single = market !== 'ALL';
  let index = 0;

  return (
    <section className="searching-now" data-section="searching-now" aria-label="Searching now">
      <header className="searching-now__header">
        <h2 className="searching-now__heading">
          Searching now
          {single && nameMarket && <span className="searching-now__place"> · {REGION_NAME[market]}</span>}
        </h2>
        <p className="searching-now__caption">Google search interest, not posts</p>
        <p className="searching-now__fresh">
          {freshnessLine(rows, clock).map((clause, at) => (
            <span key={clause}>{at > 0 ? ' · ' : ''}<span className="searching-now__clause">{clause}</span></span>
          ))}
        </p>
      </header>
      {markets.map((group) => (
        <div className="searching-now__market" data-search-market={group.market} key={group.market} role="group" aria-label={`Search interest for ${REGION_NAME[group.market]}`}>
          {!single && <h3 className="searching-now__market-name">{REGION_NAME[group.market]}</h3>}
          {termGroups(group.signals).map((terms) => {
            const labelId = `searching-now-${group.market}-${terms.key}`;
            return (
              <div className="searching-now__group" data-search-group={terms.key} key={terms.key}>
                <p className="searching-now__group-label" id={labelId}>{terms.label} · {terms.signals.length}</p>
                <ol className="searching-now__list" aria-labelledby={labelId}>
                  {terms.signals.map((signal) => {
                    const at = index++;
                    return <Row key={`${signal.term}:${signal.source}:${signal.refreshed_at}:${signal.rank ?? 'unranked'}:${at}`} signal={signal} index={at} now={clock} />;
                  })}
                </ol>
              </div>
            );
          })}
        </div>
      ))}
    </section>
  );
}
