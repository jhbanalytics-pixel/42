import {apiGetFresh, apiPost} from './api.js';

export const CHAT_WAIT_MS = 240000;
/* A submission the service never confirmed may still have started, so none of
   these resends by itself. Each question carries its own idempotency key, and
   the server joins a resubmission under that key to the request it already
   admitted, which is what makes the Retry these offer safe to press. */
export const SUBMISSION_UNCERTAIN = 'This question may already have started, but the service never confirmed it. Retry sends it again as the same request, so it cannot start a second one.';
export const SERVICE_TEMPORARY = 'The question service had a temporary problem, so this question may not have started. Retry sends it again as the same request, so it cannot start a second one.';
export const CONNECTION_LOST = 'The connection ended before the service confirmed this question, so it may already have started. Retry sends it again as the same request, so it cannot start a second one.';
export const POLICY_REVIEW_LAPSED = 'The question was not admitted. Nothing was written. The service\'s pricing review has lapsed and needs renewing before questions can be answered.';
/* The service refused to take the earlier answer as this follow-up's context,
   for example because it was asked under another client lens. The refusal
   comes at admission, before anything is written. */
export const FOLLOW_UP_CONTEXT_REFUSED = 'This follow-up could not take the earlier answer as its context, so nothing was started. Start a new question to ask it on its own.';
const FOLLOW_UP_REFUSALS = new Set(['parent_reference_invalid', 'parent_context_unavailable', 'parent_context_invalid', 'parent_context_conflict', 'parent_alias_invalid']);
export const REPLY_UNCERTAIN = 'The reply is not confirmed yet. Check the existing request without submitting another question.';

function failure(message, code, job, status){
  const error = new Error(message);
  error.code = code;
  if (job) error.job = job;
  if (Number.isInteger(status)) error.status = status;
  return error;
}

function aborted(){ const error = new Error('Stopped waiting'); error.aborted = true; return error; }

async function boundedRead(signal, milliseconds, request){
  if (signal?.aborted) throw aborted();
  const controller = new AbortController();
  const stop = () => controller.abort();
  signal?.addEventListener('abort', stop, {once: true});
  const timer = setTimeout(stop, milliseconds);
  try { return await request(controller.signal); }
  finally { clearTimeout(timer); signal?.removeEventListener('abort', stop); }
}

function pause(signal, milliseconds){
  return new Promise(resolve => {
    if (signal?.aborted) { resolve(); return; }
    const stop = () => { clearTimeout(timer); signal?.removeEventListener('abort', stop); resolve(); };
    const timer = setTimeout(stop, milliseconds);
    signal?.addEventListener('abort', stop, {once: true});
  });
}

export async function submitChat(message, history, market, signal, references, clientLensId, idempotencyKey){
  if (signal?.aborted) throw aborted();
  const payload = {message, history, market};
  if (references?.parent_request_id != null || references?.thread_anchor_request_id != null){
    payload.parent_request_id = references?.parent_request_id ?? null;
    payload.thread_anchor_request_id = references?.thread_anchor_request_id ?? null;
  }
  /* General 42 is the default and names no lens, so an empty selection sends no
     field at all. A named lens is a request the server still has to authorize. */
  if (typeof clientLensId === 'string' && clientLensId) payload.client_lens_id = clientLensId;
  if (typeof idempotencyKey === 'string' && idempotencyKey) payload.idempotency_key = idempotencyKey;
  let response;
  try { response = await boundedRead(signal, 30000, child => apiPost('/api/chat/send', payload, child)); }
  catch (error){
    if (signal?.aborted) throw aborted();
    if (error.auth || error.code === 'capacity_busy') throw error;
    /* The server names this only when the admit child stopped at the policy
       check, before anything was written; the wording is ours, not the body's. */
    if (error.code === 'policy_review_lapsed') throw failure(POLICY_REVIEW_LAPSED, 'policy_review_lapsed', null, error.status);
    if ((error.status === 400 || error.status === 409) && payload.parent_request_id && FOLLOW_UP_REFUSALS.has(error.message)) throw failure(FOLLOW_UP_CONTEXT_REFUSED, error.message, null, error.status);
    if (error.status >= 400 && error.status < 500 && error.status !== 408) throw failure('The request was not accepted. Check the question and your access.', error.code || 'request_rejected', null, error.status);
    throw failure(Number.isInteger(error.status) ? SERVICE_TEMPORARY : CONNECTION_LOST, 'submission_uncertain', null, error.status);
  }
  if (typeof response?.job_id !== 'string' || !response.job_id.trim()) throw failure(SUBMISSION_UNCERTAIN, 'submission_uncertain');
  return {id: response.job_id, deadlineAt: Date.now() + CHAT_WAIT_MS};
}

