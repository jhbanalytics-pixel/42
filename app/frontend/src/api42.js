/* Reads and writes for the 42 API (core/api/contract.md). Errors keep the
   API's own {error, message}: .code is the error code, .status the HTTP
   status, a 401 is marked .auth so the page can hand over to the
   passcode screen, and the claims a refused dossier freeze names are kept
   as .claims. */
import {PASS_KEY, storedValue} from './api.js';
import {createSSEParser} from './askTransport42.js';

async function request(path, options){
  return (await send(path, options)).json();
}

async function send(path, {method, body, signal} = {}){
  const headers = {'X-Passcode': storedValue(PASS_KEY)};
  const init = {headers, signal};
  if (method){
    init.method = method;
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body === undefined ? {} : body);
  }
  const res = await fetch(path, init);
  if (!res.ok) throw await failure(res);
  return res;
}

async function failure(res){
  let payload = null;
  try { payload = await res.json(); } catch (_error){ payload = null; }
  const error = new Error((payload && payload.message) || 'The request failed (' + res.status + ').');
  error.status = res.status;
  error.code = (payload && payload.error) || null;
  if (res.status === 401) error.auth = true;
  if (payload && Array.isArray(payload.claims)) error.claims = payload.claims;
  return error;
}

export function getJson(path, {signal} = {}){
  return request(path, {signal});
}

function postJson(path, body, {signal} = {}){
  return request(path, {method: 'POST', body, signal});
}

export function fetchToday(date, options){
  return getJson('/api/today' + (date ? '?date=' + encodeURIComponent(date) : ''), options);
}

export function fetchTrend(itemId, market, date, options){
  const query = new URLSearchParams({market});
  if (date) query.set('date', date);
  return getJson('/api/trends/' + encodeURIComponent(itemId) + '?' + query.toString(), options);
}

/* Contract section 10.2. Empty filters are left off the query. */
export function fetchDiscover({market, kind, state, platform, sort, limit, cursor} = {}, options){
  const query = new URLSearchParams();
  for (const [key, value] of [['market', market], ['kind', kind], ['state', state], ['platform', platform], ['sort', sort], ['limit', limit], ['cursor', cursor]]){
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
  }
  return getJson('/api/discover?' + query.toString(), options);
}

export function fetchRadar(market, kind, options){
  const query = new URLSearchParams({market});
  if (kind) query.set('kind', kind);
  return getJson('/api/discover/radar?' + query.toString(), options);
}

export function fetchTopic(itemId, market, options){
  return getJson('/api/topics/' + encodeURIComponent(itemId) + '?' + new URLSearchParams({market}).toString(), options);
}

export function fetchCoverage(date, options){
  return getJson('/api/coverage' + (date ? '?date=' + encodeURIComponent(date) : ''), options);
}

/* Contract section 11. Lists go comma-joined, as the contract writes them;
   each mode sends only the parameters it reads. */
export function fetchCompare({mode, items = [], market, markets = [], platforms = [], days}, options){
  const join = (values) => values.map(encodeURIComponent).join(',');
  let path = '/api/compare?mode=' + encodeURIComponent(mode) + '&items=' + join(items);
  if (mode === 'markets') path += '&markets=' + join(markets);
  else path += '&market=' + encodeURIComponent(market);
  if (mode === 'platforms') path += '&platforms=' + join(platforms);
  return getJson(path + '&days=' + encodeURIComponent(days), options);
}

/* Contract section 12. A community is found in any market when no market is
   given; empty history filters are left off the query. */
export function fetchCreator(creatorId, market, options){
  return getJson('/api/creators/' + encodeURIComponent(creatorId) + '?' + new URLSearchParams({market}).toString(), options);
}

export function fetchCommunities(market, options){
  return getJson('/api/communities?' + new URLSearchParams({market}).toString(), options);
}

export function fetchCommunity(communityId, market, options){
  return getJson('/api/communities/' + encodeURIComponent(communityId) + (market ? '?' + new URLSearchParams({market}).toString() : ''), options);
}

/* Contract section 16. The app only adds to the list and reads it; there is
   no lift. A hide answers 201 with the row and `matched`. */
export function hidePerson(body, options){
  return postJson('/api/suppressions', body, options);
}

