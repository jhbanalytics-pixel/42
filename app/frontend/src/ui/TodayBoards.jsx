import {useId, useState} from 'react';
import {boardTitle} from '../readerUnits.js';
import {platformWord} from './TrendCard.jsx';
import {PlatformLogo, platformId} from './PlatformLogo.jsx';
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
const nameOf = (chart) => [chart.word, chart.list].filter(Boolean).join(' ');

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
  for (const row of rows) row.tied = row.rank !== null && rows.filter((other) => other.rank === row.rank).length > 1;
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

const STATE_WORDS = {
  empty: 'Nothing was on this list today.',
  missing: 'This list did not come through today.',
  unreadable: "None of this list's entries had a readable name today.",
  invalid: 'This list could not be read.',
};

function Row({chart, row, where, found}){
  const music = MUSIC.has(chart.id);
  const {name, artists} = splitTitle(row.full, music);
  const charts = row.itemId ? found.get(row.itemId) : null;
  const count = charts ? charts.size : 0;
  const others = charts ? [...charts].filter(([key]) => key !== chart.key).map(([, label]) => label) : [];
  const rankWords = row.rank === null ? 'Unranked' : (row.tied ? 'Tied rank ' : 'Rank ') + row.rank;
  return (
    <li className="tb-row" data-board-row="" data-row-id={`${chart.id}|${chart.list}|${row.rank ?? ''}|${row.itemId || row.full}`}>
      <span className="tb-rank" aria-hidden="true">{row.rank === null ? '-' : (row.tied ? '=' : '') + row.rank}</span>
      <span className="tb-main">
        <span className="sr-only">{rankWords}. </span>
        <span className="tb-title">{name}</span>
        {artists && <span className="tb-artists">{artists}</span>}
        {count > 1 && <span className="tb-multi" title={others.length ? 'Also on ' + others.join('; ') : undefined}>On {count} charts</span>}
        <span className="sr-only">. {where}</span>
      </span>
    </li>
  );
}

function ChartCard({chart, found, uid, tag: Heading}){
  const [open, setOpen] = useState(false);
  const base = `tb-${uid}-${chart.index}`;
  const headId = base + '-h';
  const rowsId = base + '-rows';
  const where = chart.invalid ? '' : nameOf(chart);
  const shown = open ? chart.rows : chart.rows.slice(0, TOP);
  const more = chart.rows.length > TOP;
  const flagged = chart.state === 'invalid';
  return (
    <section className="tb-card" data-board-card="" data-chart-state={chart.state} data-platform={chart.id || undefined} aria-labelledby={headId}>
      <div className="tb-card-head">
        <span className="tb-logo"><PlatformLogo platform={chart.id} size={22} /></span>
        <Heading className="tb-card-title" id={headId}>
          <span className="tb-platform fact-unit">{flagged ? 'Unknown source' : chart.word || 'Unnamed source'}</span>
          {chart.list && <>{' '}<span className="tb-list fact-unit">{chart.list}</span></>}
        </Heading>
      </div>
      {chart.state === 'ok'
        ? <>
            <p className="tb-caption">Best rank today</p>
            <ul className="tb-rows" id={rowsId}>
              {shown.map((row, at) => <Row key={at} chart={chart} row={row} where={where} found={found} />)}
            </ul>
            {more && (
              <button type="button" className="tb-more" aria-expanded={open} aria-controls={rowsId}
                aria-label={(open ? 'Show fewer entries on ' : `Show all ${chart.rows.length} entries on `) + where}
                onClick={() => setOpen(!open)}>
                {open ? 'Show fewer' : `Show all ${chart.rows.length}`}
              </button>
            )}
          </>
        : !(chart.leftOut > 0 && sentence(chart.reason) === sentence(STATE_WORDS[chart.state])) && <p className="tb-state">{STATE_WORDS[chart.state]}</p>}
      {chart.leftOut > 0 && <p className="tb-left-out">{chart.leftOut} left out: {chart.reason || 'No readable name'}</p>}
    </section>
  );
}

/* The cards of one market, grouped, with each chart's rows counted against the
   market's own payload. level is the heading level of the group titles. */
function Charts({boards, uid, level}){
  const found = chartIndex(boards);
  const charts = (Array.isArray(boards) ? boards : []).map((raw, index) => {
    const chart = describe(raw, index);
    chart.invalid = !chart.valid;
    return chart;
  });
  const GroupTitle = 'h' + level;
  const CardTitle = 'h' + (level + 1);
  return GROUPS.map((group) => {
    const members = charts.filter((chart) => groupOf(chart.id) === group.key);
    if (members.length === 0) return null;
    const titleId = `tb-${uid}-${group.key}`;
    return (
      <div key={group.key} className="tb-group" data-board-group={group.key} role="group" aria-labelledby={titleId}>
        <GroupTitle className="tb-group-title" id={titleId}>{group.label}{' '}<span className="tb-count">{members.length}</span></GroupTitle>
        <ul className="tb-grid">
          {members.map((chart) => <li key={chart.index}><ChartCard chart={chart} found={found} uid={uid} tag={CardTitle} /></li>)}
        </ul>
      </div>
    );
  });
}

const NONE = <p className="t42-line-text">No platform lists were read today.</p>;

/* boards: one market's payload. groups: [{market, label, boards}] for the All
   view, each market kept apart under its own badge. */
export function TodayBoards({boards, groups}){
  const uid = safe(useId());
  const markets = Array.isArray(groups) ? groups.filter(isObject) : null;
  const list = Array.isArray(boards) ? boards : [];
  const any = markets ? markets.some((m) => Array.isArray(m.boards) && m.boards.length > 0) : list.length > 0;
  const sectionId = `tb-${uid}-section`;
  return (
    <section className="t42-section tb" data-section="boards" aria-labelledby={sectionId}>
      <h3 className="t42-section-title" id={sectionId}>On the boards today</h3>
      {any && <p className="tb-note">Each card is one platform's own list. A rank belongs to that list only{markets ? ', and charts are counted within their market' : ''}.</p>}
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
                {items.length > 0 ? <Charts boards={items} uid={`${uid}-m${at}`} level={5} /> : NONE}
              </div>
            );
          })
        : list.length > 0 ? <Charts boards={list} uid={uid} level={4} /> : NONE}
    </section>
  );
}
