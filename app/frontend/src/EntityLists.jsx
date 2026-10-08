import React from 'react';
import {readerFigure} from './api.js';
import {unitFor} from './readerUnits.js';
import {platformLabel} from './ui/PlatformGlyph.jsx';

const MARKETS = {ZA: 'South Africa', NG: 'Nigeria', KE: 'Kenya'};
const PLATFORMS = new Set(['tiktok', 'instagram', 'youtube', 'facebook', 'reddit', 'threads', 'x']);
const HASH = /^sha256:[0-9a-f]{64}$/;
const DAY = 86400000;
const KINDS = {
  platforms: ['retained_store_platform_claims', 'posts_desc_creators_desc', 'creators', 'posts'],
  topics: ['retained_detection_snapshot_claims', 'creators_desc_posts_desc', 'creators3', 'posts3'],
  creators: ['retained_creator_identities', null, null, null],
};
const SUBJECT = /^\s*(?:which|what)\s+(?:(?:top|rising|new|popular|active|busy|busiest|trending|south\s+african|nigerian|kenyan|tiktok|instagram|youtube|facebook|reddit|threads|x)\s+)*(platforms?|topics?|creators?)\b(?=\s*(?:[?.!,;:]|$)|\s+(?:are|is|were|have|has|do|does|on|in|from|across|during|this|last|today|rose|grew|fell|posting|post|use|used)\b)/i;
const PRIOR_UNITS = new Set(['posts', 'posts3', 'posts in previous week', 'posts in the previous week', 'posts in the same three days last week']);
const object = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const asciiFold = (text) => text.replace(/[A-Z]/g, (char) => char.toLowerCase());
const platform = (value) => value === 'twitter' ? 'x' : PLATFORMS.has(value) ? value : null;

