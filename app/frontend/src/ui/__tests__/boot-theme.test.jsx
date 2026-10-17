/* The first paint (index.html) and App agree on the theme. index.html sets
   data-dir before the bundle runs, so a visitor with no stored choice, a
   blocked store or a value App does not take must start on the same theme
   App then mirrors (App.jsx storedTheme), or the page flashes one theme and
   switches to the other when the bundle evaluates. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';

GlobalRegistrator.register();

const {storedThemePreference, resolveTheme} = await import('../../App.jsx');

const INDEX = readFileSync(new URL('../../../index.html', import.meta.url), 'utf8');
const STATIC_DIR = INDEX.match(/<html\b[^>]*\bdata-dir="([^"]+)"/)[1];
const BOOT = [...INDEX.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]).find((s) => s.includes('oi-theme'));

afterAll(() => { GlobalRegistrator.unregister(); });

/* Runs the boot script against a page that starts with index.html's own
   attribute, a store holding stored (or throwing) and a system scheme. */
function firstPaint(stored, systemDark){
  const attrs = {'data-dir': STATIC_DIR};
  const doc = {documentElement: {setAttribute: (name, value) => { attrs[name] = value; }}};
  const store = {getItem: () => { if (stored === THROWS) throw new Error('blocked'); return stored; }};
  const win = {matchMedia: () => ({matches: systemDark})};
  new Function('localStorage', 'window', 'document', 'matchMedia', BOOT)(store, win, doc, win.matchMedia);
  return attrs['data-dir'];
}

const THROWS = Symbol('throws');

/* What App mirrors for the same store and scheme. */
function appTheme(stored, systemDark){
  if (stored === THROWS) return resolveTheme('midnight', systemDark);
  if (stored === null) localStorage.removeItem('oi-theme');
  else localStorage.setItem('oi-theme', stored);
  try {
    return resolveTheme(storedThemePreference(), systemDark);
  } finally {
    localStorage.removeItem('oi-theme');
  }
}

test('index.html ships a boot script that reads the stored theme', () => {
  expect(typeof BOOT).toBe('string');
  expect(STATIC_DIR).toBe('midnight');
});

for (const stored of [null, 'midnight', 'daylight', 'system', 'bogus', '', THROWS]){
  for (const systemDark of [false, true]){
    const name = stored === THROWS ? 'a blocked store' : JSON.stringify(stored);
    test(`first paint matches App for ${name} with a ${systemDark ? 'dark' : 'light'} system`, () => {
      expect(firstPaint(stored, systemDark)).toBe(appTheme(stored, systemDark));
    });
  }
}
