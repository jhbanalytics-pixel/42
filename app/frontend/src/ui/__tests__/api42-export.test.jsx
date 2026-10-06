/* The dossier export hands the browser a blob URL and clicks a link to it.
   Some browsers lose the file if that URL is revoked straight after the
   click, before the download has started, so the URL is released about a
   minute later. A download that fails part way still revokes the URL it
   made. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';

GlobalRegistrator.register();

const api = await import('../../api42.js');

const realFetch = globalThis.fetch;
const realWindowFetch = window.fetch;
const realCreate = URL.createObjectURL;
const realRevoke = URL.revokeObjectURL;
const realSetTimeout = globalThis.setTimeout;
const realAnchorClick = window.HTMLAnchorElement.prototype.click;

let revoked = [];
let timers = [];
let downloads = [];

afterAll(() => {
  GlobalRegistrator.unregister();
});

beforeEach(() => {
  revoked = [];
  timers = [];
  downloads = [];
  window.localStorage.setItem('pulse_passcode', 'fixture-pass');
  URL.createObjectURL = () => 'blob:dossier';
  URL.revokeObjectURL = (url) => { revoked.push(url); };
  globalThis.setTimeout = (callback, delay) => { timers.push({callback, delay}); return timers.length; };
  window.HTMLAnchorElement.prototype.click = function click(){ downloads.push(this.download); };
});

afterEach(() => {
  globalThis.fetch = realFetch;
  window.fetch = realWindowFetch;
  URL.createObjectURL = realCreate;
  URL.revokeObjectURL = realRevoke;
  globalThis.setTimeout = realSetTimeout;
  window.HTMLAnchorElement.prototype.click = realAnchorClick;
});

function respond(response){
  const stub = async () => response;
  globalThis.fetch = stub;
  window.fetch = stub;
}

const pdf = {ok: true, status: 200, headers: {get: () => 'application/pdf'}, blob: async () => new Blob(['%PDF'], {type: 'application/pdf'})};

test('the export URL is not revoked when the download starts, only once the timer runs', async () => {
  respond(pdf);
  const result = await api.downloadDossierExport('d_fixture01', 2, 'pdf');
  expect(result).toBeUndefined();
  expect(downloads).toEqual(['42-dossier-d_fixture01-v2.pdf']);
  expect(revoked).toEqual([]);
  expect(timers.length).toBe(1);
  expect(timers[0].delay).toBeGreaterThanOrEqual(30000);
  timers[0].callback();
  expect(revoked).toEqual(['blob:dossier']);
});

test('a refused export makes no URL, revokes nothing and keeps the server error', async () => {
  respond({ok: false, status: 409, headers: {get: () => 'application/json'}, json: async () => ({error: 'not_frozen', message: 'Only a frozen version exports.'})});
  let caught = null;
  try { await api.downloadDossierExport('d_fixture01', 1, 'pdf'); } catch (error){ caught = error; }
  expect(caught.status).toBe(409);
  expect(caught.code).toBe('not_frozen');
  expect(caught.message).toBe('Only a frozen version exports.');
  expect(revoked).toEqual([]);
  expect(timers).toEqual([]);
});

test('a download that fails after the URL is made still revokes it and rethrows', async () => {
  respond(pdf);
  window.HTMLAnchorElement.prototype.click = () => { throw new Error('click refused'); };
  let caught = null;
  try { await api.downloadDossierExport('d_fixture01', 2, 'pdf'); } catch (error){ caught = error; }
  expect(caught.message).toBe('click refused');
  for (const timer of timers) timer.callback();
  expect(revoked).toEqual(['blob:dossier']);
});