/* The rendered response digest the status read named in its header, kept
   beside the exact response object it came with so a delivery report can only
   quote the digest of the response this page rendered. */
const servedDigests = new WeakMap();
const DIGEST = /^[0-9a-f]{64}$/;
const REQUEST_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const ASSET_VERSION = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
export const DELIVERY_PATH = '/api/internal/v2/question-delivery';

function servedDigest(res){
  try {
    const value = res?.headers?.get?.('X-Question-Response-Digest');
    return typeof value === 'string' && DIGEST.test(value) ? value : null;
  } catch (_error){ return null; }
}

/* The content hashed file name of the module the page loaded, which is the
   served asset version; anything outside the server grammar reads as unversioned. */
export function servedAssetVersion(url = import.meta.url){
  try {
    const name = new URL(url).pathname.split('/').pop() || '';
    return ASSET_VERSION.test(name) ? name : 'unversioned';
  } catch (_error){ return 'unversioned'; }
}

export function deliveryReport(response, eventType, now = new Date()){
  const digest = response && typeof response === 'object' ? servedDigests.get(response) : undefined;
  const requestId = response?.lifecycle?.request_id;
  if (!digest || typeof requestId !== 'string' || !REQUEST_ID.test(requestId)) return null;
  if (eventType !== 'rendered' && eventType !== 'render_failed') return null;
  return {request_id: requestId, response_digest: digest, asset_version: servedAssetVersion(), event_type: eventType, client_event_at: now.toISOString()};
}

/* Advisory telemetry after an answer is rendered. It never retries and never
   changes the turn: a report that is not sent leaves delivery unknown. */
export async function reportDelivery(response, eventType = 'rendered'){
  const report = deliveryReport(response, eventType);
  if (!report) return false;
  try { await apiPost(DELIVERY_PATH, report); return true; }
  catch (_error){ return false; }
}

/* A delivery waits here, keyed by the turn that will render it, until that
   turn is committed. Nothing is persisted, so a turn restored from storage has
   no held delivery and reports nothing. */
const heldDeliveries = new Map();

export function holdDelivery(turnId, response){
  if (typeof turnId !== 'string' || !turnId) return false;
  if (!deliveryReport(response, 'rendered')) { heldDeliveries.delete(turnId); return false; }
  heldDeliveries.set(turnId, response);
  return true;
}

/* Called once the turn is committed; sends at most one report per held reply. */
export function sendHeldDelivery(turnId, eventType){
  const response = heldDeliveries.get(turnId);
  if (!response) return false;
  heldDeliveries.delete(turnId);
  reportDelivery(response, eventType);
  return true;
}

export async function pollChat(job, signal){
  if (typeof job?.id !== 'string' || !job.id.trim() || !Number.isFinite(job.deadlineAt)) throw failure(REPLY_UNCERTAIN, 'reply_uncertain', job);
  const path = '/api/chat/status?job_id=' + encodeURIComponent(job.id);
  let first = true;
  while (first || Date.now() < job.deadlineAt){
    first = false;
    if (signal?.aborted) throw aborted();
    try {
      const remaining = job.deadlineAt - Date.now();
      let digest = null;
      const response = await boundedRead(signal, remaining > 0 ? Math.min(25000, remaining) : 25000, child => apiGetFresh(path + '&_=' + Date.now(), child, undefined, res => { digest = servedDigest(res); }));
      if (response && typeof response === 'object' && !Array.isArray(response) && response.pending !== true
        && (typeof response.answer === 'string' || response.error === true || Object.hasOwn(response, 'intelligence'))){
        if (digest) servedDigests.set(response, digest);
        return response;
      }
    } catch (error){
      if (signal?.aborted) throw aborted();
      if (error.auth) throw error;
      if (error.status === 404 || error.status === 403) throw failure('The existing request could not be read. Its outcome is unconfirmed.', 'reply_uncertain', job, error.status);
    }
    if (Date.now() >= job.deadlineAt) break;
    await pause(signal, Math.min(1500, job.deadlineAt - Date.now()));
  }
  throw failure(REPLY_UNCERTAIN, 'reply_uncertain', job);
}

export function terminalChatTurn(response){
  const structured = Object.hasOwn(response, 'intelligence');
  return {
    pending: false, statusUncertain: false, error: response.error === true,
    content: typeof response.answer === 'string' ? response.answer : 'The request ended without a usable answer.',
    sources: Array.isArray(response.sources) ? response.sources : [],
    ...(structured ? {intelligence: response.intelligence} : {}),
    reason: typeof response.reason === 'string' ? response.reason : null,
  };
}
