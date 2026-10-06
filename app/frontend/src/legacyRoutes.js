/* Page port, 3 October 2026. These pages were written for the desk API,
   which f42-api does not serve (core/api/app.py answers "No such API route."),
   so each one could only ever open on an error. A saved or shared link to one
   now lands on the 42 page that does the same job:

   Browse, the list of signals            Discover
   Network, who drives which topics       Communities
   Board, what you pinned                 Alerts, the watches you set
   Listen, posts that carry a word        Seed path, for the same word
   Source Lab, the sources                Fieldwork
   Historical, earlier waves              History
   the older console                      Ask for a question, Build or the
                                          investigation it named, History for
                                          a stored question
   the older topic page, given a 42 id    the 42 topic page

   An older topic or creator link that names no 42 id has nowhere honest to
   go, so it is left for the route to say so. Nothing here is fetched. */

const ITEM_ID = /^[0-9a-f]{64}$/;
const INVESTIGATION_ID = /^inv_[A-Za-z0-9_-]{1,128}$/;
const MARKETS = new Set(['za', 'ng', 'ke']);
const MOVED = Object.freeze({
  browse: '#/explore',
  network: '#/communities',
  board: '#/alerts',
  'source-lab': '#/fieldwork',
  historical: '#/history',
});
const CONSOLE_NAMES = new Set(['console', 'chat', 'research']);

function decoded(value){
  try { return decodeURIComponent(value); }
  catch { return null; }
}

/* The hash a desk page's link now stands for, or null when the hash is not
   one of them. */
export function legacyHashTarget(hash){
  const raw = String(hash || '').replace(/^#\/?/, '');
  if (!raw) return null;
  const at = raw.indexOf('?');
  const segments = (at >= 0 ? raw.slice(0, at) : raw).split('/');
  const query = new URLSearchParams(at >= 0 ? raw.slice(at + 1) : '');
  /* The view name is read decoded, as the router reads it, so an encoded
     spelling of a desk page lands where the plain one does. */
  const view = decoded(segments[0]) ?? segments[0];
  if (Object.hasOwn(MOVED, view)) return MOVED[view];

  if (view === 'listen'){
    const term = segments[1] || '';
    if (!term) return '#/seedpath';
    const regions = query.getAll('region').map((value) => value.toLowerCase());
    /* A malformed market stays on the link, so Seed path refuses it as
       Listen did rather than reading some other market. */
    if (regions.length > 1 || (regions.length === 1 && !MARKETS.has(regions[0]) && regions[0] !== 'all')) {
      return '#/seedpath/' + term + '?' + raw.slice(at + 1);
    }
    return '#/seedpath/' + term + (MARKETS.has(regions[0]) ? '?region=' + regions[0] : '');
  }

  if (view === 'topic'){
    const id = (decoded(segments[1] || '') || '').toLowerCase();
    if (!id || !ITEM_ID.test(id)) return null;
    const region = String(query.get('region') || '').toLowerCase();
    return '#/t/' + id + (MARKETS.has(region) ? '?market=' + region.toUpperCase() : '');
  }

  if (CONSOLE_NAMES.has(view)){
    const question = segments[1] ? decoded(segments.slice(1).join('/')) : '';
    if (question && question.trim()) return '#/ask?' + new URLSearchParams({q: question.trim(), draft: '1'}).toString();
    /* A blank or unreadable question in the path would open the older
       console, so it lands on Build. */
    if (segments.slice(1).some(Boolean)) return '#/console';
    if (query.has('request')) return '#/history';
    const work = query.get('work');
    /* On the console itself work=ask already lands on #/ask (App.jsx
       askAliasTarget); the chat and research names do not reach it. */
    if (work === 'ask') return view === 'console' ? null : '#/ask';
    const investigation = query.get('investigation') || '';
    if (INVESTIGATION_ID.test(investigation)) return '#/investigations/' + investigation;
    /* The plain console is Build's own page already. */
    if (view === 'console' && at < 0) return null;
    return '#/console';
  }
  return null;
}
