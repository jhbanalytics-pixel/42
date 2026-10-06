/* The guard that stops a question leaving under an unresolved lens lives inside
   send, and three of the four ways to reach send do not consult the disabled
   state of the Send button. A rendered string cannot exercise any of them, so
   this file drives the real console in a real document: it types, presses Enter,
   clicks a starter chip, and changes the hash, then asks what was submitted. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, mock, test} from 'bun:test';
import React from 'react';

/* Static imports are evaluated before the first statement of this module, and
   the renderer decides at load time whether a document exists. The document has
   to be registered first, so everything that reads one is imported after. */
GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');

const realApi = {...(await import('../../api.js'))};
const realTransport = {...(await import('../../chatTransport.js'))};

const lensApi = {state: 'loading', data: null};
const submitted = [];

mock.module('../../api.js', () => ({
  ...realApi,
  useApi: (path) => [path === '/api/chat/lenses' ? lensApi : {state: 'loading'}, () => {}],
  apiGetFresh: () => new Promise(() => {}),
  apiPost: () => new Promise(() => {}),
}));

mock.module('../../chatTransport.js', () => ({
  ...realTransport,
  submitChat: (...args) => { submitted.push(args); return new Promise(() => {}); },
  pollChat: () => new Promise(() => {}),
}));

const {ChatPage} = await import('../../chat.jsx');
const {buildWorkbenchHash} = await import('../../workbenchRoute.js');

afterAll(() => {
  mock.module('../../api.js', () => realApi);
  mock.module('../../chatTransport.js', () => realTransport);
  GlobalRegistrator.unregister();
});

const LENS = 'bsa_pulse_lens';
const DIGEST = 'e1baafa865b5c9419225102d752c651c5b6e4fada034a2317f2d6f8a395336bf';
const roster = {
  contract_version: 'client_lens_roster_v1',
  default_client_lens_id: null,
  client_scope_id: 'bsa_pulse',
  lenses: [{client_lens_id: LENS, label: 'Brand South Africa Pulse', configuration_digest: DIGEST}],
};

let host = null;
let root = null;

beforeEach(() => {
  submitted.length = 0;
  try { window.localStorage.clear(); } catch (_error) { /* nothing stored */ }
  host = document.createElement('div');
  document.body.appendChild(host);
});

afterEach(() => {
  if (root) flushSync(() => root.unmount());
  root = null;
  if (host) host.remove();
  host = null;
  window.location.hash = '#/console';
});

function openConsoleAt(hash, api){
  lensApi.state = api.state;
  lensApi.data = api.data ?? null;
  window.location.hash = hash;
  root = createRoot(host);
  flushSync(() => {
    root.render(
      <ChatPage region="ZA" session="test" onAuth={() => {}} initialThreadId={null} />,
    );
  });
}

function typeInto(field, value){
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLTextAreaElement.prototype, 'value',
  ).set;
  setter.call(field, value);
  flushSync(() => {
    field.dispatchEvent(new Event('input', {bubbles: true}));
  });
}

function pressEnter(field){
  flushSync(() => {
    field.dispatchEvent(new window.KeyboardEvent('keydown', {
      key: 'Enter', bubbles: true, cancelable: true,
    }));
  });
}

/* Quiet register, 23 Sept 2026: the general starter names the market in
   words rather than as the code ZA (rule 16), so the chip is found by its new
   words. What these tests hold, that a starter is refused under an unresolved
   lens and sent under general 42 otherwise, is unchanged. */
const STARTER = 'What is moving in South Africa?';

function starterChip(){
  return [...host.querySelectorAll('button')]
    .find((node) => node.textContent.trim() === STARTER);
}

test('Enter and a starter chip are both refused while the named lens is unresolved', () => {
  for (const api of [
    {state: 'loading'},
    {state: 'error'},
    {state: 'ready', data: {...roster, lenses: []}},
  ]){
    openConsoleAt(buildWorkbenchHash({work: 'ask', clientLensId: LENS}), api);
    const field = host.querySelector('textarea');
    expect(field).toBeTruthy();

    /* The keyboard path never consults the Send button's disabled state, so this
       is the guard inside send or nothing. */
    typeInto(field, 'What is moving?');
    pressEnter(field);
    expect(submitted).toEqual([]);

    /* Quiet register, 23 Sept 2026: a starter chip fills the field rather
       than sending, so the question it offers is refused on the same Enter
       path. */
    const chip = starterChip();
    expect(chip).toBeTruthy();
    flushSync(() => { chip.click(); });
    expect(submitted).toEqual([]);
    expect(host.querySelector('textarea').value).toBe(STARTER);
    pressEnter(host.querySelector('textarea'));
    expect(submitted).toEqual([]);

    if (root) flushSync(() => root.unmount());
    root = null;
    host.remove();
    host = document.createElement('div');
    document.body.appendChild(host);
  }
});

