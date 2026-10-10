import {useId, useState} from 'react';
import {boardTitle} from '../readerUnits.js';
import {platformWord} from './TrendCard.jsx';
import {PlatformLogo, brandOf, brandStyle, platformId} from './PlatformLogo.jsx';
import {SnapBand} from './SnapBand.jsx';
import '../styles/today-boards.css';

/* On the boards today: one card per platform list, in three groups. The
   counting contract (wave 8 plan, section 4):
   - a chart is one (platform, list) entry of a market's boards payload for
     the brief date;
   - a rank belongs to its own chart and is never merged across charts;
   - "On N charts" counts the distinct charts of the same market and date that
     hold the same item_id, each chart once however often it repeats the item,
     counted before any filter (an unreadable title or a null rank still counts);
   - an item with no item_id has no marker, and the same item_id in another
     market is a separate item.
   Row order is the payload's. */

const TOP = 5;
const MUSIC = new Set(['apple_music', 'spotify', 'shazam', 'boomplay', 'audiomack', 'turntable', 'mdundo']);
const APPS = new Set(['app_store', 'google_play']);
const GROUPS = [
  {key: 'music', label: 'Music charts'},
  {key: 'apps', label: 'Apps'},
  {key: 'social', label: 'Social boards'},
];
/* Words the shared platform names do not carry, as the backend writes them. */
const WORDS = {turntable: 'TurnTable'};

/* f42-api leaves out board entries titled with an id; this catches any that
   still arrive, so an id is never shown as a name (contract.md section 4). */
const ID_TITLE = /^(uc[a-z0-9_-]{22}|t2_[a-z0-9]+|[0-9a-f]{64})$/i;
export const readable = (title) => typeof title === 'string' && boardTitle(title) !== '' && !ID_TITLE.test(title.trim());

const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const itemIdOf = (entry) => (isObject(entry) && typeof entry.item_id === 'string' && entry.item_id.trim() !== '' ? entry.item_id : null);
const validRank = (rank) => (typeof rank === 'number' && Number.isFinite(rank) && rank > 0 ? rank : null);
const groupOf = (id) => (MUSIC.has(id) ? 'music' : APPS.has(id) ? 'apps' : 'social');
const wordFor = (id) => WORDS[id] || (id ? platformWord(id) : '');
/* The server names a list in its own words and they start with the platform
   ("Spotify daily chart"), so the platform word is left out when the list
   already begins with it. */
const startsWithWord = (word, list) => {
  const w = word.toLowerCase();
  const l = list.toLowerCase();
  return w !== '' && l.startsWith(w) && !/[a-z0-9]/.test(l.charAt(w.length));
};
const nameOf = (chart) => (startsWithWord(chart.word, chart.list) ? chart.list : [chart.word, chart.list].filter(Boolean).join(' '));

/* A chart's identity for counting. */
const chartKey = (platform, list) => platform + '\u0000' + list;

function describe(raw, index){
  if (!isObject(raw)) return {index, valid: false, id: '', word: '', list: '', key: 'invalid' + index, state: 'invalid', rows: [], leftOut: 0, reason: null};
  const id = platformId(raw.platform);
  const list = typeof raw.list === 'string' ? raw.list.trim() : '';
  const entries = Array.isArray(raw.entries) ? raw.entries : null;
  const base = {index, valid: true, id, word: wordFor(id), list, key: chartKey(id, list), raw};
  const declared = Math.max(0, Math.trunc(Number(raw.left_out)) || 0);
  if (!entries) return {...base, state: 'missing', rows: [], leftOut: declared, reason: raw.left_out_reason || null};
  const shown = entries.filter((entry) => isObject(entry) && readable(entry.title));
  const rows = shown.map((entry) => ({
    rank: validRank(entry.rank),
    full: boardTitle(entry.title),
    itemId: itemIdOf(entry),
  }));
  rows.forEach((row, at) => {
    row.tied = row.rank !== null && rows.filter((other) => other.rank === row.rank).length > 1;
    row.tiedAfter = row.tied && rows.findIndex((other) => other.rank === row.rank) < at;
  });
  const leftOut = declared + entries.length - shown.length;
  const state = rows.length > 0 ? 'ok' : entries.length === 0 && declared === 0 ? 'empty' : 'unreadable';
  return {...base, state, rows, leftOut, reason: raw.left_out_reason || null};
}

/* item_id to the distinct charts that hold it, for one market's boards. */
function chartIndex(boards){
  const found = new Map();
  for (const raw of Array.isArray(boards) ? boards : []){
    if (!isObject(raw) || !Array.isArray(raw.entries)) continue;
    const id = platformId(raw.platform);
    const list = typeof raw.list === 'string' ? raw.list.trim() : '';
    const key = chartKey(id, list);
    const label = nameOf({word: wordFor(id), list});
    for (const entry of raw.entries){
      const itemId = itemIdOf(entry);
      if (!itemId) continue;
      if (!found.has(itemId)) found.set(itemId, new Map());
      found.get(itemId).set(key, label);
    }
  }
  return found;
}

