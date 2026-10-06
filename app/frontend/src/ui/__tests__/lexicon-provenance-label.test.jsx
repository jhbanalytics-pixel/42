/* Loading uses the package state frame. Route metadata uses the approved
   label role through the route stylesheet. */
import {expect, test} from 'bun:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {readFileSync} from 'node:fs';
import postcss from 'postcss';

import {DecodePanel, LexiconPage} from '../../lexicon.jsx';

/* useApi opens in its loading state and only leaves it inside an effect, and
   a static render runs no effects, so this is the loading frame. */
const markup = renderToStaticMarkup(<LexiconPage region="ZA" session="test" onAuth={() => {}} />);

test('the lexicon loading line renders and names what the route is doing', () => {
  expect(markup).toContain('Reading the lexicon');
});

test('Lexicon loading preserves the shared state frame and provenance disclosure', () => {
  expect(markup).toContain('data-state-frame="loading"');
  expect(markup).toContain('state-view');
  expect(markup).toContain('Details');
});

/* Quiet register, 23 Sept 2026: the metadata line is a label a reader reads,
   so it is set in the sans at 14px in sentence case with no tracking, and the
   MONO axis is turned off rather than left to the shared mono rule. */
test('Lexicon metadata explicitly declares the label role and the sans axis', () => {
  const css = postcss.parse(readFileSync(new URL('../../styles/lexlisten.css', import.meta.url), 'utf8'));
  const declarations = {};
  css.walkRules((rule) => {
    if (rule.selector === '.lexicon-page .lex-meta') rule.walkDecls((decl) => { declarations[decl.prop] = decl.value; });
  });
  expect(declarations.font).toBe('400 var(--type-3)/1.4 var(--sans)');
  expect(declarations['letter-spacing']).toBeUndefined();
  expect(declarations['text-transform']).toBeUndefined();
  expect(declarations['font-variation-settings']).toContain('"MONO" 0');
});

test('Lexicon says what the page is in plain words, with no engine and no detector', () => {
  /* Shell consistency, 2 October 2026: the old headline leads the sentence.
     Page port, 3 October 2026: the page now lists the words and hashtags 42
     records in one market over the 28 days its read covers, so the sentence
     says that instead of the desk's 30 day slang index.
     UX pass, 3 October 2026: each page's line under its title leads with the question the page answers (pageQuestions.js). */
  expect(markup).toContain('Which words and hashtags are people using? Counted in one market over the last 28 days. Pick one to see the posts that use it.');
  expect(markup).not.toMatch(/engine|detector|archive/);
});

test('Lexicon draws no empty decode box before a term is picked', () => {
  expect(renderToStaticMarkup(<DecodePanel region="ZA" term={null} session="test" onAuth={() => {}} />)).toBe('');
});

test('a picked term names its market and window in words, not a code and 30d', () => {
  const html = renderToStaticMarkup(<DecodePanel region="ZA" term="piano" entry={{term: 'piano', market: 'ZA', n: 2}} session="test" onAuth={() => {}} />);
  /* Page port, 3 October 2026: the read covers 28 days, as Seed path does. */
  expect(html).toContain('South Africa · last 28 days');
  expect(html).not.toContain('30d');
  expect(html).not.toMatch(/>Decode/);
});

test('the Seed path link says "this term" only when a term is picked', () => {
  /* Demo polish, 2 October 2026: with no term picked the link offered
     "this term" and opened Seed path with nothing to trace. */
  expect(markup).toContain('Trace a term in Seed path');
  expect(markup).not.toContain('Trace this term');
  const picked = renderToStaticMarkup(<LexiconPage region="ZA" term="amapiano" session="test" onAuth={() => {}} />);
  expect(picked).toContain('Trace this term in Seed path');
});
