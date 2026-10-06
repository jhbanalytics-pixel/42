/* Round three, 25 Sept 2026: the red command style drew its buttons 35 to 37px
   tall. The Ask panel's Retry and Back to Today and the Refine bar under a
   generated brief are mounted by no route the browser suite can open, so
   their markup is read here. Each is the app's 48px action now: the Ask
   panel's one action keeps its primary look, and Refine, its suggestions
   and its field are secondary to the brief above them. */
import {expect, test} from 'bun:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {AskLoadingPanel} from '../../ask.jsx';
import {RefineBar} from '../../views.jsx';

const button = (markup, name) => {
  const found = [...markup.matchAll(/<button([^>]*)>([\s\S]*?)<\/button>/g)].find((match) => match[2] === name);
  expect(found, `${name} is rendered`).toBeTruthy();
  return found[1];
};

test('the Ask panel Retry and Back to Today are the 48px primary action', () => {
  const capacity = renderToStaticMarkup(<AskLoadingPanel query="weekend repair" error={{code: 'capacity_busy'}} region="ZA" onClose={() => {}} onRetry={() => {}} />);
  const failed = renderToStaticMarkup(<AskLoadingPanel query="weekend repair" error region="ZA" onClose={() => {}} onRetry={() => {}} />);
  for (const attributes of [button(capacity, 'Retry'), button(failed, 'Back to Today')]){
    expect(attributes).toContain('class="legacy-action legacy-action--primary"');
    expect(attributes).not.toContain('cmd-go');
  }
});

test('the brief Refine bar is secondary and every control in it is a 48px target', () => {
  const markup = renderToStaticMarkup(<RefineBar t={{id: 'topic_1', generated: true}} onUpdate={() => {}} />);
  expect(button(markup, 'Refine')).toContain('class="legacy-action"');
  for (const name of ['Sharper PR angle', 'Alternative mechanic', 'Stronger evidence challenge']){
    expect(button(markup, name)).toContain('class="legacy-chip"');
  }
  expect(markup).not.toContain('cmd-go');
  const css = readFileSync(fileURLToPath(new URL('../../app.css', import.meta.url)), 'utf8');
  expect(css).toMatch(/\.refine-row input \{[^}]*min-height: 48px;/);
});
