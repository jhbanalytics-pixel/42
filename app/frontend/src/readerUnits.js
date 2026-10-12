/* Visual QA, 5 October 2026: a unit is written for a reader. A count of one
   takes the singular ("1 post first seen in 7 days", not "1 posts"), and a
   unit the model wrote as a warehouse name ("located_posts") reads as words.
   Only the leading noun changes; the rest of the unit stays as sent. */

import {platformLabel} from './ui/PlatformGlyph.jsx';

const PLURALS = Object.freeze({
  posts: 'post', creators: 'creator', items: 'item', days: 'day', credits: 'credit', accounts: 'account',
  topics: 'topic', searches: 'search', views: 'view', platforms: 'platform', markets: 'market', hashtags: 'hashtag',
});

const NAMED_UNITS = Object.freeze({
  located_posts: 'posts with a known location',
  located_creators: 'creators with a known location',
  posts_read: 'posts read',
  unique_creators: 'different creators',
});

/* A unit with its leading plural made singular when the value is exactly one. */
export function unitFor(value, unit){
  const text = String(unit || '').trim();
  if (Number(value) !== 1 || !text) return text;
  return text.replace(/^([A-Za-z]+)\b/, (word) => {
    const single = PLURALS[word.toLowerCase()];
    if (!single) return word;
    return word[0] === word[0].toUpperCase() ? single[0].toUpperCase() + single.slice(1) : single;
  });
}

/* A unit in reader words: a known warehouse name gets its words, and any
   other unit that is one snake_case name has its underscores read as spaces.
   A unit in words keeps its hashtags and handles as written. */
export function plainUnit(unit){
  const text = String(unit || '').trim();
  if (Object.hasOwn(NAMED_UNITS, text)) return NAMED_UNITS[text];
  return /^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$/i.test(text) ? text.replace(/_/g, ' ') : text;
}

/* A board entry title can carry the source page's line breaks as literal
   <br> tags ("<br>Gratitude<br>Asake"). The parts read as one line. */
export function boardTitle(title){
  return String(title || '')
    .split(/<br\s*\/?>/i)
    .map((part) => part.replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim())
    .filter(Boolean)
    .join(' · ');
}

/* A gap's search can name the records it read by their ids ("cited posts
   obs1_7bfd..., obs1_66fa..."). A reader gets how many; the ids stay in
   the stored record. A search can also name its source routes
   ("instagram/hashtag #fixture"): each route on a platform this app knows
   reads as that platform, and the search term right after it reads as
   "for" that term ("Instagram for #fixture", "Instagram for sunday best").
   The term ends at a comma, a colon, a semicolon, a full stop, "and", "or"
   or the next route, so each route keeps its own "for", and runs to six
   words at most. Words that open as a status sentence ("came back empty",
   "was rate limited", "returned nothing", "and x/search") stay as written,
   with any route in them named too. A slash after a word that is not a
   platform is left alone. */
const SOURCE_ROUTE = /\b([a-z][a-z0-9_]*)\/[a-z0-9_/-]*[a-z0-9_]((?:\s+[^\s,:;]+)*)/gi;
const JOINING = /^(?:and|or|at|for|returned|returns|with|from|by|via|on|in|then|but|came|comes|was|were|is|are|gave|gives|found|had|has|timed|failed|not|no|skipped|empty)\b/i;
const TERM_END = /\s+(?:and|or)\s+|\.(?=\s|$)|\s+(?=[a-z][a-z0-9_]*\/)/i;
const MAX_TERM_WORDS = 6;
const routeWords = (text) => text.replace(SOURCE_ROUTE, (whole, platform, tail) => {
  const route = whole.slice(0, whole.length - tail.length);
  const label = platformLabel(platform);
  if (!label) return route + routeWords(tail);
  const end = tail.search(TERM_END);
  const head = end === -1 ? tail : tail.slice(0, end);
  const term = head.trim();
  const isTerm = Boolean(term) && !JOINING.test(term) && term.split(/\s+/).length <= MAX_TERM_WORDS;
  return isTerm ? label + ' for ' + term + routeWords(tail.slice(head.length)) : label + routeWords(tail);
});

export function plainSearched(text){
  const ids = String(text || '').match(/\bobs\d*_[0-9a-f]{8,}\b/gi) || [];
  if (ids.length === 0) return routeWords(String(text || ''));
  const rest = String(text).replace(/\s*\bobs\d*_[0-9a-f]{8,}\b,?/gi, '').replace(/[\s,:]+$/, '').trim();
  if (/\bposts$/i.test(rest)) return ids.length + ' ' + (ids.length === 1 ? rest.replace(/posts$/i, 'post') : rest);
  return (rest ? rest + ', ' : '') + ids.length + (ids.length === 1 ? ' post' : ' posts');
}
