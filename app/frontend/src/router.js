/* Hash router: every view is a shareable URL. Parses location.hash into
   {view, param, param2}; go() navigates; useHashRoute() subscribes. Kept tiny
   and dependency-free, the way the prototype runtime did it. */
import {useState, useEffect} from 'react';
import {legacyHashTarget} from './legacyRoutes.js';

function safeDecode(s){
  // A malformed percent-escape (e.g. '#/%' or '#/foo%zz') makes
  // decodeURIComponent throw URIError; parseHash runs in the useState
  // initializer and the hashchange handler, so an unguarded throw crashes the
  // whole React tree. Fall back to the raw segment instead.
  try { return decodeURIComponent(s); }
  catch { return s; }
}

const INVESTIGATION_ID = /^inv_[A-Za-z0-9_-]{1,128}$/;
const ARTIFACT_ID = /^ra_[A-Za-z0-9_-]{1,128}$/;
export const QUESTION_REQUEST_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const HISTORICAL_MODES = new Set(['analogue', 'recurrence', 'replay']);

export function canonicalizeDiscoverHash(hash){
  const source = String(hash || '');
  return /^#\/?discover(?:[/?]|$)/.test(source) ? '#/explore' : source;
}

function invalidEscape(value){
  return /%(?![0-9a-fA-F]{2})/.test(String(value || ''));
}

