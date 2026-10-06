/* Restated 2 October 2026: the retired page printed a funding lane key in a
   held row. The roster carries series, status and group keys in its payload;
   none of them may reach what a reader sees. The laneWords test at the foot
   still guards the shared helper. */

import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

import {FieldworkPage} from '../../fieldwork.jsx';
import {laneWords} from '../../plainLabels.js';

const days = JSON.parse(readFileSync(new URL('./fixtures/fieldwork42_days.json', import.meta.url), 'utf8'));
const decode = (html) => html.replace(/&#x27;/g, "'").replace(/&quot;/g, '"').replace(/&amp;/g, '&');
/* What a reader sees: text only, disclosures included. */
const surface = (html) => decode(html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' '));

test('the fixture still carries the keys, so the page is what keeps them off the screen', () => {
  const keys = days.ready.markets.flatMap((m) => m.sources.map((s) => s.series));
  expect(keys).toContain('panel_ig_gossip');
  expect(keys).toContain('board_tiktok_hashtag');
});

test('no series, status or group key reaches the reader', () => {
  for (const payload of [days.ready, days.absent]){
    const seen = surface(renderToStaticMarkup(<FieldworkPage payload={payload} />));
    const keys = new Set(payload.markets.flatMap((m) => m.sources.flatMap((s) => [s.series, s.status, s.group])));
    for (const key of keys) if (key.includes('_')) expect(seen).not.toContain(key);
    expect(seen).not.toMatch(/\b[a-z]+_[a-z_]+\b/);
    expect(seen).not.toMatch(/\u2014|\u2013|--/);
  }
});

test('a source name is stated as the API wrote it', () => {
  const payload = JSON.parse(JSON.stringify(days.ready));
  payload.markets[0].sources[0].name = 'Two source families disagree';
  expect(surface(renderToStaticMarkup(<FieldworkPage payload={payload} />))).toContain('Two source families disagree');
});

test('lane words replace only whole known keys and never double the word lane', () => {
  expect(laneWords('the ogilvy_funded lane is exhausted')).toBe('the Ogilvy funded lane is exhausted');
  expect(laneWords('ogilvy_funded exhausted')).toBe('Ogilvy funded lane exhausted');
  expect(laneWords('Lane: jhb_core.')).toBe('Lane: JHB core.');
  expect(laneWords('the lane ogilvy_funded is exhausted')).toBe('the lane Ogilvy funded is exhausted');
  expect(laneWords('ogilvy_funded lanes')).toBe('Ogilvy funded lanes');
  expect(laneWords('ogilvy_funded Lane')).toBe('Ogilvy funded Lane');
  expect(laneWords('ogilvy_funded lane-level cap')).toBe('Ogilvy funded lane-level cap');
  expect(laneWords('SOCIALCRAWL_CREDENTIAL_LANE=ogilvy_funded and path/ogilvy_funded and cfg.ogilvy_funded')).toBe('SOCIALCRAWL_CREDENTIAL_LANE=ogilvy_funded and path/ogilvy_funded and cfg.ogilvy_funded');
  expect(laneWords('ogilvy_funded=on and ogilvy_funded/x')).toBe('ogilvy_funded=on and ogilvy_funded/x');
  expect(laneWords('paid by jhb_analytics')).toBe('paid by JHB Analytics account');
  expect(laneWords('x_ogilvy_funded and ogilvy_funded_2 and ogilvy_funded-v2')).toBe('x_ogilvy_funded and ogilvy_funded_2 and ogilvy_funded-v2');
  expect(laneWords('no key here')).toBe('no key here');
  expect(laneWords(null)).toBe(null);
});
