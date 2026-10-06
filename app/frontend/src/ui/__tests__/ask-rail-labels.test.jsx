import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {railSummaryForTopics} from '../../instrumentAdapters.js';
import {ROOT_SIGNAL, freshFixture} from './fixtures/instrument-payloads.js';

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');

/* The shell rail and the Ask page each name a window, and they are different
   windows: the rail's is the Briefing run's, the Ask page's is the one a
   question can be answered for. Each says which it is. */
/* Quiet register, 23 Sept 2026: the rail is read, not copied, so its window
   is written the way every other date on the page is ("18 to 24 Aug 2026")
   rather than as the ISO pair the payload carries. It still says whose window
   it is. */
test('the shell rail names its window as the Briefing window, in readable dates', () => {
  const summary = railSummaryForTopics([freshFixture(ROOT_SIGNAL)]);
  expect(summary.window).toBe('18 to 24 Aug 2026 (Briefing)');
  expect(summary.window).not.toMatch(/\d{4}-\d{2}-\d{2}/);
});

/* Ask redesign, 23 Sept 2026: the Ask window is stated once, in the page
   head under the Ask title, as the market and dates a question can be answered
   for. The rail no longer repeats it. */
test('the Ask page head names the covered market and dates from the coverage route', () => {
  const workbench = read('../../ConsoleWorkbench.jsx');
  expect(workbench).toContain('COVERAGE_PATH');
  expect(workbench).toMatch(/\{coverageLine\(askWindow, region\)\}/);
  expect(workbench).not.toMatch(/Ask window/);
});
