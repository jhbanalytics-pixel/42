/* Ask · what an answer rests on, in the reader's counts. Pure functions: the
   page, the tests and the export all say the same things.

   A comment is not a post. The record carries a comment's own id (the
   platform, then "_comment_") but the link of the post it sits under, so a
   commenter must never read as a creator, a post, or the owner of that link
   (core/agent/answer_view.py holds the Python twin of these rules). */
import {readerFigure} from './api.js';
import {plainUnit, unitFor} from './readerUnits.js';

export const isComment = (item) => Boolean(item && typeof item.id === 'string' && item.id.includes('_comment_'));

/* The author of the post a comment sits under. Only a TikTok link names its
   author; any other platform returns '' and the reader sees "a post". */
export function commentParentHandle(item){
  const match = /^https?:\/\/(?:www\.)?tiktok\.com\/@([^/?#]+)\//i.exec(String(item && item.url || ''));
  return match ? match[1] : '';
}

export function parentWords(item){
  const handle = commentParentHandle(item);
  return handle ? '@' + handle + "'s post" : 'a post';
}

const plural = (count, one, many) => readerFigure(count) + ' ' + (count === 1 ? one : many);

/* One tag for the whole answer from its claims' own labels: the label when
   every claim carries it, otherwise "Mixed confidence". */
export function confidenceTag(claims, words){
  const labels = [...new Set((claims || []).map((claim) => claim && claim.label).filter(Boolean))];
  if (labels.length === 0) return null;
  if (labels.length === 1) return {label: labels[0], ...(words[labels[0]] || {word: labels[0]})};
  return {label: 'mixed', word: 'Mixed confidence', shape: '◆'};
}

/* "Answer based on 3 posts by 3 creators and 2 comments". Creators are the
   distinct accounts behind the posts; a comment counts as a comment only. */
export function basisWords(evidence){
  const items = (evidence || []).filter((item) => item && typeof item === 'object');
  const posts = items.filter((item) => !isComment(item));
  const comments = items.filter((item) => isComment(item));
  if (!posts.length && !comments.length) return '';
  const creators = new Set(posts.map((post) => post.platform + ':' + String(post.handle || '').replace(/^@+/, '').toLowerCase()).filter((key) => !key.endsWith(':')));
  let line = 'Answer based on ';
  if (posts.length) line += plural(posts.length, 'post', 'posts') + ' by ' + plural(creators.size || posts.length, 'creator', 'creators');
  if (comments.length) line += (posts.length ? ' and ' : '') + plural(comments.length, 'comment', 'comments');
  return line;
}

/* The three scopes in one line: what the answer rests on, how much was read,
   how much the store holds for the window. */
export function scopeWords(record, windowWords){
  const run = (record && record.run) || {};
  const parts = [];
  const basis = basisWords(record && record.answer && record.answer.evidence);
  if (basis) parts.push(basis);
  if (typeof run.posts === 'number'){
    let read = plural(run.posts, 'post', 'posts') + ' read';
    if (typeof run.platforms === 'number') read += ' on ' + plural(run.platforms, 'platform', 'platforms');
    parts.push(read);
  }
  const store = run.store || {};
  if (typeof store.posts === 'number'){
    const span = windowWords(run.window);
    parts.push(plural(store.posts, 'post', 'posts') + ' in the store' + (span ? ' for ' + span : ''));
  }
  return parts.join(' · ');
}

/* Each distinct figure once, with the unit in words. Two claims that count
   the same thing (same value, same unit) leave one tile. */
export function distinctFigures(claims, limit = 8){
  const seen = new Set();
  const out = [];
  for (const claim of claims || []){
    for (const number of (claim && claim.numbers) || []){
      if (!number || typeof number.value !== 'number') continue;
      const unit = unitFor(number.value, plainUnit(number.unit));
      const key = readerFigure(number.value) + ' ' + unit;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({key, value: readerFigure(number.value), unit, queryId: number.query_id || ''});
    }
  }
  return out.slice(0, limit);
}

/* Posts each platform returned, once per platform. A call that failed is not
   a row with a zero: it is a note ("1 TikTok search failed"). */
const FAILED = new Set(['error', 'auth_failed', 'rate_limited', 'schema_drift', 'cap_reached']);
export function platformTotals(sourceStatus, nameOf){
  const totals = new Map();
  const failed = new Map();
  for (const source of sourceStatus || []){
    if (!source || !source.platform) continue;
    if (FAILED.has(source.status)){
      failed.set(source.platform, (failed.get(source.platform) || 0) + 1);
    } else if (typeof source.items === 'number'){
      totals.set(source.platform, (totals.get(source.platform) || 0) + source.items);
    }
  }
  return {
    rows: [...totals].map(([platform, posts]) => ({platform, posts})).sort((a, b) => b.posts - a.posts),
    notes: [...failed].map(([platform, count]) => count + ' ' + nameOf(platform) + (count === 1 ? ' search failed' : ' searches failed')),
  };
}

/* The gaps a reader sees: the first three, the rest left for the technical
   details. */
export const GAPS_SHOWN = 3;
export function gapSplit(gaps){
  const list = (Array.isArray(gaps) ? gaps : []).filter((gap) => gap && typeof gap === 'object');
  return {shown: list.slice(0, GAPS_SHOWN), rest: list.slice(GAPS_SHOWN)};
}
