import {afterAll, afterEach, expect, mock, test} from 'bun:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

/* The console is what actually asks the question, so the lens has to be proved
   where it is used rather than only in the selector. A deep link naming a lens
   must not be answered as general 42 while the roster is unknown, the choice
   must keep the rest of the route, and a later hash change must resync. */

const realApi = {...(await import('../../api.js'))};
const lensState = {state: 'loading', data: null};

mock.module('../../api.js', () => ({
  ...realApi,
  useApi: (path) => [path === '/api/chat/lenses' ? lensState : {state: 'loading'}, () => {}],
  apiGetFresh: () => Promise.reject(new Error('not called during static render')),
  apiPost: () => Promise.reject(new Error('not called during static render')),
}));

afterAll(() => { mock.module('../../api.js', () => realApi); });

const {ChatPage, askIsBlocked, lensRoutePath, routeLensId, subscribeToRouteLens} = await import('../../chat.jsx');
const {buildWorkbenchHash, parseWorkbenchRoute} = await import('../../workbenchRoute.js');
const {resolveClientLensState} = await import('../ClientLensSelector.jsx');

const LENS = 'bsa_pulse_lens';
const DIGEST = 'e1baafa865b5c9419225102d752c651c5b6e4fada034a2317f2d6f8a395336bf';
const roster = {
  contract_version: 'client_lens_roster_v1',
  default_client_lens_id: null,
  client_scope_id: 'bsa_pulse',
  lenses: [{client_lens_id: LENS, label: 'Brand South Africa Pulse', configuration_digest: DIGEST}],
};

/* A static render still reads the hash, so the console gets one to read. The
   server render never attaches listeners, so a bare location is enough. */
const previousWindow = globalThis.window;
const fakeWindow = {
  location: {hash: '#/console'},
  addEventListener: () => {},
  removeEventListener: () => {},
};
globalThis.window = fakeWindow;

function openConsoleAt(hash, api){
  lensState.state = api.state;
  lensState.data = api.data ?? null;
  fakeWindow.location.hash = hash;
  return renderToStaticMarkup(
    <ChatPage region="ZA" session="test" onAuth={() => {}} initialThreadId={null} />,
  );
}

afterEach(() => { fakeWindow.location.hash = '#/console'; });
afterAll(() => { globalThis.window = previousWindow; });

test('a lens deep link is never answered as general 42 while the roster is unknown', () => {
  const hash = buildWorkbenchHash({work: 'ask', clientLensId: LENS});
  for (const [api, state] of [
    [{state: 'loading'}, 'pending'],
    [{state: 'error'}, 'unavailable'],
    [{state: 'ready', data: {...roster, lenses: []}}, 'unauthorized'],
  ]){
    const markup = openConsoleAt(hash, api);
    expect(markup).toContain(`data-client-lens-state="${state}"`);
    expect(markup).toContain('role="alert"');
    /* The send control is unusable while the named lens is unresolved, so no
       question can leave the console under the wrong configuration. */
    expect(markup).toContain('aria-label="Send"');
    expect(markup).toMatch(/<button[^>]*disabled[^>]*aria-label="Send"/);
  }
});

test('an authorized lens deep link opens under that lens and names its configuration', () => {
  const markup = openConsoleAt(
    buildWorkbenchHash({work: 'ask', clientLensId: LENS}),
    {state: 'ready', data: roster},
  );
  expect(markup).toContain(`data-client-lens="${LENS}"`);
  expect(markup).not.toContain('data-client-lens-state=');
  expect(markup).toContain(DIGEST.slice(0, 12));
});

test('a console naming no lens is general 42 and asks without refusal', () => {
  for (const api of [{state: 'loading'}, {state: 'error'}, {state: 'ready', data: roster}]){
    const markup = openConsoleAt(buildWorkbenchHash({work: 'ask'}), api);
    expect(markup).toContain('data-client-lens="general"');
    expect(markup).not.toContain('data-client-lens-state=');
  }
});

