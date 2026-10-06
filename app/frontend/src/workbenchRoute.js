/* Pure workbench URL parse/build helpers (no React). */
import {buildWorkspaceHash, parseWorkspaceHash} from './router.js';

function parseHashQuery(hash){
  const raw = String(hash || '').replace(/^#\/?/, '');
  const qi = raw.indexOf('?');
  return new URLSearchParams(qi >= 0 ? raw.slice(qi + 1) : '');
}

function hashRouteName(hash){
  const raw = String(hash || '').replace(/^#\/?/, '');
  const first = raw.split('/')[0] || '';
  const qi = first.indexOf('?');
  return qi >= 0 ? first.slice(0, qi) : first;
}

function marketList(raw){
  return String(raw || '')
    .split(',')
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);
}

function copyMarkets(markets){
  return Array.isArray(markets)
    ? markets.map((m) => String(m).trim().toLowerCase()).filter(Boolean)
    : [];
}

export function resolveResearchRouteSelection(route, personas){
  const requestedMarkets = copyMarkets(route && route.markets);
  const persona = (personas || []).find((item) => item.id === (route && route.personaId));
  if (!persona){
    return {personaId: '', productFrame: '', markets: requestedMarkets};
  }
  return {
    personaId: persona.id,
    productFrame: persona.product_frame || '',
    markets: requestedMarkets.length ? requestedMarkets : copyMarkets(persona.default_markets),
  };
}

export function resolveResearchRouteUpdate(
  route,
  personas,
  currentPersonaId,
  currentProductFrame,
){
  const selection = resolveResearchRouteSelection(route, personas);
  if (selection.personaId && selection.personaId === currentPersonaId){
    return {...selection, productFrame: String(currentProductFrame ?? '')};
  }
  return selection;
}

export function resolveResearchPersonaSelection(personaId, personas, currentMarkets){
  const retainedMarkets = copyMarkets(currentMarkets);
  const persona = (personas || []).find((item) => item.id === personaId);
  if (!persona){
    return {personaId: '', productFrame: '', markets: retainedMarkets};
  }
  return {
    personaId: persona.id,
    productFrame: persona.product_frame || '',
    markets: retainedMarkets.length ? retainedMarkets : copyMarkets(persona.default_markets),
  };
}

/* A dossier or artifact version is an exact content digest. Anything else on
   the hash is not a version, so it is not carried: a word like latest would
   let a copied link silently reopen something other than what was shared. */
const VERSION_DIGEST = /^[0-9a-f]{64}$/;

function exactVersion(raw){
  const value = String(raw || '').trim();
  return VERSION_DIGEST.test(value) ? value : null;
}

export function parseWorkbenchRoute(hash){
  const h = hash || '#/console';
  const route = hashRouteName(h);
  const sp = parseHashQuery(h);
  if (sp.has('request')) {
    const parsed = parseWorkspaceHash(h);
    return {route, work:'ask', requestId:parsed.requestId || null, error:parsed.view === 'console' ? parsed.error : 'workspace_request_invalid', investigationId:null, dossierVersion:undefined, artifactId:null, artifactVersion:undefined, personaId:null, markets:null, topics:null, clientLensId:sp.get('lens') || null};
  }
  const artifactId = sp.get('artifact') || sp.get('research') || null;
  const investigationId = sp.get('investigation') || null;
  /* The route carries identities only: the investigation, the exact dossier
     version and the exact artifact version. Content is read by id and never
     travels on the hash. */
  /* An absent version is undefined rather than null, so a route that names
     none keeps exactly the shape it had before versions were carried. */
  const dossierVersion = (investigationId && exactVersion(sp.get('version'))) || undefined;
  const artifactVersion = (artifactId && exactVersion(sp.get('artifact_version'))) || undefined;
  const personaId = sp.get('persona_id') || null;
  /* The client lens is part of the console's identity: a reload under a lens
     reopens under that lens, and no lens on the hash is general 42. */
  const clientLensId = sp.get('lens') || null;
  const markets = sp.get('markets') ? marketList(sp.get('markets')) : null;
  const topics = sp.get('topics')
    ? String(sp.get('topics')).split(',').map((pair) => {
        const [mk, qg] = String(pair).split(':');
        return {market: (mk || '').trim().toLowerCase(), query_group: (qg || '').trim()};
      }).filter((t) => t.market && t.query_group)
    : null;
  let work = sp.get('work') || null;
  if (!work && sp.get('mode') === 'research') work = 'brief';
  if (!work && (route === 'research' || artifactId || personaId)) work = 'brief';
  if (!work) work = 'landing';
  if (work === 'research') work = 'brief';
  /* The Console has exactly three work states. Anything else on the hash is
     not a route, so it resolves to the landing rather than being carried
     through as if it named a surface. */
  if (work !== 'ask' && work !== 'brief') work = 'landing';
  return {route, work, investigationId, dossierVersion, artifactId, artifactVersion, personaId, markets, topics, clientLensId};
}

export function buildWorkbenchHash(state){
  const st = state || {};
  if (st.requestId !== undefined && st.requestId !== null) {
    if (st.work !== 'ask' || st.investigationId || st.artifactId || st.personaId || st.markets?.length || st.topics?.length) throw new Error('invalid question route');
    return buildWorkspaceHash({view:'console', requestId:st.requestId});
  }
  const sp = new URLSearchParams();
  const work = st.work === 'ask' ? 'ask' : st.work === 'brief' ? 'brief' : '';
  if (work) sp.set('work', work);
  if (st.investigationId) sp.set('investigation', st.investigationId);
  if (st.investigationId && exactVersion(st.dossierVersion)) sp.set('version', st.dossierVersion);
  if (st.artifactId) sp.set('artifact', st.artifactId);
  if (st.artifactId && exactVersion(st.artifactVersion)) sp.set('artifact_version', st.artifactVersion);
  if (st.personaId) sp.set('persona_id', st.personaId);
  if (st.clientLensId) sp.set('lens', String(st.clientLensId));
  if (st.markets && st.markets.length) {
    sp.set('markets', st.markets.map((m) => String(m).toLowerCase()).join(','));
  }
  if (st.topics && st.topics.length) {
    sp.set('topics', st.topics.map((t) => `${String(t.market).toLowerCase()}:${t.query_group}`).join(','));
  }
  const q = sp.toString();
  return '#/console' + (q ? '?' + q : '');
}

export function buildWorkbenchPath(state){
  return buildWorkbenchHash(state).replace(/^#/, '');
}
