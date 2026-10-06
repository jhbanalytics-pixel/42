/* Data access. One in-session cache for GETs; the engine refreshes daily. */
import {useState, useEffect} from 'react';
import {apiFailureDetails, normalizeApiDetail} from './redesignContract.js';

export const PASS_KEY = 'pulse_passcode';

export function storedValue(key, fallback = ''){
  try {
    const value = localStorage.getItem(key);
    return value === null ? fallback : value;
  } catch (_error){
    return fallback;
  }
}

/* bounded LRU: the engine refreshes daily, so a handful of GET paths is the
   real working set. Cap the Map and evict the oldest entry on overflow so a
   long-lived session cannot grow it without limit. */
const MAX_CACHE = 50;
const cache = new Map();

function cacheSet(path, p){
  if (cache.size >= MAX_CACHE){
    const oldest = cache.keys().next().value;
    if (oldest !== undefined) cache.delete(oldest);
  }
  cache.set(path, p);
}

async function readApiError(res){
  try {
    const j = await res.json();
    const parsed = apiFailureDetails(res.status, j, res.headers && res.headers.get('Retry-After'));
    if (parsed.message) return parsed;
    const detail = normalizeApiDetail(j.detail ?? j.message ?? '');
    if (detail) return {message: String(detail)};
  } catch (_) {}
  if (res.status === 429) return {message: 'Too many requests. Wait a moment and try again.'};
  if (res.status === 503) return {message: 'Service unavailable right now.'};
  const path = (res.url || '').split('?')[0];
  if (path.includes('/api/research/')) return {message: 'Research request failed (' + res.status + ').'};
  /* No code: the body carried none. A read that raises inside its handler is
     answered by the framework in plain text, and a proxy in front of the
     service answers in HTML, so there is nothing here that is the producer's
     own reason. useApi falls back to the status, which is announced as a
     status and not as a reason the producer gave. */
  return {message: 'The completed run could not be read just now.'};
}

async function authCheck(res){
  if (res.status === 401){ const e = new Error('Passcode required'); e.auth = true; e.status = 401; throw e; }
  if (!res.ok){
    const failure = await readApiError(res);
    const e = new Error(failure.message);
    e.status = res.status;
    if (failure.code) e.code = failure.code;
    if (failure.retryAfterSeconds) e.retryAfterSeconds = failure.retryAfterSeconds;
    throw e;
  }
  // when the feed is down the dev/proxy hands back an HTML page, so res.json()
  // throws a raw "Unexpected token '<'". Swallow it for a clean, demo-safe line.
  return res.json().catch(() => { const error = new Error('the signal feed is offline right now.'); error.code = 'response_invalid'; error.status = res.status; throw error; });
}

/* Where a failed read's code came from. useApi prints the producer's own
   code when the body carried one, the status when it did not, and the
   transport's truth when no response arrived, so a page that shows the code
   has to say which of the three it is: a status and a token this app derived
   are not the producer's words. */
export function failureOrigin(code){
  if (typeof code !== 'string' || !code.trim()) return 'unstated';
  if (/^http_\d{3}$/.test(code)) return 'status';
  if (code === 'network_unreachable') return 'transport';
  if (DERIVED_READ_CODES.has(code)) return 'derived';
  return 'producer';
}

/* Codes this module derives without a producer body: response_invalid follows
   a successful status whose body did not parse as JSON; request_timeout is
   the local read deadline. */
const DERIVED_READ_CODES = new Set(['response_invalid', 'request_timeout']);

/* The code useApi records for a failed read: the body code where the
   producer sent one, the status where it did not, and the transport's truth
   where no response arrived. */
export function failureCode(error){
  if (error && error.code) return error.code;
  if (error && error.status) return 'http_' + error.status;
  return 'network_unreachable';
}

/* The label each origin is announced under. A page that derives its own
   refusal from the records it read announces it as derived. */
export const FAILURE_ORIGIN_LABEL = Object.freeze({
  producer: 'Reason the producer gave:',
  status: 'The producer sent no reason; the read failed with status:',
  transport: 'The producer sent no reason and no answer arrived:',
  unstated: 'The producer sent no reason:',
  derived: 'No producer said this; this page derived it from the records it read:',
});