function named(text, name){
  text = asciiFold(String(text || ''));
  const pattern = asciiFold(name).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const continuation = (char) => /[\p{L}\p{N}\p{M}_#]/u.test(char || '');
  return [...text.matchAll(new RegExp(pattern, 'gu'))].some((match) => (
    !continuation(Array.from(text.slice(0, match.index)).at(-1))
      && !continuation(Array.from(text.slice(match.index + match[0].length))[0])
  ));
}

function creatorName(text, handle){
  if (!handle || /[\s@#]/u.test(handle)) return null;
  text = asciiFold(String(text || ''));
  const pattern = asciiFold(handle).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const part = (char) => /[\p{L}\p{N}\p{M}_-]/u.test(char || '');
  for (const match of text.matchAll(new RegExp('@?' + pattern, 'gu'))){
    const before = Array.from(text.slice(0, match.index)).at(-1);
    const after = Array.from(text.slice(match.index + match[0].length).replace(/^\.+/, ''))[0];
    if (part(before) || ['.', '@', '#'].includes(before) || part(after) || ['@', '#'].includes(after)) continue;
    return (match[0].startsWith('@') ? '@' : '') + handle;
  }
  return null;
}

function bounds(window){
  if (!object(window)) return null;
  const dates = [window.from, window.to].map((text) => {
    if (typeof text !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(text) || text.startsWith('0000-')) return null;
    const value = Date.parse(text + 'T00:00:00Z');
    return Number.isFinite(value) && new Date(value).toISOString().slice(0, 10) === text ? value : null;
  });
  return dates.every((value) => value !== null) && dates[0] <= dates[1] ? dates : null;
}

function checked(group, kind, record, claims, evidence){
  const shape = KINDS[kind];
  const window = bounds(group?.window);
  const askWindow = bounds(record.run?.window);
  if (!object(group) || group.kind !== kind || group.scope !== shape[0] || group.order !== shape[1]
      || group.measure_columns?.creators !== shape[2] || group.measure_columns?.posts !== shape[3]
      || (group.market !== null && !MARKETS[group.market]) || (kind === 'topics' && group.market === null)
      || !Array.isArray(group.query_ids) || !group.query_ids.length
      || group.query_ids.some((qid) => typeof qid !== 'string' || !qid)
      || new Set(group.query_ids).size !== group.query_ids.length || !window || !askWindow
      || !object(group.unavailable) || !Array.isArray(group.items) || !group.items.length
      || typeof record.run?.run_id !== 'string' || !record.run.run_id) return null;
  const creator = kind === 'creators';
  if (group.unavailable.creators !== (creator ? 'not_recorded' : null)
      || group.unavailable.posts !== (creator ? 'not_recorded' : null)
      || (kind === 'topics' ? window[1] - window[0] !== 2 * DAY || window[1] < askWindow[0] || window[1] > askWindow[1]
        : group.window.from !== record.run.window?.from || group.window.to !== record.run.window?.to)
      || (creator && group.query_ids.length !== 2)) return null;
  const items = [];
  const seen = new Set();
  let sourceHash = null;
  let hasPrior = false;
  for (const item of group.items){
    if (!object(item) || typeof item.entity_id !== 'string' || !item.entity_id || seen.has(item.entity_id)
        || typeof item.name !== 'string' || !item.name.trim() || !claims.has(item.claim_id)
        || !Array.isArray(item.evidence_ids) || !item.evidence_ids.length
        || new Set(item.evidence_ids).size !== item.evidence_ids.length) return null;
    const claim = claims.get(item.claim_id);
    if ((!creator && !named(claim.text, item.name)) || !Array.isArray(claim.evidence_ids)
        || item.evidence_ids.some((id) => typeof id !== 'string' || !claim.evidence_ids.includes(id) || !evidence.has(id))) return null;
    if (kind === 'topics' ? item.platform !== null : !PLATFORMS.has(item.platform)) return null;
    const sources = item.evidence_ids.map((id) => evidence.get(id));
    if (kind === 'platforms' && (item.entity_id !== item.platform || item.name !== platformLabel(item.platform)
        || sources.some((source) => platform(source.platform) !== item.platform))) return null;
    if (creator && (!item.entity_id.startsWith(item.platform + ':') || item.entity_id === item.platform + ':'
        || sources.some((source) => platform(source.platform) !== item.platform
          || creatorName(claim.text, String(source.handle || '').replace(/^@/, '')) !== item.name))) return null;
    const number = (index) => {
      if (!Number.isInteger(index) || index < 0 || !Array.isArray(claim.numbers)) return null;
      const pin = claim.numbers[index];
      return object(pin) && Number.isFinite(pin.value) && Number.isInteger(pin.value) && pin.value >= 0
        && pin.run_id === record.run.run_id && HASH.test(pin.result_hash) ? pin : null;
    };
    let creators = null;
    let posts = null;
    let previous = null;
    let counts = null;
    if (creator){
      if (item.creators_index !== null || item.posts_index !== null || item.previous_posts_index !== null
          || item.tied_with_previous !== null) return null;
    } else {
      creators = number(item.creators_index);
      posts = number(item.posts_index);
      if (!creators || !posts || !['creators', shape[2]].includes(String(creators.unit).trim().toLowerCase())
          || !['posts', shape[3]].includes(String(posts.unit).trim().toLowerCase())
          || creators.query_id !== group.query_ids[0] || posts.query_id !== group.query_ids[0]
          || creators.result_hash !== posts.result_hash || (sourceHash && creators.result_hash !== sourceHash)) return null;
      sourceHash = creators.result_hash;
      if (item.previous_posts_index !== null){
        previous = number(item.previous_posts_index);
        if (kind !== 'topics' || !previous || !PRIOR_UNITS.has(String(previous.unit).trim().toLowerCase())
            || !group.query_ids.slice(1).includes(previous.query_id)) return null;
        hasPrior = true;
      }
      counts = kind === 'platforms' ? [posts.value, creators.value] : [creators.value, posts.value];
      const last = items.at(-1)?.counts;
      if (last && (counts[0] > last[0] || (counts[0] === last[0] && counts[1] > last[1]))) return null;
      const tied = Boolean(last && counts[0] === last[0] && counts[1] === last[1]);
      if (item.tied_with_previous !== tied) return null;
    }
    items.push({item, claim, creators, posts, previous, counts});
    seen.add(item.entity_id);
  }
  if (hasPrior){
    const previous = bounds(group.previous_window);
    if (kind !== 'topics' || !previous || previous[1] - previous[0] !== 2 * DAY
        || window[0] - previous[0] !== 7 * DAY || window[1] - previous[1] !== 7 * DAY
        || group.previous_basis !== 'same_three_day_period_previous_week' || group.unavailable.previous_week !== null) return null;
  } else if (group.previous_window !== null || group.previous_basis !== null || group.unavailable.previous_week !== 'not_recorded') return null;
  return {group, items};
}

export function EntityLists({record, windowLabel, renderSources}){
  if (record.skin_id || !['complete', 'partial'].includes(record.answer?.status)
      || !Array.isArray(record.run?.entity_lists) || typeof windowLabel !== 'function') return null;
  const subject = SUBJECT.exec(record.question || '')?.[1];
  if (!subject) return null;
  const kind = subject.toLowerCase().replace(/s$/, '') + 's';
  const claims = new Map((Array.isArray(record.answer.claims) ? record.answer.claims : [])
    .filter((claim) => object(claim) && typeof claim.id === 'string').map((claim) => [claim.id, claim]));
  const evidence = new Map((Array.isArray(record.answer.evidence) ? record.answer.evidence : [])
    .filter((source) => object(source) && typeof source.id === 'string').map((source) => [source.id, source]));
  if (record.run.entity_lists.reduce((sum, group) => sum + (Array.isArray(group?.items) ? group.items.length : 0), 0) > 10) return null;
  const groups = record.run.entity_lists.map((group) => checked(group, kind, record, claims, evidence)).filter(Boolean);
  return <>{groups.map(({group, items}, index) => {
    const creator = kind === 'creators';
    const List = creator ? 'ul' : 'ol';
    return <section className="ask42-ranked ask42-entity-list" aria-label={creator ? 'Creators named in checked posts' : `Comparable checked ${kind}`} key={index}>
      <h3 className="ask42-section-title">{creator ? 'Creators named in checked posts, ranking unavailable' : `Comparable checked ${kind}`}</h3>
      <p className="ask42-ranked-basis">{creator ? 'Discovery scope: ' : ''}{group.market === null ? 'All markets' : MARKETS[group.market]} · {kind === 'topics' ? 'Activity over three days, ' : ''}{windowLabel(group.window)}.
        {!creator && <> Ranked by {kind === 'platforms' ? 'posts, then creators' : 'creators, then posts'}, among these checked items.</>}</p>
      <List className="ask42-claims">{items.map(({item, claim, creators, posts, previous}) => <li className="ask42-claim" key={item.entity_id} data-claim-id={claim.id}>
        <span className="ask42-ranked-name">{item.name}</span>
        {creator && <span className="ask42-ranked-note">{platformLabel(item.platform)}</span>}
        <p className="ask42-ranked-counts">{creator ? 'Creator total: Not recorded. Post total: Not recorded.' : <>{readerFigure(creators.value)} {unitFor(creators.value, 'creators')}, {readerFigure(posts.value)} {unitFor(posts.value, 'posts')}.</>}</p>
        <p className="ask42-ranked-note">{previous ? (previous.value === 0 ? 'No posts recorded in the same three days a week earlier.' : `${readerFigure(previous.value)} ${unitFor(previous.value, 'posts')} in the same three days a week earlier.`)
          : kind === 'topics' ? 'Same three days a week earlier: Not recorded.' : 'Previous week: Not recorded.'}</p>
        {item.tied_with_previous && <p className="ask42-ranked-note">Tied on creators and posts.</p>}
        {renderSources?.(claim, item.evidence_ids)}
      </li>)}</List>
    </section>;
  })}</>;
}
