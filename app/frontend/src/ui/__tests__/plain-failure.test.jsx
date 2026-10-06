/* Quiet register, 23 Sept 2026: a failed read on the brief flow printed the
   server's raw string in red beside a framed red Retry ("Missing fixture",
   "No configured research persona is served."). Rule 20 asks errors to say
   what happened and what to do, with codes behind Details, and rule 9 keeps
   red for the current item and the one primary action. */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {PlainFailure} from '../../behaviourScan.jsx';

const outsideDetails = (html) => html.replace(/<details[\s\S]*?<\/details>/g, '').replace(/<[^>]+>/g, ' ');

test('a failure says what happened in plain words and keeps the server reason and code behind Details', () => {
  const error = Object.assign(new Error('No configured research persona is served.'), {code: 'research_personas_unimplemented', status: 501});
  const html = renderToStaticMarkup(createElement(PlainFailure, {summary: 'The persona lenses could not be loaded.', error, onRetry: () => {}}));
  expect(html).toContain('role="alert"');
  expect(outsideDetails(html)).toContain('The persona lenses could not be loaded.');
  expect(outsideDetails(html)).not.toContain('No configured research persona is served.');
  expect(outsideDetails(html)).not.toContain('research_personas_unimplemented');
  expect(html).toMatch(/<details class="research-error-details"><summary>Details<\/summary><p>Reason given: No configured research persona is served\.<\/p><p>Code: <code>research_personas_unimplemented<\/code><\/p><\/details>/);
  expect(html).toContain('>Retry</button>');
});

test('a status with no body code shows the status, and nothing is guessed when neither was read', () => {
  const status = renderToStaticMarkup(createElement(PlainFailure, {summary: 'The behaviours could not be loaded.', error: Object.assign(new Error('Missing fixture'), {status: 501})}));
  expect(status).toContain('<code>http_501</code>');
  const bare = renderToStaticMarkup(createElement(PlainFailure, {summary: 'The behaviours could not be loaded.', error: {message: 'Behaviour scan failed.'}}));
  expect(bare).not.toContain('<code>');
  expect(bare).not.toContain('network_unreachable');
});

test('the brief flow renders its failed reads through the plain failure, never the raw message', () => {
  for (const file of ['behaviourScan.jsx', 'ResearchDocPanel.jsx']){
    const source = readFileSync(new URL('../../' + file, import.meta.url), 'utf8');
    expect(source, file).not.toMatch(/>\s*\{metaErr\}/);
    expect(source, file).not.toMatch(/role="alert">\s*\{err\}/);
  }
  const sheet = readFileSync(new URL('../../styles/console.css', import.meta.url), 'utf8');
  const red = sheet.match(/([^{}]*)\{\s*color: var\(--ogilvy-red-text\);\s*\}/g) || [];
  for (const rule of red){
    expect(rule).not.toMatch(/\.research-error[\s,{]/);
    expect(rule).not.toMatch(/\.research-inline-action[\s,{]/);
  }
});

/* A 501 means the server does not offer the feature, not that the read
   stumbled. The failure then says the feature is not available and offers no
   retry promise and no Retry, while the code stays behind Details. */
test('a 501 says the feature is not available, with no retry promise and no Retry', () => {
  const error = Object.assign(new Error('No configured research persona is served.'), {code: 'research_personas_unimplemented', status: 501});
  const html = renderToStaticMarkup(createElement(PlainFailure, {
    summary: 'The persona lenses could not be loaded. Try again in a moment.',
    unavailable: 'Persona lenses are not available here yet, so a brief cannot be shaped.',
    error,
    onRetry: () => {},
  }));
  const shown = outsideDetails(html);
  expect(shown).toContain('Persona lenses are not available here yet, so a brief cannot be shaped.');
  expect(shown).not.toMatch(/try again/i);
  expect(html).not.toContain('>Retry</button>');
  expect(html).toContain('<code>research_personas_unimplemented</code>');
  const other = renderToStaticMarkup(createElement(PlainFailure, {
    summary: 'The persona lenses could not be loaded. Try again in a moment.',
    unavailable: 'Persona lenses are not available here yet, so a brief cannot be shaped.',
    error: Object.assign(new Error('Service unavailable right now.'), {status: 503}),
    onRetry: () => {},
  }));
  expect(outsideDetails(other)).toContain('Try again in a moment.');
  expect(other).toContain('>Retry</button>');
});

test('the persona read names the unavailable case on both of its failure lines', () => {
  const source = readFileSync(new URL('../../ResearchDocPanel.jsx', import.meta.url), 'utf8');
  const uses = source.match(/<PlainFailure summary=\{PERSONAS_FAILED\}[^>]*\/>/g) || [];
  expect(uses).toHaveLength(2);
  for (const use of uses) expect(use).toContain('unavailable={PERSONAS_UNAVAILABLE}');
  const line = source.match(/const PERSONAS_UNAVAILABLE = '([^']+)'/);
  expect(line).toBeTruthy();
  expect(line[1]).toMatch(/not available/);
  expect(line[1]).not.toMatch(/try again/i);
});
