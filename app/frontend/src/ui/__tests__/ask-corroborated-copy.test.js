/* Ask's "What you get back" defines corroborated the way the claim checks do
   (core/agent/checks.py, TRUST.md): different people on two platforms, or
   three unrelated people behind a counted figure. "Two platforms" alone
   made a one-platform answer's Corroborated badges look wrong (live audit F03). */
import {expect, test} from 'bun:test';

test('the onboarding copy states both routes to corroborated', async () => {
  const source = await Bun.file(new URL('../../ask42.jsx', import.meta.url)).text();
  expect(source).not.toContain('from corroborated on two platforms to inferred');
  expect(source).toContain('different people on two platforms, or three unrelated people behind a counted figure');
});