test('an authorized lens deep link submits under that lens from the keyboard', () => {
  openConsoleAt(buildWorkbenchHash({work: 'ask', clientLensId: LENS}), {state: 'ready', data: roster});
  const field = host.querySelector('textarea');
  typeInto(field, 'What is moving?');
  pressEnter(field);
  expect(submitted.length).toBe(1);
  /* submitChat(text, prior, market, signal, references, clientLensId) */
  expect(submitted[0][0]).toBe('What is moving?');
  expect(submitted[0][5]).toBe(LENS);
});

/* Quiet register, 23 Sept 2026: a starter chip fills the field and the
   reader sends it, so the chip is followed by Enter. What the test holds, that
   the question leaves as general 42, is unchanged. */
test('a console naming no lens submits as general 42 from a starter chip', () => {
  openConsoleAt(buildWorkbenchHash({work: 'ask'}), {state: 'ready', data: roster});
  flushSync(() => { starterChip().click(); });
  expect(submitted).toEqual([]);
  pressEnter(host.querySelector('textarea'));
  expect(submitted.length).toBe(1);
  expect(submitted[0][0]).toBe(STARTER);
  expect(submitted[0][5]).toBe('');
});

test('the mounted console follows a later hashchange rather than its opening lens', () => {
  openConsoleAt(buildWorkbenchHash({work: 'ask'}), {state: 'ready', data: roster});
  expect(host.innerHTML).toContain('data-client-lens="general"');

  window.location.hash = buildWorkbenchHash({work: 'ask', clientLensId: LENS});
  flushSync(() => {
    window.dispatchEvent(new window.Event('hashchange'));
  });
  expect(host.innerHTML).toContain(`data-client-lens="${LENS}"`);

  /* And the question that leaves after the move carries the lens it moved to. */
  const field = host.querySelector('textarea');
  typeInto(field, 'What changed?');
  pressEnter(field);
  expect(submitted.length).toBe(1);
  expect(submitted[0][5]).toBe(LENS);
});

test('a hashchange to a lens this console cannot resolve closes the console again', () => {
  openConsoleAt(buildWorkbenchHash({work: 'ask'}), {state: 'ready', data: {...roster, lenses: []}});
  const field = host.querySelector('textarea');
  typeInto(field, 'What is moving?');
  pressEnter(field);
  expect(submitted.length).toBe(1);

  submitted.length = 0;
  window.location.hash = buildWorkbenchHash({work: 'ask', clientLensId: LENS});
  flushSync(() => {
    window.dispatchEvent(new window.Event('hashchange'));
  });
  expect(host.innerHTML).toContain('data-client-lens-state="unauthorized"');
  const after = host.querySelector('textarea');
  typeInto(after, 'What is moving?');
  pressEnter(after);
  expect(submitted).toEqual([]);
});


/* Choosing a lens is the fourth way into the console's configuration, and the
   value the handler is given comes from the document rather than from the
   roster. The console must follow the roster, not the control. */
function lensSelect(){
  return host.querySelector('#workbench-client-lens');
}

function chooseValue(select, value){
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLSelectElement.prototype, 'value',
  ).set;
  setter.call(select, value);
  flushSync(() => {
    select.dispatchEvent(new window.Event('change', {bubbles: true}));
  });
}

test('the lens control moves the route only to a lens the roster authorizes', () => {
  openConsoleAt(buildWorkbenchHash({work: 'ask'}), {state: 'ready', data: roster});
  const select = lensSelect();
  expect(select).toBeTruthy();

  /* An authorized choice moves the console, and the next ask carries it. */
  chooseValue(select, LENS);
  expect(window.location.hash).toContain('lens=' + LENS);
  flushSync(() => {
    window.dispatchEvent(new window.Event('hashchange'));
  });
  expect(host.innerHTML).toContain(`data-client-lens="${LENS}"`);
  /* Quiet register, 23 Sept 2026: the chip fills the field, and Enter sends. */
  flushSync(() => { starterChip().click(); });
  expect(submitted).toEqual([]);
  pressEnter(host.querySelector('textarea'));
  expect(submitted.length).toBe(1);
  expect(submitted[0][5]).toBe(LENS);

  /* A value the roster does not carry is not a choice. The control is the only
     thing that supplied it, so the console normalises it back to general 42
     rather than writing an unauthorized configuration onto its own link. */
  submitted.length = 0;
  const rogue = document.createElement('option');
  rogue.value = 'other_lens';
  rogue.textContent = 'Not authorized';
  lensSelect().appendChild(rogue);
  chooseValue(lensSelect(), 'other_lens');
  expect(window.location.hash).not.toContain('other_lens');
  flushSync(() => {
    window.dispatchEvent(new window.Event('hashchange'));
  });
  expect(host.innerHTML).toContain('data-client-lens="general"');
  expect(host.innerHTML).not.toContain('data-client-lens-state=');
});