/* Presentation helpers for the workspace pages. They change how a value is
   written for a reader and never the value a request sent or a page reads.

   A figure is written the South African way: thousands grouped by a space,
   so 25000 reads 25 000. The decimal keeps its point, 1.5, because that is
   the one decimal mark the rest of the product prints (US$0.10, 1.2k) and a
   page must not mix two. The space is a no-break space so a figure never
   splits across two lines. Grouping is done here rather than through the
   en-ZA locale because a runtime without that locale's data falls back to
   commas, and a figure must read the same everywhere it is printed. Anything that is not a plain number is written
   as given. */
export function readerFigure(value){
  /* A producer that sends a decimal as a string, "250100", is written the
     same way as the number, digit for digit, without a round trip through
     floating point. */
  const written = typeof value === 'number' && Number.isFinite(value) ? String(value)
    : typeof value === 'string' ? value.trim() : '';
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(written);
  if (!match) return String(value);
  const grouped = match[2].replace(/\B(?=(\d{3})+(?!\d))/g, '\u00a0');
  return match[1] + grouped + (match[3] ? '.' + match[3] : '');
}

/* A charted series value: a panel day is posts scaled for the share of the
   panel read (views.sql v_series_daily), so it can be 2.3333333333333335.
   One decimal at most; a whole number stays whole. */
export function seriesFigure(value){
  if (typeof value !== 'number' || !Number.isFinite(value)) return readerFigure(value);
  const rounded = Math.round(value * 10) / 10;
  return readerFigure(Number.isInteger(rounded) ? rounded : rounded.toFixed(1));
}

/* A state word as a reader sees it: the first letter raised, the rest as the
   label already wrote it, so "not applicable" reads "Not applicable". */
export function sentenceCase(text){
  const words = String(text ?? '').trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : words;
}

export function apiGet(path){
  if (cache.has(path)) return cache.get(path);
  const p = fetch(path, {headers: {'X-Passcode': storedValue(PASS_KEY)}})
    .then(authCheck)
    .catch(err => {
      if (cache.get(path) === p) cache.delete(path);
      throw err;
    });
  cacheSet(path, p);
  return p;
}

/* uncached GET for polling endpoints: every call hits the server. An optional
   AbortSignal lets the ask flow stop polling when the panel closes. Extra
   headers are added after the passcode and never replace it. */
export function apiGetFresh(path, signal, extraHeaders, onResponse){
  return fetch(path, {headers: {...(extraHeaders || {}), 'X-Passcode': storedValue(PASS_KEY)}, signal})
    .then(res => { if (onResponse) onResponse(res); return res; })
    .then(authCheck);
}

/* Extra headers are added after the passcode and never replace it. The dossier
   review commands use them for the review session's CSRF token, which is held
   in memory and never written to a URL or to storage. */
export function apiPost(path, body, signal, extraHeaders){
  return fetch(path, {
    method: 'POST',
    headers: {
      ...(extraHeaders || {}),
      'Content-Type': 'application/json',
      'X-Passcode': storedValue(PASS_KEY)
    },
    body: JSON.stringify(body),
    signal
  }).then(authCheck);
}

export function clearCache(){
  cache.clear();
}

/* Drops every cached read whose path starts with prefix, for a page whose
   answer changes through the day and must be read again when it opens. */
export function forgetCached(prefix){
  for (const path of [...cache.keys()]) if (path.startsWith(prefix)) cache.delete(path);
}

export function useApi(path, refreshKey, onAuth, enabled, timeoutMs = 0){
  const [s, setS] = useState(() => (enabled === false ? {state: 'idle'} : {state: 'loading'}));
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (enabled === false){
      setS({state: 'idle'});
      return;
    }
    let live = true;
    let timedOut = false;
    let timer = null;
    const clearTimer = () => {
      if (timer !== null){
        clearTimeout(timer);
        timer = null;
      }
    };
    setS({state: 'loading'});
    const request = apiGet(path);
    if (timeoutMs > 0){
      timer = setTimeout(() => {
        if (live){
          timedOut = true;
          setS({state: 'error', message: 'The request timed out. Try again.', code: 'request_timeout'});
        }
      }, timeoutMs);
    }
    request.then(d => {
      clearTimer();
      if (live && !timedOut) setS({state: 'ready', data: d});
    })
      .catch(e => {
        clearTimer();
        if (!live || timedOut) return;
        const code = failureCode(e);
        if (e && e.auth){ onAuth && onAuth(); setS({state: 'auth', code}); }
        else setS({state: 'error', message: e && e.message ? e.message : 'Unknown failure', code});
      });
    return () => {
      live = false;
      clearTimer();
    };
  }, [path, tick, refreshKey, enabled, timeoutMs]);
  return [s, () => { cache.delete(path); setTick(t => t + 1); }];
}
