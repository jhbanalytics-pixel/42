/* The dossier working view as a resource the workbench reads by identity.

   Three rules shape it. The identity is the scope, the investigation and the
   exact dossier version, and the panel shows content only while the content's
   identity is the one asked for: the render that changes identity already
   shows nothing, before any read has answered. A superseded read is aborted,
   and if its answer arrives anyway it is dropped. An answer that names a
   different investigation or version than the one asked for is refused,
   because showing it would put one investigation's material under another's
   name. */
import {useCallback, useEffect, useState} from 'react';
import {apiGetFresh, apiPost} from './api.js';
import {SIGN_IN_NOT_ENROLLED, SIGN_IN_WRONG_DOMAIN, reviewRefusal} from './intelligenceDossierContract.js';

export const DOSSIER_WORKING_CONTRACT = 'dossier_working_v1';

function investigationPath(investigationId){
  return `/api/internal/v2/investigations/${encodeURIComponent(investigationId)}`;
}

export function dossierReadPath(investigationId, dossierVersion){
  const base = `${investigationPath(investigationId)}/dossier/working`;
  return dossierVersion ? `${base}?dossier_version=${encodeURIComponent(dossierVersion)}` : base;
}

export function dossierCacheKey({scope, investigationId, dossierVersion}){
  return [scope || 'workspace', investigationId || '', dossierVersion || 'current'].join('|');
}

/* The working read is internal: it needs the review session, which the
   server holds in its cookie, and the session's CSRF token in the header. */
export function readDossier({investigationId, dossierVersion, signal, csrfToken}){
  return apiGetFresh(dossierReadPath(investigationId, dossierVersion), signal, {'X-Review-CSRF': csrfToken || ''});
}

/* Only an exact version is cached: its content is immutable. The current
   version is a pointer and is read fresh every time. */
const MAX_CACHED = 20;
const cache = new Map();

export function clearDossierCache(){
  cache.clear();
}

function remember(key, value){
  if (cache.has(key)) cache.delete(key);
  if (cache.size >= MAX_CACHED) cache.delete(cache.keys().next().value);
  cache.set(key, value);
}

function mismatch(){
  return {code: 'identity_mismatch', words: 'The dossier that came back does not match the investigation or version this page asked for, so it is not shown.'};
}

function signInNeeded(){
  return {code: null, words: 'Sign in as a reviewer to open this dossier.'};
}

function readFailure(error){
  if (error && error.status === 404) return {code: error.code || null, words: 'This investigation or version is not available to you.'};
  if (error && error.status === 503) return {code: error.code || null, words: 'The dossier could not be read right now. Try again in a moment.'};
  return reviewRefusal(error);
}

export function useDossierResource({investigationId, dossierVersion, onAuth, csrfToken = null, onSessionLost, scope = 'workspace', read = readDossier} = {}){
  const key = investigationId ? dossierCacheKey({scope, investigationId, dossierVersion}) : null;
  const [entry, setEntry] = useState({key: null, status: 'idle', dossier: null, error: null});
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!key) return undefined;
    /* No review session, no working read: the panel asks for sign in. */
    if (!csrfToken){
      setEntry({key, status: 'signin', dossier: null, error: signInNeeded()});
      return undefined;
    }
    if (dossierVersion && cache.has(key)){
      setEntry({key, status: 'ready', dossier: cache.get(key), error: null});
      return undefined;
    }
    const controller = new AbortController();
    let live = true;
    setEntry({key, status: 'loading', dossier: null, error: null});
    Promise.resolve()
      .then(() => read({investigationId, dossierVersion, signal: controller.signal, csrfToken}))
      .then((payload) => {
        if (!live || controller.signal.aborted) return;
        const valid = payload && payload.contract_version === DOSSIER_WORKING_CONTRACT
          && payload.investigation_id === investigationId
          && typeof payload.dossier_version === 'string' && payload.dossier_version
          && (!dossierVersion || payload.dossier_version === dossierVersion);
        if (!valid){
          setEntry({key, status: 'error', dossier: null, error: mismatch()});
          return;
        }
        /* A current read is remembered under the exact version it turned
           out to be, so pinning that version on the URL reads nothing again. */
        remember(dossierCacheKey({scope, investigationId, dossierVersion: payload.dossier_version}), payload);
        setEntry({key, status: 'ready', dossier: payload, error: null});
      })
      .catch((error) => {
        if (!live || controller.signal.aborted || (error && error.name === 'AbortError')) return;
        if (error && error.auth){
          /* Access is gone, so nothing read under it is kept. */
          cache.clear();
          if (onAuth) onAuth();
          setEntry({key, status: 'auth', dossier: null, error: reviewRefusal(error)});
          return;
        }
        /* The server refuses the working read without a live review session
           and its token (403, or 400 for a missing or stale token). The
           session held here is then no longer good: it is ended and the
           panel asks for sign in again. */
        if (error && (error.status === 403 || error.status === 400)){
          cache.clear();
          if (onSessionLost) onSessionLost();
          setEntry({key, status: 'signin', dossier: null, error: signInNeeded()});
          return;
        }
        setEntry({key, status: 'error', dossier: null, error: readFailure(error)});
      });
    return () => {
      live = false;
      controller.abort();
    };
  }, [key, tick, csrfToken]); // eslint-disable-line react-hooks/exhaustive-deps

  const reload = useCallback(() => {
    if (key) cache.delete(key);
    setTick((value) => value + 1);
  }, [key]);

  /* Content is returned only under its own identity. On the render that
     changes identity the stored entry still belongs to the previous one, so
     it reads as loading with nothing shown. */
  if (!key) return {status: 'idle', dossier: null, error: null, reload};
  if (entry.key !== key) return {status: 'loading', dossier: null, error: null, reload};
  return {status: entry.status, dossier: entry.dossier, error: entry.error, reload};
}