test('the lens state resolver refuses exactly the states the console refuses', () => {
  expect(resolveClientLensState('ready', roster, '').state).toBe('ready');
  expect(resolveClientLensState('loading', null, '').state).toBe('ready');
  expect(resolveClientLensState('loading', null, LENS).state).toBe('pending');
  expect(resolveClientLensState('error', null, LENS).state).toBe('unavailable');
  expect(resolveClientLensState('ready', roster, 'other_lens').state).toBe('unauthorized');
  expect(resolveClientLensState('ready', roster, LENS).lens.configurationDigest).toBe(DIGEST);
});

test('choosing a lens keeps every other field the route was carrying', () => {
  const carried = {
    work: 'ask',
    artifactId: 'art_0000000000000001',
    markets: ['za', 'ng'],
    personaId: 'persona_one',
    investigationId: 'inv_one',
  };
  const before = parseWorkbenchRoute(buildWorkbenchHash(carried));
  /* lensRoutePath is what the console navigates to when a lens is chosen. */
  const after = parseWorkbenchRoute('#' + lensRoutePath(before, LENS));
  expect(after.clientLensId).toBe(LENS);
  expect(after.markets).toEqual(['za', 'ng']);
  expect(after.artifactId).toBe(carried.artifactId);
  expect(after.personaId).toBe(carried.personaId);
  expect(after.investigationId).toBe(carried.investigationId);
  expect(after.work).toBe('ask');
  /* The defect: a build from the lens alone loses everything else on the route. */
  const discarded = parseWorkbenchRoute(buildWorkbenchHash({work: 'ask', clientLensId: LENS}));
  expect(discarded.markets).toBeNull();
  expect(discarded.artifactId).toBeNull();
});

/* The route is the console's identity, so the lens has to follow a later hash
   change rather than stay on the one the page opened with. The console reads it
   through this subscription, so these are the two halves of that mechanism. */
test('the lens the console reads comes from the route on every hash change', () => {
  fakeWindow.location.hash = buildWorkbenchHash({work: 'ask', clientLensId: LENS});
  expect(routeLensId()).toBe(LENS);
  fakeWindow.location.hash = buildWorkbenchHash({work: 'ask'});
  expect(routeLensId()).toBe('');
  fakeWindow.location.hash = '#not a route at all';
  expect(routeLensId()).toBe('');
});

test('the console subscribes to the route and lets the subscription go', () => {
  const added = [];
  const removed = [];
  const listening = {
    location: {hash: '#/console'},
    addEventListener: (name, handler) => added.push([name, handler]),
    removeEventListener: (name, handler) => removed.push([name, handler]),
  };
  const previous = globalThis.window;
  globalThis.window = listening;
  try {
    let resyncs = 0;
    const release = subscribeToRouteLens(() => { resyncs += 1; });
    expect(added.map(([name]) => name)).toEqual(['hashchange']);
    /* The handler the console registered is what reports the new lens. */
    listening.location.hash = buildWorkbenchHash({work: 'ask', clientLensId: LENS});
    added[0][1]();
    expect(resyncs).toBe(1);
    expect(routeLensId()).toBe(LENS);
    release();
    expect(removed).toEqual([['hashchange', added[0][1]]]);
  } finally {
    globalThis.window = previous;
  }
});

test('a console with no window subscribes to nothing and releases cleanly', () => {
  const previous = globalThis.window;
  globalThis.window = undefined;
  try {
    expect(routeLensId()).toBe('');
    expect(() => subscribeToRouteLens(() => {})()).not.toThrow();
  } finally {
    globalThis.window = previous;
  }
});

/* The send control is what stops a question leaving under the wrong
   configuration, and an unresolved lens has to close it on its own, not only
   when the draft happens to be empty. */
test('a drafted question cannot be sent while the named lens is unresolved', () => {
  expect(askIsBlocked({busy: false, draft: 'What is moving?', lensBlocked: true})).toBe(true);
  expect(askIsBlocked({busy: false, draft: 'What is moving?', lensBlocked: false})).toBe(false);
  expect(askIsBlocked({busy: false, draft: '   ', lensBlocked: false})).toBe(true);
  expect(askIsBlocked({busy: false, draft: '', lensBlocked: true})).toBe(true);
  /* A request already running stays stoppable whatever the lens is doing. */
  expect(askIsBlocked({busy: true, draft: '', lensBlocked: true})).toBe(false);
});
