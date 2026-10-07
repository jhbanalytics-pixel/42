import React from 'react';
import {readerFigure} from './api.js';
import {unitFor} from './readerUnits.js';
import {platformLabel} from './ui/PlatformGlyph.jsx';

const MARKETS = {ZA: 'South Africa', NG: 'Nigeria', KE: 'Kenya'};
const HASH = /^sha256:[0-9a-f]{64}$/;
const PRIOR_UNITS = new Set(['posts', 'posts in previous week', 'posts in the previous week', 'posts in preceding week']);
const UNRANKED_NOTICE = 'The checked findings do not share a verified basis for ranking, so they are shown without a ranked list.';
const ITEM_QUESTION = /^\s*(?:which|what)\s+(?:(?:tiktok|instagram|youtube|facebook|threads|reddit|x|new|rising|trending|original|untitled|top|popular|most\s+used)\s+)*(sounds?|audio|hashtags?)\b(?=\s*(?:[?.!,;:]|$)|\s+(?:are|is|was|were|have|has|had|do|does|did|can|could|will|would|should|on|in|from|across|during|since|between|over|this|last|today|yesterday|rose|grew|fell|gained|spread|spreading|rising|trending|popular|used)\b)/i;

function questionKind(question){
  const subject = ITEM_QUESTION.exec(question || '')?.[1];
  return subject ? (subject.toLowerCase().startsWith('hashtag') ? 'hashtags' : 'sounds') : null;
}