export function chartCounts(boards){
  const counts = new Map();
  for (const [itemId, charts] of chartIndex(boards)) counts.set(itemId, charts.size);
  return counts;
}

/* A music chart's "Song by Artist" (or the source's line break, read as
   "Song · Artist") reads as the song, then its artists. Other boards keep the
   whole title: a hashtag that happens to contain "by" is not an artist. */
function splitTitle(full, music){
  if (!music) return {name: full, artists: ''};
  const bys = [...full.matchAll(/\s+by\s+/gi)];
  const at = bys.length ? bys[bys.length - 1] : null;
  if (at && at.index > 0 && at.index + at[0].length < full.length){
    return {name: full.slice(0, at.index).trim(), artists: full.slice(at.index + at[0].length).trim()};
  }
  const dot = full.indexOf(' · ');
  if (dot > 0 && dot + 3 < full.length) return {name: full.slice(0, dot).trim(), artists: full.slice(dot + 3).trim()};
  return {name: full, artists: ''};
}

/* The server's reason for an unreadable chart is the state line's own
   sentence, so the two are compared by their words, not their full stop. */
const sentence = (text) => String(text || '').trim().replace(/[.\s]+$/, '').toLowerCase();

const safe = (id) => id.replace(/[^a-zA-Z0-9_-]/g, '');

/* day is the brief day's own words: "today", or "on 30 September 2026" for a
   past brief. */
const stateWords = (day) => ({
  empty: `Nothing was on this list ${day}.`,
  missing: `This list did not come through ${day}.`,
  unreadable: `None of this list's entries had a readable name ${day}.`,
  invalid: 'This list could not be read.',
});
const dayWords = (day) => (typeof day === 'string' && day.trim() !== '' ? day.trim() : 'today');

function Row({chart, row, where, found, hero}){
  const music = MUSIC.has(chart.id);
  const {name, artists} = splitTitle(row.full, music);
  const charts = row.itemId ? found.get(row.itemId) : null;
  const count = charts ? charts.size : 0;
  const others = charts ? [...charts].filter(([key]) => key !== chart.key).map(([, label]) => label) : [];
  const rankWords = row.rank === null ? 'Unranked' : (row.tied ? 'Tied rank ' : 'Rank ') + row.rank;
  return (
    <li className="tb-row" data-board-row="" data-hero={hero ? '' : undefined} data-row-id={`${chart.id}|${chart.list}|${row.rank ?? ''}|${row.itemId || row.full}`}>
      <span className={row.tiedAfter ? 'tb-rank tb-rank-tied' : 'tb-rank'} aria-hidden="true">{row.rank === null ? '-' : row.tiedAfter ? 'tied' : row.rank}</span>
      <span className="tb-main">
        <span className="sr-only">{rankWords}. </span>
        <span className="tb-title">{name}</span>
        {artists && <span className="tb-artists">{artists}</span>}
        {count > 1 && <span className="tb-multi">On {count} charts</span>}
        {others.length > 0 && <span className="tb-also">Also on {others.join('; ')}</span>}
        <span className="sr-only">. {where}</span>
      </span>
    </li>
  );
}