export function listHidden(options){
  return getJson('/api/suppressions', options);
}

export function fetchHistoryItem(itemId, market, options){
  return getJson('/api/history/items/' + encodeURIComponent(itemId) + '?' + new URLSearchParams({market}).toString(), options);
}

function historyQuery(pairs){
  const query = new URLSearchParams();
  for (const [key, value] of pairs){
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
  }
  const text = query.toString();
  return text ? '?' + text : '';
}

export function searchHistory(q, market, options){
  return getJson('/api/history/search' + historyQuery([['q', q], ['market', market]]), options);
}

export function fetchHistoryAsks({limit, before, market} = {}, options){
  return getJson('/api/history/asks' + historyQuery([['limit', limit], ['before', before], ['market', market]]), options);
}

export function fetchHistoryBriefs({limit, before, market} = {}, options){
  return getJson('/api/history/briefs' + historyQuery([['limit', limit], ['before', before], ['market', market]]), options);
}

export function fetchHistoryFindings({itemId, status, market} = {}, options){
  return getJson('/api/history/findings' + historyQuery([['item_id', itemId], ['status', status], ['market', market]]), options);
}

/* Contract section 10.5. The writes go to f42-agent through f42-api. */
export function listWatches(options){
  return getJson('/api/watches', options);
}

export function createWatch(body, options){
  return postJson('/api/watches', body, options);
}

export function pauseWatch(watchId, options){
  return postJson('/api/watches/' + encodeURIComponent(watchId) + '/pause', undefined, options);
}

export function resumeWatch(watchId, options){
  return postJson('/api/watches/' + encodeURIComponent(watchId) + '/resume', undefined, options);
}

export function fetchAlerts(date, options){
  return getJson('/api/alerts' + (date ? '?date=' + encodeURIComponent(date) : ''), options);
}

/* Contract section 10.6. A tap only tells reviewers where to look. */
export function sendFeedback(body, options){
  return postJson('/api/feedback', body, options);
}

/* Contract section 13.2. The server owns every claim, so an edit sends only
   which claims to keep, their order, the title and the notes; anything else
   it is handed stays here. */
export function createDossier(askId, options){
  return postJson('/api/dossiers', {from: {ask_id: askId}}, options);
}

export function saveFinding(askId, options){
  return postJson('/api/findings', {from: {ask_id: askId}}, options);
}

export function listDossiers({limit, before} = {}, options){
  const query = new URLSearchParams();
  if (limit) query.set('limit', String(limit));
  if (before) query.set('before', before);
  return getJson('/api/dossiers?' + query.toString(), options);
}

const dossierPath = (dossierId) => '/api/dossiers/' + encodeURIComponent(dossierId);

export function getDossier(dossierId, options){
  return getJson(dossierPath(dossierId), options);
}

export function getDossierVersion(dossierId, version, options){
  return getJson(dossierPath(dossierId) + '/versions/' + encodeURIComponent(version), options);
}

/* fromVersion is the version the change was made from; the server refuses
   it with 409 stale_version once another tab has added a later one. */
export function updateDossier(dossierId, {keep, order, title, notes, fromVersion}, {signal} = {}){
  return request(dossierPath(dossierId), {method: 'PUT', body: {keep, order, title, notes, from_version: fromVersion}, signal});
}

export function tickClaim(dossierId, claimId, ticked, options){
  return postJson(dossierPath(dossierId) + '/ticks', {claim_id: claimId, ticked}, options);
}

export function freezeDossier(dossierId, fromVersion, options){
  return postJson(dossierPath(dossierId) + '/freeze', {from_version: fromVersion}, options);
}

/* Frozen versions only; the file is fetched with the passcode header and
   handed to the browser as a download, named as the server names it. The
   blob URL is released a minute after the click, because some browsers lose
   the file if it goes before the download has begun; a download that fails
   before the click releases it at once. */
const EXPORT_URL_LIFETIME_MS = 60000;
const exportUrlTimers = new Set();

