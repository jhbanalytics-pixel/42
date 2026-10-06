/* Round 17, the L10 row the sweep read as one hit on ten cells. Section 10
   forbids a machine token on a reader surface with no Details above it, and
   the listen and network empty states were handing their status line raw
   slots: a market code, an upper-cased sentiment constant, a category token
   the desk wrote and shouted counts. The sweep saw only ZA, because a fixture
   with no sentiment and no category filter never renders the two slots that
   would have raised beside it, and a run with a space in it clears the token
   shapes whatever it shouts.

   The status line is the provenance value role of section 5 and renders
   uppercase by its own rule, so what these tests hold is the words rather
   than the case. Every phrase a reader can meet on that line is written in
   one register, and the two routes read it rather than writing their own, so
   a later cut that drops one fails here rather than in a sweep. */
import {describe, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {renderToStaticMarkup} from 'react-dom/server';

import {EmptyState} from '../../parts.jsx';
import {EMPTY_STATE_FACTS, listenEmptyFacts, networkEmptyFacts, readerPhrase} from '../../instrumentRouteModels.js';

test('Network does not invent a shared window when none was supplied', () => {
  const facts = networkEmptyFacts({market: 'ZA', marketLabel: 'South Africa', topics: 1, voices: 1});
  expect(facts.join(' ')).not.toContain('days');
  expect(facts.join(' ')).not.toContain('null');
});

const listenSource = readFileSync(fileURLToPath(new URL('../../listen.jsx', import.meta.url)), 'utf8');
const networkSource = readFileSync(fileURLToPath(new URL('../../network.jsx', import.meta.url)), 'utf8');

/* The four shapes audit.mjs reads a machine token by, copied from the rule
   rather than described, so a fact that would raise in a sweep raises here. A
   run with whitespace in it is only read for an embedded snake_case token,
   which is the rule's own order. */
const SNAKE_TOKEN = /^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$/;
const CAPS_TOKEN = /^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$/;
const DOTTED_FIELD = /^[a-zA-Z_$][a-zA-Z0-9_$]*(?:\.[a-zA-Z_$][a-zA-Z0-9_$]*)+$/;
const CAMEL_FIELD = /^[a-z]+(?:[A-Z][a-z0-9]*)+$/;
const EMBEDDED_SNAKE = /\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b/g;

function machineTokenShape(text){
  const trimmed = String(text ?? '').trim();
  if (!trimmed) return null;
  if (!/\s/.test(trimmed)){
    if (SNAKE_TOKEN.test(trimmed)) return 'snake_case token';
    if (DOTTED_FIELD.test(trimmed)) return 'dotted field path';
    if (CAMEL_FIELD.test(trimmed) && trimmed.length > 3) return 'camelCase field name';
    if (CAPS_TOKEN.test(trimmed) && trimmed.length >= 2) return 'all caps constant';
  }
  const embedded = trimmed.match(EMBEDDED_SNAKE);
  return embedded && embedded.length ? 'snake_case token inside a sentence' : null;
}

/* A shouted phrase clears every token shape the moment it carries a space, so
   the sweep cannot see it and a reader still can. Reader register is the
   second half of the ruling and it is held separately: a fact reads as words,
   so no run of two or more letters is written in capitals. */
function shouts(text){
  return /\b[A-Z]{2,}\b/.test(String(text ?? ''));
}

const factsOf = (markup) => [...markup.matchAll(/<span class="es-fact">(?:.*?)<span>([^<]*)<\/span><\/span>/g)].map((m) => m[1]);

describe('the empty state status line carries reader phrases', () => {
  test('every listen slot is a reader phrase, not a token', () => {
    const facts = listenEmptyFacts({market: 'ZA', sentiment: 'positive', category: 'tiktok', loaded: 0});
    expect(facts.length).toBe(4);
    for (const fact of facts){
      expect(machineTokenShape(fact)).toBe(null);
      expect(shouts(fact)).toBe(false);
    }
    expect(facts[0]).toMatch(/South Africa/);
    expect(facts[1]).toMatch(/[Pp]ositive/);
    expect(facts[2]).toMatch(/[Tt]ik[Tt]ok|tiktok/i);
    expect(facts[3]).toMatch(/mentions/i);
  });

  test('a listen line with no filters on drops the filter slots and keeps the rest reader side', () => {
    const facts = listenEmptyFacts({market: 'ALL', sentiment: '', category: '', loaded: 0});
    expect(facts.length).toBe(2);
    for (const fact of facts){
      expect(machineTokenShape(fact)).toBe(null);
      expect(shouts(fact)).toBe(false);
    }
    expect(facts[0]).toMatch(/All markets/i);
  });

  test('every network slot is a reader phrase, not a token', () => {
    const facts = networkEmptyFacts({market: 'ZA', marketLabel: 'South Africa', topics: 12, voices: 8, days: 30});
    expect(facts.length).toBe(5);
    for (const fact of facts){
      expect(machineTokenShape(fact)).toBe(null);
      expect(shouts(fact)).toBe(false);
    }
    expect(facts[0]).toMatch(/South Africa/);
    expect(facts[1]).toMatch(/link/i);
    expect(facts[2]).toMatch(/\b12\b/);
    expect(facts[3]).toMatch(/\b8\b/);
    expect(facts[4]).toMatch(/30/);
  });

  test('a network line with nothing counted still says so in words', () => {
    const facts = networkEmptyFacts({market: 'ALL', marketLabel: 'All markets', topics: null, voices: null, days: 30});
    for (const fact of facts){
      expect(machineTokenShape(fact)).toBe(null);
      expect(shouts(fact)).toBe(false);
    }
    expect(facts.some((f) => /\bALL\b/.test(f))).toBe(false);
  });

  test('a category the desk invents tomorrow prints in words rather than in code', () => {
    expect(readerPhrase('sports_culture')).toBe('Sports culture');
    expect(readerPhrase('brand-moment')).toBe('Brand moment');
    expect(machineTokenShape(readerPhrase('sports_culture'))).toBe(null);
    expect(readerPhrase('tiktok')).toBe('TikTok');
    expect(readerPhrase('')).toBe('');
  });
});

describe('the register holds every phrase and the routes read it', () => {
  test('the register carries a row for every slot on both lines', () => {
    expect(Object.isFrozen(EMPTY_STATE_FACTS)).toBe(true);
    for (const key of ['market', 'sentiment', 'category', 'mentionsLoaded', 'linksDrawn', 'topicsOnBoard', 'voicesOnBoard', 'window']){
      expect(EMPTY_STATE_FACTS[key]).toBeDefined();
    }
  });

  test('neither route writes a status slot of its own', () => {
    expect(listenSource).not.toMatch(/MENTIONS LOADED/);
    expect(listenSource).not.toMatch(/sentiment\.toUpperCase\(\)/);
    expect(listenSource).toMatch(/listenEmptyFacts/);
    expect(networkSource).not.toMatch(/'0 EDGES'/);
    expect(networkSource).not.toMatch(/reg\.toUpperCase\(\)/);
    expect(networkSource).toMatch(/networkEmptyFacts/);
  });
});

describe('the rendered status line carries no machine token', () => {
  test('listen renders four reader facts', () => {
    const markup = renderToStaticMarkup(
      <EmptyState loader="drum" isLoading={false} title="Nothing heard" body="" status={listenEmptyFacts({market: 'ZA', sentiment: 'negative', category: 'tiktok', loaded: 0})} />,
    );
    const facts = factsOf(markup);
    expect(facts.length).toBe(4);
    for (const fact of facts) expect(machineTokenShape(fact)).toBe(null);
  });

  test('network renders five reader facts', () => {
    const markup = renderToStaticMarkup(
      <EmptyState loader="orbit" isLoading={false} title="No links" body="" status={networkEmptyFacts({market: 'ZA', marketLabel: 'South Africa', topics: 12, voices: 8, days: 30})} />,
    );
    const facts = factsOf(markup);
    expect(facts.length).toBe(5);
    for (const fact of facts) expect(machineTokenShape(fact)).toBe(null);
  });
});