function hasHashtag(text, title){
  if (typeof title !== 'string' || title.length <= 1 || !title.startsWith('#') || /\s/u.test(title)) return false;
  const asciiFold = (value) => value.replace(/[A-Z]/g, (char) => char.toLowerCase());
  text = asciiFold(String(text || ''));
  const continuation = (char) => /[\p{L}\p{N}\p{M}_#]/u.test(char || '');
  const escaped = asciiFold(title).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return [...text.matchAll(new RegExp(escaped, 'gu'))].some((match) => (
    !continuation(Array.from(text.slice(0, match.index)).at(-1))
      && !continuation(Array.from(text.slice(match.index + match[0].length))[0])
  ));
}

function windowBounds(value){
  const day = (text) => {
    if (typeof text !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(text) || text.startsWith('0000-')) return null;
    const stamp = Date.parse(text + 'T00:00:00Z');
    return Number.isFinite(stamp) && new Date(stamp).toISOString().slice(0, 10) === text ? stamp : null;
  };
  const from = day(value?.from);
  const to = day(value?.to);
  return from !== null && to !== null && to >= from ? {from, to} : null;
}

function checkedItems(record){
  const run = record.run || {};
  const list = run.ranked_list;
  const currentWindow = windowBounds(list?.window);
  if (record.skin_id || !questionKind(record.question)
      || !['complete', 'partial'].includes(record.answer?.status)
      || !list || list.kind !== questionKind(record.question) || list.scope !== 'retained_whole_store_claims'
      || list.order !== 'creators_desc_posts_desc' || typeof list.query_id !== 'string' || !list.query_id
      || typeof list.platform !== 'string' || !list.platform || (list.market !== null && !MARKETS[list.market])
      || list.measure_columns?.creators !== 'creators' || list.measure_columns?.posts !== 'posts'
      || !currentWindow
      || list.window?.from !== run.window?.from || list.window?.to !== run.window?.to
      || !Array.isArray(list.items) || list.items.length === 0) return null;
  const claims = new Map((record.answer?.claims || []).map((claim) => [claim.id, claim]));
  const seen = new Set();
  const items = [];
  let sourceHash = null;
  for (const item of list.items){
    if (!item || typeof item !== 'object' || Array.isArray(item)) return null;
    const claim = claims.get(item.claim_id);
    if (!claim || seen.has(item.claim_id) || (item.title !== null && typeof item.title !== 'string')
        || (item.usage_handle !== null && typeof item.usage_handle !== 'string')
        || typeof item.tied_with_previous !== 'boolean') return null;
    if (list.kind === 'hashtags' && (item.usage_handle !== null || !hasHashtag(claim.text, item.title))) return null;
    const number = (index) => {
      if (!Number.isInteger(index) || index < 0) return null;
      const pin = claim.numbers?.[index];
      return pin && Number.isFinite(pin.value) && Number.isInteger(pin.value) && pin.value >= 0
        && pin.run_id === run.run_id && HASH.test(pin.result_hash) ? pin : null;
    };
    const creators = number(item.creators_index);
    const posts = number(item.posts_index);
    if (!creators || !posts || String(creators.unit).trim().toLowerCase() !== 'creators'
        || String(posts.unit).trim().toLowerCase() !== 'posts'
        || creators.query_id !== list.query_id || posts.query_id !== list.query_id
        || creators.result_hash !== posts.result_hash || (sourceHash && creators.result_hash !== sourceHash)) return null;
    sourceHash = creators.result_hash;
    let previous = null;
    if (item.previous_posts_index !== null){
      previous = number(item.previous_posts_index);
      if (!previous || !list.previous_window?.from || !list.previous_window?.to
          || !PRIOR_UNITS.has(String(previous.unit).trim().toLowerCase())
          || !previous.query_id || previous.query_id === list.query_id) return null;
      const priorWindow = windowBounds(list.previous_window);
      if (!priorWindow || currentWindow.to - currentWindow.from !== 6 * 86400000
          || priorWindow.to - priorWindow.from !== 6 * 86400000
          || currentWindow.from - priorWindow.to !== 86400000) return null;
    }
    const last = items.at(-1);
    if (last && (creators.value > last.creators.value
        || (creators.value === last.creators.value && posts.value > last.posts.value))) return null;
    const tied = Boolean(last && creators.value === last.creators.value && posts.value === last.posts.value);
    if (item.tied_with_previous !== tied) return null;
    items.push({item, claim, creators, posts, previous});
    seen.add(item.claim_id);
  }
  return {list, items};
}

export function RankedAnswer({record, windowLabel, renderSources}){
  const checked = checkedItems(record);
  if (!checked){
    if (!record.skin_id && ['complete', 'partial'].includes(record.answer?.status)
        && questionKind(record.question)
        && !(record.run?.notices || []).includes(UNRANKED_NOTICE)){
      return <p className="ask42-ranked-unavailable">{record.run?.ranked_list
        ? 'A comparable ranked list could not be verified for this answer.'
        : 'A comparable ranked list was not recorded for this saved answer.'}</p>;
    }
    return null;
  }
  if (typeof windowLabel !== 'string' || !windowLabel) return null;
  const {list, items} = checked;
  return (
    <section className="ask42-ranked" aria-label={`Comparable checked ${list.kind}`}>
      <h3 className="ask42-section-title">Comparable checked {list.kind}</h3>
      <p className="ask42-ranked-basis">Market scope: {list.market === null ? 'All markets' : MARKETS[list.market]} · {platformLabel(list.platform)} · {windowLabel}. Ranked by creators, then posts, among the checked items below.</p>
      <ol className="ask42-claims">
        {items.map(({item, claim, creators, posts, previous}) => (
          <li className="ask42-claim" key={claim.id} data-claim-id={claim.id}>
            <span className="ask42-ranked-name">{item.title || (item.usage_handle ? `Original sound used by @${item.usage_handle.replace(/^@/, '')}` : 'Untitled sound')}</span>
            {!item.title && <span className="ask42-ranked-note">Title not verified</span>}
            <p className="ask42-ranked-counts" data-query-id={creators.query_id}>{readerFigure(creators.value)} {unitFor(creators.value, 'creators')}, {readerFigure(posts.value)} {unitFor(posts.value, 'posts')}.</p>
            <p className="ask42-ranked-note" data-query-id={previous?.query_id}>
              {previous ? (previous.value === 0 ? 'No stored posts in the previous week.' : `${readerFigure(previous.value)} ${unitFor(previous.value, 'posts')} in the previous week.`) : 'Previous week not verified.'}
            </p>
            {item.tied_with_previous && <p className="ask42-ranked-note">Tied on creators and posts.</p>}
            {renderSources?.(claim)}
          </li>
        ))}
      </ol>
    </section>
  );
}
