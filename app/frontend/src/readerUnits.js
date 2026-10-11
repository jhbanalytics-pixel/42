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
   reads as that platform, and one search term right after it, before a
   comma, a colon or the end, reads as "for" that term ("Instagram for
   #fixture"). Any other words after a route stay as written, and a slash
   after a word that is not a platform is left alone. */
const SOURCE_ROUTE = /\b([a-z][a-z0-9_]*)\/[a-z0-9_/-]*[a-z0-9_](?:\s+([^\s,:]+)(?=[,:]|$))?/gi;
const routeWords = (text) => text.replace(SOURCE_ROUTE, (whole, platform, term) => {
  const label = platformLabel(platform);
  if (!label) return whole;
  return label + (term ? ' for ' + term : '');
});

export function plainSearched(text){
  const ids = String(text || '').match(/\bobs\d*_[0-9a-f]{8,}\b/gi) || [];
  if (ids.length === 0) return routeWords(String(text || ''));
  const rest = String(text).replace(/\s*\bobs\d*_[0-9a-f]{8,}\b,?/gi, '').replace(/[\s,:]+$/, '').trim();
  if (/\bposts$/i.test(rest)) return ids.length + ' ' + (ids.length === 1 ? rest.replace(/posts$/i, 'post') : rest);
  return (rest ? rest + ', ' : '') + ids.length + (ids.length === 1 ? ' post' : ' posts');
}
