import {test, expect} from 'bun:test';
import {buildWorkbenchHash, parseWorkbenchRoute} from '../../workbenchRoute.js';

test('saved brief identity survives its URL', () => {
  const state = {work: 'brief', investigationId: 'inv_fixture', artifactId: 'ra_fixture'};
  const parsed = parseWorkbenchRoute(buildWorkbenchHash(state));
  expect(parsed.investigationId).toBe(state.investigationId);
  expect(parsed.artifactId).toBe(state.artifactId);
});

/* The same control for the two other identity-bearing console routes the
   manifest names: a stored question and a persona entry. Each identity is
   parsed back exactly, and the work state is the one that was built. */
test('stored question identity survives its URL', () => {
  const state = {work: 'ask', requestId: '00000000-0000-4000-8000-000000000001'};
  const parsed = parseWorkbenchRoute(buildWorkbenchHash(state));
  expect(parsed.work).toBe('ask');
  expect(parsed.requestId).toBe(state.requestId);
  expect(parsed.error).toBeNull();
});

test('persona entry identity survives its URL', () => {
  const state = {work: 'brief', personaId: 'audience_neutral', markets: ['KE', 'ng']};
  const parsed = parseWorkbenchRoute(buildWorkbenchHash(state));
  expect(parsed.work).toBe('brief');
  expect(parsed.personaId).toBe(state.personaId);
  expect(parsed.markets).toEqual(['ke', 'ng']);
});
