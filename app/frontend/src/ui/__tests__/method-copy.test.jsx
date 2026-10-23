/* Audit W26-06 and W26-07: Method sends each kind of topic to the page that
   holds it, and describes share of voice as the comparison Brand lens makes. */
import {expect, test} from 'bun:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {MethodPage} from '../../views.jsx';

const html = renderToStaticMarkup(<MethodPage />);
const text = html.replace(/<[^>]+>/g, ' ').replace(/&#x27;/g, "'").replace(/\s+/g, ' ');

test('Method names where shown, held and not-assessed topics go, and links to Today for the last', () => {
  expect(text).not.toMatch(/the rest stay in Discover/);
  expect(text).toMatch(/held back[^.]*Discover/i);
  expect(text).toMatch(/not assessed[^.]*Today/i);
  expect(text).toContain('Not assessed');
  expect(html).toMatch(/<a [^>]*href="#\/pulse"[^>]*>[^<]*Not assessed/);
});

test('Method describes share of voice within the chosen brands, per platform and window, with the five-post floor', () => {
  expect(text).not.toMatch(/adding up to 100 across that market/);
  expect(text).toMatch(/competitors/);
  expect(text).toMatch(/platform/);
  expect(text).toMatch(/window/);
  expect(text).toMatch(/fewer than 5 posts/);
  expect(text).toMatch(/not of all conversation in the market/);
});