function ChartCard({chart, found, uid, day, tag: Heading}){
  const [open, setOpen] = useState(false);
  const base = `tb-${uid}-${chart.index}`;
  const headId = base + '-h';
  const rowsId = base + '-rows';
  const where = chart.invalid ? '' : nameOf(chart);
  const named = Boolean(chart.list) && startsWithWord(chart.word, chart.list);
  const shown = open ? chart.rows : chart.rows.slice(0, TOP);
  const more = chart.rows.length > TOP;
  const flagged = chart.state === 'invalid';
  const brand = chart.invalid ? null : brandOf(chart.id);
  const words = stateWords(day)[chart.state];
  /* The server words an all-ids chart's reason as "today" whatever the day, so
     that sentence is shown in the brief day's words instead. Only a chart with
     no readable row can carry it, which keeps a null reason from matching. */
  const saidByReason = chart.state !== 'ok' && chart.leftOut > 0 && [words, stateWords('today')[chart.state]].some((text) => sentence(text) === sentence(chart.reason));
  return (
    <section className="tb-card" style={brand ? brandStyle(brand) : undefined} data-board-card="" data-chart-state={chart.state} data-platform={chart.id || undefined} aria-labelledby={headId}>
      <div className="tb-card-head">
        <span className="tb-logo"><PlatformLogo platform={chart.id} size={28} /></span>
        <Heading className="tb-card-title" id={headId}>
          {named
            ? <span className="tb-list tb-list-named fact-unit">{chart.list}</span>
            : <>
                <span className="tb-platform fact-unit">{flagged ? 'Unknown source' : chart.word || 'Unnamed source'}</span>
                {chart.list && <>{' '}<span className="tb-list fact-unit">{chart.list}</span></>}
              </>}
        </Heading>
      </div>
      {chart.state === 'ok'
        ? <>
            <p className="tb-caption">Best rank {day}</p>
            <ul className={open ? 'tb-rows tb-rows-open' : 'tb-rows'} id={rowsId}>
              {shown.map((row, at) => <Row key={at} chart={chart} row={row} where={where} found={found} hero={at === 0 && row.rank === 1} />)}
            </ul>
            {more && (
              <button type="button" className="tb-more" aria-expanded={open} aria-controls={rowsId}
                aria-label={(open ? 'Show fewer entries on ' : `Show all ${chart.rows.length} entries on `) + where}
                onClick={() => setOpen(!open)}>
                {open ? 'Show fewer' : `Show all ${chart.rows.length}`}
                <svg className="tb-more-mark" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" focusable="false"><path d="M5 9l7 7 7-7" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="square" /></svg>
              </button>
            )}
          </>
        : !saidByReason && <p className="tb-state">{words}</p>}
      {chart.leftOut > 0 && <p className="tb-left-out">{chart.leftOut} left out: {saidByReason ? words.replace(/[.]$/, '') : chart.reason || 'No readable name'}</p>}
    </section>
  );
}

function Band({label, children}){
  return (
    <SnapBand label={label} prevLabel="Show earlier platform lists" nextLabel="Show later platform lists" itemSelector=".tb-grid > li">
      {children}
    </SnapBand>
  );
}

/* The cards of one market, grouped, with each chart's rows counted against the
   market's own payload. level is the heading level of the group titles. */
function Charts({boards, uid, level, day, label}){
  const found = chartIndex(boards);
  const charts = (Array.isArray(boards) ? boards : []).map((raw, index) => {
    const chart = describe(raw, index);
    chart.invalid = !chart.valid;
    return chart;
  });
  const GroupTitle = 'h' + level;
  const CardTitle = 'h' + (level + 1);
  const groups = GROUPS.map((group) => {
    const members = charts.filter((chart) => groupOf(chart.id) === group.key);
    if (members.length === 0) return null;
    const titleId = `tb-${uid}-${group.key}`;
    return (
      <div key={group.key} className="tb-group" data-board-group={group.key} role="group" aria-labelledby={titleId}>
        <GroupTitle className="tb-group-title" id={titleId}>{group.label}{' '}<span className="tb-count">{members.length}</span></GroupTitle>
        <ul className="tb-grid">
          {members.map((chart) => <li key={chart.index}><ChartCard chart={chart} found={found} uid={uid} day={day} tag={CardTitle} /></li>)}
        </ul>
      </div>
    );
  });
  return <Band label={label}>{groups}</Band>;
}

const none = (day) => <p className="t42-line-text">No platform lists were read {day}.</p>;

/* boards: one market's payload. groups: [{market, label, boards}] for the All
   view, each market kept apart under its own badge. day: the brief day's
   words, "today" or "on 30 September 2026". */
export function TodayBoards({boards, groups, day: given}){
  const day = dayWords(given);
  const uid = safe(useId());
  const markets = Array.isArray(groups) ? groups.filter(isObject) : null;
  const list = Array.isArray(boards) ? boards : [];
  const any = markets ? markets.some((m) => Array.isArray(m.boards) && m.boards.length > 0) : list.length > 0;
  const sectionId = `tb-${uid}-section`;
  return (
    <section className="t42-section tb" data-section="boards" aria-labelledby={sectionId}>
      <h3 className="t42-section-title" id={sectionId}>On the boards {day}</h3>
      {any && <p className="tb-note">{`Each card is one platform's own list, and a rank belongs to that list only. "On N charts" counts the lists in the same market ${day}.`}</p>}
      {markets
        ? markets.map((m, at) => {
            const nameId = `tb-${uid}-m${at}`;
            const items = Array.isArray(m.boards) ? m.boards : [];
            return (
              <div key={m.market || at} className="tb-market" data-board-market={m.market} role="group" aria-labelledby={nameId}>
                <h4 className="tb-market-head">
                  <span className="tb-market-badge">{m.market}</span>
                  <span className="tb-market-name" id={nameId}>{m.label || m.market}</span>
                </h4>
                {items.length > 0 ? <Charts boards={items} uid={`${uid}-m${at}`} level={5} day={day} label={`Platform lists for ${m.label || m.market}`} /> : none(day)}
              </div>
            );
          })
        : list.length > 0 ? <Charts boards={list} uid={uid} level={4} day={day} label="Platform lists" /> : none(day)}
    </section>
  );
}
