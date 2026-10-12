/* Ask · what an answer rests on, in the reader's counts. Pure functions: the
   page, the tests and the export all say the same things.

   A comment is not a post. The record carries a comment's own id (the
   platform, then "_comment_") but the link of the post it sits under, so a
   commenter must never read as a creator, a post, or the owner of that link
   (core/agent/answer_view.py holds the Python twin of these rules). */
import {readerFigure} from './api.js';
import {plainUnit, unitFor} from './readerUnits.js';

export const isComment = (item) => Boolean(item && typeof item.id === 'string' && item.id.includes('_comment_'));

/* A comment carries the link of the post it sits under, handle and all, and
   no reader has judged that link: a creator hidden by id and renamed since
   still shows in it by the old handle. So a comment is named for its post
   only when that post is itself among the sources shown, which the privacy
   readers have already judged (core/api/export.py says the same in Python).
   withParents returns the evidence with each comment resolved that way; the
   resolution lives outside the record, so nothing on the wire can name a
   post, and a comment nobody resolved reads "a post". */
const parentHandles = new WeakMap();

export function withParents(evidence){
  const list = Array.isArray(evidence) ? evidence : [];
  const posts = new Map();
  for (const item of list){
    if (item && typeof item === 'object' && !isComment(item) && item.url && typeof item.handle === 'string' && item.handle.trim()) posts.set(item.url, item.handle.trim());
  }
  return list.map((item) => {
    if (!isComment(item)) return item;
    const copy = {...item};
    if (posts.has(item.url)) parentHandles.set(copy, posts.get(item.url));
    return copy;
  });
}

/* Whether the post a comment sits under is among the sources shown. A link to a post that is not shown is not shown. */
export const parentShown = (item) => parentHandles.has(item);

export function parentWords(item){
  const handle = parentHandles.get(item);
  return handle ? '@' + handle.replace(/^@+/, '') + "'s post" : 'a post';
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
   the same thing (same value, same unit) leave one tile. Each figure also
   carries the position of the claim it is pinned to, so a tile can name it. */
export function distinctFigures(claims, limit = 8){
  const seen = new Set();
  const out = [];
  (claims || []).forEach((claim, claimIndex) => {
    for (const number of (claim && claim.numbers) || []){
      if (!number || typeof number.value !== 'number') continue;
      const unit = unitFor(number.value, plainUnit(number.unit));
      const key = readerFigure(number.value) + ' ' + unit;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({key, value: readerFigure(number.value), unit, queryId: number.query_id || '', raw: number.value, claimIndex, claimText: String(claim.text || '')});
    }
  });
  return out.slice(0, limit);
}

export const UNNAMED_FIGURE = 'Counted for a finding with no words of its own.';

const sentence = (text) => (/[.!?]$/.test(text) ? text : text + '.');
const nounOf = (unit) => (/^posts?$/i.test(unit) ? 'posts' : /^creators?$/i.test(unit) ? 'creators' : '');
const nounOfNumber = (number) => nounOf(unitFor(number.value, plainUnit(number.unit)));

/* The claims whose posts and creators may be named accounts posting side by
   side. The decision is made on the claim's own numbers, before any repeat is
   dropped: exactly one posts figure and one creators figure, counted by the
   same query, with no more creators than posts. Anything else would put
   figures from two queries, or two periods, in one sentence. */
function joinableClaims(claims){
  const joinable = new Set();
  (claims || []).forEach((claim, index) => {
    const numbers = ((claim && claim.numbers) || []).filter((number) => number && typeof number.value === 'number');
    const posts = numbers.filter((number) => nounOfNumber(number) === 'posts');
    const creators = numbers.filter((number) => nounOfNumber(number) === 'creators');
    if (posts.length === 1 && creators.length === 1 && posts[0].query_id && posts[0].query_id === creators[0].query_id && creators[0].value <= posts[0].value) joinable.add(index);
  });
  return joinable;
}

/* One count in words, and a list of counts read aloud. */
const countWords = (part) => part.value + ' ' + part.unit;
const spoken = (parts) => (parts.length < 3 ? parts.map(countWords).join(' and ') : parts.slice(0, -1).map(countWords).join(', ') + ' and ' + countWords(parts[parts.length - 1]));

/* The tiles of The numbers: one tile for each claim that has a figure to
   show. The headline shows at most the claim's first posts figure and its
   first creators figure, and only when they stand side by side ("179 posts ·
   118 accounts posting"): the claim has exactly one of each, counted by the
   same query, with no more creators than posts. Otherwise the headline is the
   claim's first figure alone. Every other figure of the claim is left to the
   claim text under the headline, never shown as a bare number beside it. The
   claim is shown once, whole: it is never cut, clamped or hidden behind a
   toggle. There is no "by" between figures, because the same query does not
   prove the same row. A claim with no words says so, so no tile is ever a bare
   number. A tile gives no market or window: a stored figure does not record
   one. A tile carries every query id of its claim's figures. The limit counts
   the figures shown. */
export function figureTiles(claims, limit = 8){
  const joinable = joinableClaims(claims);
  const byClaim = new Map();
  for (const figure of distinctFigures(claims, Infinity)) byClaim.set(figure.claimIndex, [...(byClaim.get(figure.claimIndex) || []), figure]);
  const tiles = [];
  let shown = 0;
  for (const [claimIndex, list] of byClaim){
    const posts = list.find((figure) => nounOf(figure.unit) === 'posts');
    const creators = list.find((figure) => nounOf(figure.unit) === 'creators');
    const paired = joinable.has(claimIndex) && Boolean(posts) && Boolean(creators);
    const parts = paired ? [posts, {...creators, unit: unitFor(creators.raw, 'accounts posting')}] : [list[0]];
    if (shown + parts.length > limit) break;
    shown += parts.length;
    const text = list[0].claimText.replace(/\s+/g, ' ').trim();
    const ids = [...new Set(((claims[claimIndex] && claims[claimIndex].numbers) || []).filter((number) => number && typeof number.value === 'number').map((number) => number.query_id).filter(Boolean))];
    tiles.push({
      key: claimIndex + ': ' + parts.map((part) => part.key).join(' | '),
      parts: parts.map(({value, unit}) => ({value, unit})),
      line: parts.map(countWords).join(' · '),
      queryId: ids[0] || '',
      queryIds: ids,
      joined: parts.length > 1,
      about: text || UNNAMED_FIGURE,
      sentence: spoken(parts) + '. ' + (text ? 'Finding: ' + sentence(text) : UNNAMED_FIGURE),
    });
  }
  return tiles;
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
