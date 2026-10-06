import {expect, test} from 'bun:test';
import {renderToStaticMarkup} from 'react-dom/server';
import React from 'react';
import {ClientLensSelector, GENERAL_LENS_LABEL, clientLensRoster} from '../ClientLensSelector.jsx';
import {buildWorkbenchHash, parseWorkbenchRoute} from '../../workbenchRoute.js';
import {submitChat} from '../../chatTransport.js';

const LENS = 'bsa_pulse_lens';
const DIGEST = 'e1baafa865b5c9419225102d752c651c5b6e4fada034a2317f2d6f8a395336bf';
const roster = {
  contract_version: 'client_lens_roster_v1',
  default_client_lens_id: null,
  client_scope_id: 'bsa_pulse',
  lenses: [{client_lens_id: LENS, label: 'Brand South Africa Pulse', configuration_digest: DIGEST}],
};

/* The Console shows the lens it is asking under, and general 42 is what it
   shows until an authorized lens is chosen. A roster the server did not
   authorize, or one it could not return, leaves the general default alone. */
test('the roster keeps general 42 first and admits only authorized entries', () => {
  expect(clientLensRoster(roster)).toEqual([
    {clientLensId: '', label: GENERAL_LENS_LABEL, configurationDigest: null},
    {clientLensId: LENS, label: 'Brand South Africa Pulse', configurationDigest: DIGEST},
  ]);
  for (const broken of [null, {}, {lenses: null}, {...roster, contract_version: 'other_v1'},
    {...roster, lenses: [{client_lens_id: LENS}]},
    {...roster, lenses: [{client_lens_id: '', label: 'x', configuration_digest: DIGEST}]},
    {...roster, lenses: [{client_lens_id: LENS, label: 'x', configuration_digest: 'short'}]}]){
    expect(clientLensRoster(broken)).toEqual([
      {clientLensId: '', label: GENERAL_LENS_LABEL, configurationDigest: null},
    ]);
  }
});

test('the selector is visible, defaults to general 42 and names the bound configuration', () => {
  const general = renderToStaticMarkup(
    <ClientLensSelector roster={roster} value="" onChange={() => {}} />,
  );
  /* Ask redesign, 23 Sept 2026: the select sits beside Send. Quiet register,
     23 Sept 2026: its visible label names what is chosen, a client lens, and
     the default reads None rather than the internal "General 42" (rule 16). */
  expect(general).toContain('>Client lens</label>');
  expect(GENERAL_LENS_LABEL).toBe('None');
  expect(general).not.toContain('General 42');
  expect(general).toContain(GENERAL_LENS_LABEL);
  expect(general).toContain('Brand South Africa Pulse');
  expect(general).toContain('value="" selected');
  expect(general).not.toContain(DIGEST);

  const bound = renderToStaticMarkup(
    <ClientLensSelector roster={roster} value={LENS} onChange={() => {}} />,
  );
  expect(bound).toContain(`value="${LENS}" selected`);
  expect(bound).toContain(DIGEST.slice(0, 12));
});

test('an unauthorized selection never renders as chosen', () => {
  const markup = renderToStaticMarkup(
    <ClientLensSelector roster={roster} value="invented_lens" onChange={() => {}} />,
  );
  expect(markup).not.toContain('invented_lens');
  expect(markup).toContain('value="" selected');
});

test('the console hash carries the lens so a reload keeps its binding', () => {
  const hash = buildWorkbenchHash({work: 'ask', clientLensId: LENS});
  expect(hash).toBe('#/console?work=ask&lens=' + LENS);
  expect(parseWorkbenchRoute(hash).clientLensId).toBe(LENS);
  expect(parseWorkbenchRoute('#/console?work=ask').clientLensId).toBeNull();
  expect(parseWorkbenchRoute('#/console?work=ask&lens=').clientLensId).toBeNull();
  expect(buildWorkbenchHash({work: 'ask'})).toBe('#/console?work=ask');
  expect(parseWorkbenchRoute(buildWorkbenchHash({work: 'brief', clientLensId: LENS})).clientLensId).toBe(LENS);
});

function capturingFetch(seen){
  return async (path, options) => {
    seen.push({path, body: JSON.parse(options.body)});
    return {ok: true, status: 202, headers: {get: () => 'application/json'}, json: async () => ({job_id: 'chat_x'})};
  };
}

test('a question sends the chosen lens and sends none under general 42', async () => {
  const seen = [];
  const original = globalThis.fetch;
  globalThis.fetch = capturingFetch(seen);
  try {
    await submitChat('Play Your Part?', [], 'za', null, null, LENS);
    await submitChat('Play Your Part?', [], 'za', null, null, '');
    await submitChat('Play Your Part?', [], 'za', null, null, null);
  } finally {
    globalThis.fetch = original;
  }
  expect(seen.map((row) => row.path)).toEqual(['/api/chat/send', '/api/chat/send', '/api/chat/send']);
  expect(seen[0].body.client_lens_id).toBe(LENS);
  expect(Object.hasOwn(seen[1].body, 'client_lens_id')).toBe(false);
  expect(Object.hasOwn(seen[2].body, 'client_lens_id')).toBe(false);
});
