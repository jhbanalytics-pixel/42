/* Night Desk, 4 October 2026. The visual layer's behaviour: the Today poster
   marks the trend it names, Ask leads with its first sentence, and the
   layer keeps the house laws (square corners, token sizes, no new shadow). */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';

const {headlineParts, leadSentence} = await import('../../nightdesk.js');
const css = readFileSync(new URL('../../styles/nightdesk.css', import.meta.url), 'utf8');
const main = readFileSync(new URL('../../main.jsx', import.meta.url), 'utf8');

test('the poster marks the trend name it leads with, matched without case', () => {
  expect(headlineParts('#fixture_za_step is the biggest mover in South Africa.', '#Fixture_ZA_step'))
    .toEqual({before: '', term: '#fixture_za_step', after: ' is the biggest mover in South Africa.'});
  expect(headlineParts('A dance to Khanyisa jumped to 31 creators.', 'Khanyisa'))
    .toEqual({before: 'A dance to ', term: 'Khanyisa', after: ' jumped to 31 creators.'});
});

test('the poster stays plain text when the name is missing or absent', () => {
  expect(headlineParts('Nothing named here.', 'Khanyisa')).toEqual({before: 'Nothing named here.', term: '', after: ''});
  expect(headlineParts('Nothing named here.', '')).toEqual({before: 'Nothing named here.', term: '', after: ''});
  expect(headlineParts('Nothing named here.', undefined)).toEqual({before: 'Nothing named here.', term: '', after: ''});
});

test('the short answer splits after its first full sentence only', () => {
  expect(leadSentence('Khanyisa is the sound. It carries a dance.')).toEqual({lead: 'Khanyisa is the sound.', rest: ' It carries a dance.'});
  expect(leadSentence('One sentence only.')).toEqual({lead: 'One sentence only.', rest: ''});
  expect(leadSentence('Views rose 2.5 times in S.A. this week. Then more.')).toEqual({lead: 'Views rose 2.5 times in S.A. this week.', rest: ' Then more.'});
  expect(leadSentence('')).toEqual({lead: '', rest: ''});
});

test('the layer loads after the other deferred sheets and brings its own face', () => {
  expect(main.indexOf("import('./styles/nightdesk.css')")).toBeGreaterThan(main.indexOf("import('./styles/controls42.css')"));
  expect(css).toContain('url("../assets/fonts/archivo-variable.woff2")');
  expect(css).not.toMatch(/fonts\.googleapis|fonts\.gstatic|@import/);
});

test('the layer keeps corners square, casts no shadow and names no raw font size', () => {
  const body = css.replace(/\/\*[\s\S]*?\*\//g, '');
  expect(body).not.toMatch(/border(?:-[a-z-]+)?-radius\s*:\s*(?!\s|0\b)/);
  for (const m of body.matchAll(/box-shadow\s*:\s*([^;]+)/g)) expect(m[1].trim().startsWith('inset') || m[1].trim() === 'none').toBe(true);
  expect(body).not.toMatch(/font-size\s*:\s*[0-9.]+(px|rem|em)/);
});

test('heat and market marks are defined for both themes', () => {
  for (const theme of ['midnight', 'daylight']){
    const block = css.split('.oi-product[data-dir="' + theme + '"]')[1].split('}')[0];
    for (const token of ['--heat-0', '--heat-1', '--heat-2', '--heat-3', '--heat-4', '--heat-ink', '--mk-za', '--mk-ng', '--mk-ke']) expect(block).toContain(token + ':');
  }
});

// Design review, 4 October 2026: rank numerals only where the order is a rank.
test('rank numerals are drawn only on lists that declare a ranking, and Today declares one', () => {
  const body = css.replace(/\/\*[\s\S]*?\*\//g, '');
  const rules = [...body.matchAll(/([^{}]+)\{[^{}]*(?:counter-reset|counter-increment|counter\()[^{}]*\}/g)].map((m) => m[1].trim());
  expect(rules.length).toBeGreaterThan(0);
  for (const selector of rules) expect(selector).toContain('[data-ranked]');
  const today = readFileSync(new URL('../../today42.jsx', import.meta.url), 'utf8');
  expect(today).toMatch(/<ol className="t42-cards" data-ranked=""/);
});

test('the confidence meter gives each claim label its own step', () => {
  const steps = Object.fromEntries([...css.matchAll(/\.ask42-confidence\[data-label="([a-z_]+)"\] \{ --nd-sure: (\d+)%; \}/g)].map((m) => [m[1], Number(m[2])]));
  expect(steps).toEqual({corroborated: 100, observed: 75, single_source: 50, inferred: 25});
});

// Design review, 4 October 2026: the face is registered before the first render.
test('the Night Desk face is loaded with the critical faces, before the app renders', () => {
  expect(main).toContain("new FontFace('Archivo 42'");
  expect(main).toContain("archivo-variable.woff2?url");
  expect(main).toMatch(/bootstrapNightDeskFace\(\)\]\)/);
  expect(main.indexOf('bootstrapCriticalFonts()')).toBeLessThan(main.indexOf('root.render(<App/>'));
});

// Albert, 4 October 2026: "keep the old font". The condensed face is for headlines and figures only.
test('the layer leaves the house sans in place for the interface', () => {
  expect(css).not.toMatch(/--(font-sans|sans|oi-sans):/);
  expect(css).not.toMatch(/html body[^{]*\{[^}]*font-family/);
  expect(css).not.toMatch(/html \.rail42 \{[^}]*font-family/);
});