export async function downloadDossierExport(dossierId, version, format){
  const res = await send(dossierPath(dossierId) + '/versions/' + encodeURIComponent(version) + '/export?format=' + encodeURIComponent(format));
  const url = URL.createObjectURL(await res.blob());
  try {
    const link = document.createElement('a');
    link.href = url;
    link.download = '42-dossier-' + String(dossierId).replace(/[^A-Za-z0-9_-]/g, '') + '-v' + Number(version) + '.' + format;
    link.rel = 'noopener';
    document.body.appendChild(link);
    link.click();
    link.remove();
  } catch (error){
    URL.revokeObjectURL(url);
    throw error;
  }
  const timer = setTimeout(() => {
    exportUrlTimers.delete(timer);
    URL.revokeObjectURL(url);
  }, EXPORT_URL_LIFETIME_MS);
  exportUrlTimers.add(timer);
}

/* Contract section 13.1. Every refusal (429 when today's model budget is
   spent, 409 naming ASK_DAILY or MODEL_DAILY_USD or an unknown spend, 503
   before the T3 agent exists) comes back as the error's own message. */
const investigationPath = (investigationId) => '/api/investigations/' + encodeURIComponent(investigationId);

export function createInvestigation({question, market, angles}, options){
  const body = {question, market: market || null};
  if (Array.isArray(angles) && angles.length) body.angles = angles;
  return postJson('/api/investigations', body, options);
}

export function updateInvestigationPlan(investigationId, plan, {signal} = {}){
  return request(investigationPath(investigationId) + '/plan', {method: 'PUT', body: {plan}, signal});
}

export function startInvestigation(investigationId, options){
  return postJson(investigationPath(investigationId) + '/start', undefined, options);
}

export function stopInvestigation(investigationId, options){
  return postJson(investigationPath(investigationId) + '/stop', undefined, options);
}

export function getInvestigation(investigationId, options){
  return getJson(investigationPath(investigationId), options);
}

export function listInvestigations({status} = {}, options){
  return getJson('/api/investigations' + (status ? '?status=' + encodeURIComponent(status) : ''), options);
}

export function createInvestigationDossier(investigationId, options){
  return postJson('/api/dossiers', {from: {investigation_id: investigationId}}, options);
}

/* Contract section 14.2. The writes go to f42-agent through f42-api; each
   refusal comes back as the error's own message. */
const schedulePath = (scheduleId) => '/api/schedules/' + encodeURIComponent(scheduleId);

export function listSchedules(options){
  return getJson('/api/schedules', options);
}

export function createSchedule({question, market, tier, cadence, deliver}, options){
  return postJson('/api/schedules', {question, market, tier, cadence, deliver}, options);
}

export function pauseSchedule(scheduleId, options){
  return postJson(schedulePath(scheduleId) + '/pause', undefined, options);
}

export function resumeSchedule(scheduleId, options){
  return postJson(schedulePath(scheduleId) + '/resume', undefined, options);
}

/* How a schedule's last run ended, in the words Today and the Schedules
   screen share. A refused or thin answer says so; a skip names its reason. */
const OUTCOME_WORDS = {partial: 'Partial answer', insufficient_evidence: 'Not enough evidence', refused: 'Refused'};
export function scheduledRunWords(run){
  const r = run || {};
  if (r.status === 'skipped') return r.outcome ? 'Skipped, ' + r.outcome : 'Skipped';
  if (r.status === 'failed') return 'Failed';
  if (r.status === 'stopped') return 'Stopped early';
  if (r.status === 'running') return 'Running';
  // A started row (core/api/scheduled.py) means the ask began but no answer was recorded: never "Answered".
  if (r.status === 'started') return 'Started, no answer recorded yet';
  if (r.status !== 'complete' && r.status !== 'ok') return 'No answer recorded';
  return OUTCOME_WORDS[r.outcome] || 'Answered';
}

/* Contract section 14.1. Answers with the ask's 202, whose ask_id the Ask
   page then follows; a gap day is refused with 400 "No measurement that day". */
export function askSpike({item_id, market, date, series}, options){
  const body = {item_id, market, date};
  if (series) body.series = series;
  return postJson('/api/spikes', body, options);
}

/* Contract section 15.1. A skin write sends only the contract's fields;
   every refusal (the word check, an account not on the approved list, the
   word check not installed) comes back as the error's own message. */
const skinPath = (skinId) => '/api/skins/' + encodeURIComponent(skinId);
const skinBody = ({skin_key, name, markets, terms, hashtags, accounts, watch_ids, template}) => (
  {skin_key, name, markets, terms, hashtags, accounts, watch_ids, template}
);

