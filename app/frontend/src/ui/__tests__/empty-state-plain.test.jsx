/* Quiet register, 23 Sept 2026, rulebook rules 10, 12 and 20. The shared
   empty state was a centred card: a frozen loader above a centred title, the
   package's panel fill behind it in midnight. It is now a plain passage that
   starts at the column's left edge with no fill, saying what happened and
   what to do next. These tests hold the markup and the rules that draw it,
   and that the loader, with its status role, shows only while a fetch is in
   flight. */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {renderToStaticMarkup} from 'react-dom/server';

import {EmptyState} from '../../parts.jsx';

const appCss = readFileSync(fileURLToPath(new URL('../../app.css', import.meta.url)), 'utf8');

const ruleBody = (selector) => {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = appCss.match(new RegExp(`(?:^|\\n)${escaped}\\s*\\{([^}]*)\\}`));
  return match ? match[1] : null;
};

test('a settled empty state is a plain block with its words, actions and no loader', () => {
  const markup = renderToStaticMarkup(
    <EmptyState
      loader="drum"
      isLoading={false}
      title="Nothing heard yet"
      body="No posts matched this market in the window. Widen the window or choose another market."
      status={['South Africa', 'Last 30 days']}
      actions={[{label: 'Open Discover', href: '#/explore', primary: true}, {label: 'Try again', onClick: () => {}}]}
    />,
  );
  expect(markup).toMatch(/^<div class="es-block es-plain es-drum">/);
  expect(markup).toContain('<h2 class="es-title">Nothing heard yet</h2>');
  expect(markup).toContain('<p class="es-body">No posts matched');
  expect(markup).toContain('<a class="es-act" href="#/explore">Open Discover</a>');
  expect(markup).toContain('<button type="button" class="es-act-ghost">Try again</button>');
  /* The frozen loader is gone from a settled state, and so is any status
     role that would announce a wait. */
  expect(markup).not.toContain('es-loader');
  expect(markup).not.toContain('ld-drum');
  expect(markup).not.toContain('role="status"');
});

test('the loader shows, and announces itself, only while a fetch is in flight', () => {
  const drum = renderToStaticMarkup(<EmptyState loader="drum" isLoading title="Reading the market" />);
  expect(drum).toContain('class="es-loader"');
  expect(drum).toContain('role="status"');
  expect(drum).toContain('aria-label="Loading"');

  const sweep = renderToStaticMarkup(<EmptyState loader="sweep" field="Scanning signals" isLoading title="Reading the market" />);
  expect(sweep).toContain('class="es-field"');
  expect(sweep).toContain('Scanning signals');
  expect(sweep).toContain('role="status"');

  const none = renderToStaticMarkup(<EmptyState loader={false} isLoading title="Reading the market" />);
  expect(none).toMatch(/^<div class="es-block es-plain">/);
  expect(none).not.toContain('role="status"');
});

test('the plain block starts at the left edge with no fill, outranking the centred and midnight rules', () => {
  const block = ruleBody('html .es-block.es-plain');
  expect(block).not.toBeNull();
  expect(block).toMatch(/text-align: start;/);
  expect(block).toMatch(/background: transparent;/);
  expect(ruleBody('html .es-plain .es-copy')).toMatch(/padding: 24px 0 32px;/);
  expect(ruleBody('html .es-plain .es-body')).toMatch(/margin: 0 0 16px;/);
  expect(ruleBody('html .es-plain .es-loader')).toMatch(/justify-content: flex-start;/);
  expect(ruleBody('html .es-plain .es-status,\nhtml .es-plain .es-actions')).toMatch(/justify-content: flex-start;/);
  expect(appCss).not.toMatch(/\.es-plain[^{]*\{[^}]*text-align: center/);
});

test('the action is the same paper control in both themes, answering the pointer in the accent', () => {
  const night = ruleBody('html[data-dir="midnight"] .es-plain .es-act');
  expect(night).not.toBeNull();
  expect(night).toMatch(/background: transparent;/);
  expect(night).toMatch(/color: var\(--ink\);/);
  const answer = ruleBody('html[data-dir="midnight"] .es-plain .es-act:hover,\nhtml[data-dir="midnight"] .es-plain .es-act:focus-visible');
  expect(answer).toMatch(/color: var\(--accent-text\);/);
  expect(answer).toMatch(/border-color: var\(--accent\);/);
});