export function parseWorkspaceHash(hash){
  const source = String(hash || '');
  const raw = source.replace(/^#\/?/, '');
  const queryAt = raw.indexOf('?');
  const pathRaw = queryAt >= 0 ? raw.slice(0, queryAt) : raw;
  const queryRaw = queryAt >= 0 ? raw.slice(queryAt + 1) : '';
  const rawSegments = pathRaw.split('/');
  const view = safeDecode(rawSegments[0] || '');
  const result = {
    view,
    investigationId: null,
    artifactId: null,
    mode: null,
    error: null,
  };
  if (['topic', 'listen', 'seedpath', 'lexicon'].includes(view)){
    const query = new URLSearchParams(queryRaw);
    if (query.has('region')){
      const regions = query.getAll('region');
      const region = regions[0].toLowerCase();
      if (regions.length !== 1 || !['za', 'ng', 'ke', 'all'].includes(region) || invalidEscape(queryRaw) || rawSegments.some(invalidEscape) || rawSegments.length > 2 || (view === 'topic' && !rawSegments[1])) result.error = view === 'topic' ? 'topic_scope_invalid' : 'read_scope_invalid';
      else if (view === 'topic') result.topicRegion = region.toUpperCase();
      else result.readRegion = region.toUpperCase();
    }
    return result;
  }
  if (view === 'source-lab') {
    if (rawSegments.length !== 1 || queryRaw) result.error = 'workspace_request_invalid';
    return result;
  }
  if (view === 'fieldwork') {
    /* Scope is server owned. Fieldwork takes no client, market, vendor,
       source, budget, investigation, actor, approval or execution selector, so
       a path child or any query at all is refused. It stays on its own hash
       when refused: normalising to Briefing would hide the bad request and
       strand the strategist somewhere they did not ask to be. */
    if (rawSegments.length !== 1 || queryRaw) result.error = 'workspace_request_invalid';
    return result;
  }
  if (view === 'historical') {
    if (rawSegments.length === 1 && !queryRaw) return result;
    if (rawSegments.length !== 3 || queryRaw || rawSegments.some(invalidEscape)) {
      result.error = 'workspace_request_invalid';
      return result;
    }
    result.investigationId = safeDecode(rawSegments[1]);
    result.mode = safeDecode(rawSegments[2]);
    if (!INVESTIGATION_ID.test(result.investigationId)) {
      result.error = 'workspace_request_invalid';
    } else if (!HISTORICAL_MODES.has(result.mode)) {
      result.error = 'historical_mode_ineligible';
    }
    return result;
  }
  if (view === 'console') {
    if (rawSegments.length !== 1 || invalidEscape(queryRaw)) {
      result.error = 'workspace_request_invalid';
      return result;
    }
    const query = new URLSearchParams(queryRaw);
    if (query.has('request')) {
      result.requestId = null;
      try { decodeURIComponent(queryRaw); } catch { result.error = 'workspace_request_invalid'; return result; }
      const ids = query.getAll('request');
      const works = query.getAll('work');
      if (ids.length !== 1 || !QUESTION_REQUEST_ID.test(ids[0]) || works.length !== 1 || works[0] !== 'ask' || [...query.keys()].some(key => !['work', 'request'].includes(key))) result.error = 'workspace_request_invalid';
      else result.requestId = ids[0];
      return result;
    }
    const investigations = query.getAll('investigation');
    const artifacts = query.getAll('artifact');
    if (investigations.length > 1 || artifacts.length > 1) {
      result.error = 'workspace_request_invalid';
      return result;
    }
    result.investigationId = investigations[0] || null;
    result.artifactId = artifacts[0] || null;
    if (
      (result.investigationId && !INVESTIGATION_ID.test(result.investigationId))
      || (result.artifactId && !ARTIFACT_ID.test(result.artifactId))
      || (result.artifactId && !result.investigationId)
    ) result.error = 'workspace_request_invalid';
    return result;
  }
  return result;
}

export function buildWorkspaceHash({view, investigationId=null, artifactId=null, mode=null, requestId=null, work=null, personaId=null, markets=null, topics=null}={}){
  if (view === 'source-lab') return '#/source-lab';
  // Fieldwork takes no selector of any kind, so its hash is exactly itself.
  if (view === 'fieldwork') return '#/fieldwork';
  if (view === 'console') {
    if (requestId !== null) {
      if (typeof requestId !== 'string' || !QUESTION_REQUEST_ID.test(requestId) || investigationId || artifactId || mode || personaId || markets?.length || topics?.length || (work!==null && work!=='ask')) throw new Error('invalid question route');
      return '#/console?work=ask&request=' + requestId;
    }
    if (artifactId && !investigationId) throw new Error('artifact requires investigation');
    const query = new URLSearchParams({work: 'brief'});
    if (investigationId) query.set('investigation', investigationId);
    if (artifactId) query.set('artifact', artifactId);
    return '#/console?' + query.toString();
  }
  if (view === 'historical' && investigationId && HISTORICAL_MODES.has(mode)) {
    return '#/historical/' + encodeURIComponent(investigationId) + '/' + mode;
  }
  if (view === 'historical' && !investigationId && !mode) return '#/historical';
  throw new Error('invalid workspace route');
}

export function buildTopicHash(id, region){
  const scope = String(region || '').toLowerCase();
  if (typeof id !== 'string' || !id.trim() || !['za', 'ng', 'ke', 'all'].includes(scope)) throw new Error('Invalid topic identity');
  return '#/topic/' + encodeURIComponent(id) + '?region=' + scope;
}

export function buildScopedReadHash(view, term, region){
  const scope = String(region || '').toLowerCase();
  if (!['listen', 'seedpath', 'lexicon'].includes(view) || typeof term !== 'string' || !['za', 'ng', 'ke', 'all'].includes(scope)) throw new Error('Invalid read scope');
  return '#/' + view + (term ? '/' + encodeURIComponent(term) : '') + '?region=' + scope;
}

export function parseHash(){
  const source = window.location.hash || '';
  /* A desk page's link is replaced by its 42 page (legacyRoutes.js). */
  const canonical = legacyHashTarget(source) || canonicalizeDiscoverHash(source);
  if (canonical !== source){
    if (window.history && typeof window.history.replaceState === 'function') window.history.replaceState(null, '', canonical);
    else window.location.hash = canonical;
  }
  const raw = canonical.replace(/^#\/?/, '');
  const path = raw.split('?')[0];
  const seg = path.split('/');
  const workspace = parseWorkspaceHash(canonical);
  return {
    ...workspace,
    param: safeDecode(seg[1] || ''),
    param2: safeDecode(seg[2] || ''),
  };
}

export function go(path){
  const p = String(path || '');
  window.location.hash = p.startsWith('/') ? p : ('/' + p);
}

export function useHashRoute(){
  const [r, setR] = useState(parseHash);
  useEffect(() => {
    const onHash = () => { setR(parseHash()); window.scrollTo(0, 0); };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  return r;
}

/* Coverage reads one day from its hash, #/coverage?date=YYYY-MM-DD, so a day
   can be linked. Anything else (no date, a malformed one) reads as no date,
   and the API then answers for today. */
const COVERAGE_DATE = /^\d{4}-\d{2}-\d{2}$/;
export function parseCoverageDate(hash){
  const text = String(hash || '');
  const at = text.indexOf('?');
  if (at < 0) return null;
  const date = new URLSearchParams(text.slice(at + 1)).get('date');
  if (!date || !COVERAGE_DATE.test(date)) return null;
  const [y, m, d] = date.split('-').map(Number);
  const day = new Date(Date.UTC(y, m - 1, d));
  return day.getUTCFullYear() === y && day.getUTCMonth() === m - 1 && day.getUTCDate() === d ? date : null;
}

export function coverageHash(date){
  return date ? '#/coverage?date=' + date : '#/coverage';
}