export function listSkins(options){
  return getJson('/api/skins', options);
}

export function getSkin(skinId, options){
  return getJson(skinPath(skinId), options);
}

export function createSkin(body, options){
  return postJson('/api/skins', skinBody(body), options);
}

export function updateSkin(skinId, body, {signal} = {}){
  return request(skinPath(skinId), {method: 'PUT', body: skinBody(body), signal});
}

export function archiveSkin(skinId, options){
  return postJson(skinPath(skinId) + '/archive', undefined, options);
}

export function getSkinToday(skinId, date, options){
  return getJson(skinPath(skinId) + '/today' + (date ? '?date=' + encodeURIComponent(date) : ''), options);
}

/* Answers with an investigation draft (section 13.1); Start stays on the
   investigation page. */
export function startSkinReport(skinId, options){
  return postJson(skinPath(skinId) + '/report', undefined, options);
}

/* Contract sections 6 and 15.2. An ask with an optional tier and skill;
   answers with the ask's 202, whose ask_id the Ask page then follows. */
export function askQuestion({question, market, tier, skill}, options){
  const body = {question, market: market || null};
  if (tier) body.tier = tier;
  if (skill) body.skill = skill;
  return postJson('/api/ask', body, options);
}

const POLL_MS = 2000;

const pause = (ms, signal) => new Promise((resolve, reject) => {
  const onAbort = () => { clearTimeout(timer); reject(signal.reason || new Error('aborted')); };
  const timer = setTimeout(() => { if (signal) signal.removeEventListener('abort', onAbort); resolve(); }, ms);
  if (signal) signal.addEventListener('abort', onAbort, {once: true});
});

/* The live log, read as Ask's is (contract section 6, Live progress): the
   event stream through fetch so the passcode header goes with it, parsed by
   Ask's parser, and resolved with the done event's data. If the stream
   cannot open, ends early or breaks, the investigation is polled instead,
   passing on the steps not yet seen, until it stops running; that resolves
   with {status, investigation}. A 401 is thrown, never polled. */
export async function streamInvestigation(investigationId, onEvent, options = {}){
  const {signal, pollMs = POLL_MS} = options;
  let lastEventId = options.lastEventId != null ? String(options.lastEventId) : null;
  let lastSeq = lastEventId != null && Number.isFinite(Number(lastEventId)) ? Number(lastEventId) : 0;
  let done = null;
  const aborted = () => signal && signal.aborted;

  try {
    const headers = {'X-Passcode': storedValue(PASS_KEY), Accept: 'text/event-stream'};
    if (lastEventId != null) headers['Last-Event-ID'] = lastEventId;
    const res = await fetch(investigationPath(investigationId) + '/events', {headers, signal});
    if (res.status === 401) throw await failure(res);
    if (res.ok && res.body){
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      const parser = createSSEParser((message) => {
        if (done) return;
        let payload = null;
        try { payload = JSON.parse(message.data); } catch (_error){ return; }
        if (message.id != null){
          lastEventId = message.id;
          if (Number.isFinite(Number(message.id))) lastSeq = Math.max(lastSeq, Number(message.id));
        }
        if (message.event === 'done') done = payload;
        onEvent({type: message.event, id: message.id, data: payload});
      });
      try {
        while (!done){
          const {value, done: ended} = await reader.read();
          if (ended) break;
          parser.push(decoder.decode(value, {stream: true}));
        }
      } finally {
        if (done && reader.cancel) reader.cancel().catch(() => {});
      }
      if (done) return done;
    }
  } catch (error){
    if (error && error.auth) throw error;
    if (aborted()) throw error;
  }

  for (;;){
    if (aborted()) throw signal.reason || new Error('aborted');
    const investigation = await getInvestigation(investigationId, {signal});
    const steps = investigation && investigation.record && Array.isArray(investigation.record.steps) ? investigation.record.steps : [];
    for (const step of steps){
      if (typeof step.seq === 'number' && step.seq > lastSeq){
        lastSeq = step.seq;
        onEvent({type: 'step', id: String(step.seq), data: step});
      }
    }
    if (investigation.status !== 'running') return {status: investigation.status, investigation};
    await pause(pollMs, signal);
  }
}
