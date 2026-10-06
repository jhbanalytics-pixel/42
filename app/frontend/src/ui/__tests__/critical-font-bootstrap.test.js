import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';

const main = readFileSync(fileURLToPath(new URL('../../main.jsx', import.meta.url)), 'utf8');

test('the first application render waits for the three critical package faces and survives a failed request', () => {
  expect(main).toContain('document.fonts.load');
  expect(main).toContain('Newsreader');
  expect(main).toContain('Recursive Sans');
  expect(main).toContain('Recursive Mono');
  expect(main).toContain('.catch(() => undefined)');
  expect(main.indexOf('bootstrapCriticalFonts()')).toBeLessThan(main.indexOf('root.render(<App/>'));
});