/* The review session. Its CSRF token is held in memory only: never in a URL,
   in storage or in a dossier record. The session itself is the Secure,
   HttpOnly, SameSite cookie the server set, which this page cannot read. */
let reviewSessionState = null;
const sessionListeners = new Set();

export function currentReviewSession(){
  return reviewSessionState;
}

export function subscribeReviewSession(listener){
  sessionListeners.add(listener);
  return () => sessionListeners.delete(listener);
}

function setReviewSession(value){
  reviewSessionState = value;
  for (const listener of sessionListeners) listener(value);
}

export function endReviewSession(){
  cache.clear();
  setReviewSession(null);
}

/* Sign in is set up when the page names the identity provider's client id,
   which the server does only while review is configured. */
export function reviewSignInConfigured(){
  try {
    const meta = document.querySelector('meta[name="review-oauth-client-id"]');
    return Boolean(meta && meta.getAttribute('content'));
  } catch (_error){
    return false;
  }
}

/* Sign in: the server issues a nonce and a login CSRF token, the identity
   provider's flow returns a credential carrying that nonce, and the server
   binds the two into a session once. This desk does not mint or inspect
   credentials itself. */
export async function beginReviewerSignIn({post = apiPost} = {}){
  const login = await post('/api/internal/v2/dossier/review/login');
  if (!login || typeof login.nonce !== 'string' || typeof login.csrf_token !== 'string') throw new Error('The sign in could not be started. Try again.');
  return login;
}

export async function finishReviewerSignIn({credential, login, post = apiPost} = {}){
  if (typeof credential !== 'string' || !credential) throw new Error('The sign in was not completed.');
  let session;
  try {
    session = await post('/api/internal/v2/dossier/review/session', {credential, g_csrf_token: login.csrf_token});
  } catch (error){
    /* A valid account that is not on the reviewer list is refused with a
       role code; that is a fact about enrolment, said as such. */
    if (error && error.status === 403) throw new Error(error.message === 'hosted_domain_mismatch' ? SIGN_IN_WRONG_DOMAIN : SIGN_IN_NOT_ENROLLED);
    throw error;
  }
  if (!session || typeof session.csrf_token !== 'string' || typeof session.role !== 'string') throw new Error('The sign in could not be completed. Try again.');
  const roles = Array.isArray(session.roles) ? session.roles.filter((role) => typeof role === 'string') : [session.role];
  const value = {csrfToken: session.csrf_token, role: session.role, roles};
  setReviewSession(value);
  return value;
}

/* The whole flow with any credential source, for callers that hold one. */
export async function signInReviewer({obtainCredential, post = apiPost} = {}){
  if (typeof obtainCredential !== 'function') throw new Error('Reviewer sign in is not set up on this desk yet.');
  const login = await beginReviewerSignIn({post});
  const credential = await obtainCredential({nonce: login.nonce});
  return finishReviewerSignIn({credential, login, post});
}

/* The identity provider as this page loaded it, and the client id the server
   named for it. The provider script loads asynchronously, so either may be
   absent for a moment after the page opens. */
export function reviewSignInProvider(){
  try {
    return (window.google && window.google.accounts && window.google.accounts.id) || null;
  } catch (_error){
    return null;
  }
}

export function reviewSignInClientId(){
  try {
    const meta = document.querySelector('meta[name="review-oauth-client-id"]');
    return (meta && meta.getAttribute('content')) || null;
  } catch (_error){
    return null;
  }
}

/* The review, selection and export commands for one investigation. Commands
   carry the session's CSRF token in the header the server reads; the export
   read is the workspace read and takes the exact artifact version only. */
export function createReviewClient({post = apiPost, investigationId, csrfToken} = {}){
  const base = investigationPath(investigationId);
  const headers = {'X-Review-CSRF': csrfToken || ''};
  return {
    saveSelection: (body) => post(`${base}/dossier/selection`, body, undefined, headers),
    review: (body) => post(`${base}/dossier/review`, body, undefined, headers),
    prepare: (body) => post(`${base}/artifacts/prepare`, body, undefined, headers),
    readArtifact: ({artifactId, artifactVersion}) => post(
      `/api/v2/investigations/${encodeURIComponent(investigationId)}/artifacts/${encodeURIComponent(artifactId)}/read?artifact_version=${encodeURIComponent(artifactVersion)}`,
    ),
  };
}
