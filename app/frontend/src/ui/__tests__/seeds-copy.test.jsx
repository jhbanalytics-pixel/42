/* The seeds route names no vendor tool. A seed is the topic a brand could
   plant a move on before it peaks, and the activation carries a surface label
   (visual, audio, video, live) rather than a product name.
   Page port, 3 October 2026: Seeds is now 42's own search queue, so the copy
   it must carry is the new lead, and the label it must give is the reader's
   name for how 42 picked a search, in place of the activation surface. */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {laneWords} from '../../seeds.jsx';

const source = readFileSync(fileURLToPath(new URL('../../seeds.jsx', import.meta.url)), 'utf8');

test('the seeds route source names no vendor tool', () => {
  for (const banned of [/nano\s*banana/i, /lyria/i, /google/i]) {
    expect(source).not.toMatch(banned);
  }
  /* UX pass, 3 October 2026: each page's line under its title leads with the question the page answers (pageQuestions.js). */
  expect(source).toMatch(/What will 42 search for next/);
  expect(source).not.toMatch(/Each one below is evidenced, timed/);
});

test('the lane renders as a reader label', () => {
  expect(laneWords('expansion')).toBe('Following what is rising');
  expect(laneWords('exploration')).toBe('Trying something new');
  expect(laneWords('anchor')).toBe('Re-checking what worked');
  expect(laneWords('placebo')).toBe('Other searches');
  expect(laneWords('')).toBe('Other searches');
  expect(laneWords(undefined)).toBe('Other searches');
});
