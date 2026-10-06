/* Round 12 of the 42 in Black programme measured the consumer production
   build at design system 2.0.13 and left exactly two open rows, both of them
   the same fault on two consumer files.

   Two uppercase mono headings sit at 0.16em where section 5 gives the
   provenance label 11px at 500 and 0.18em. Neither row is a regression from
   the round 11 fix wave: both were at 0.16em through rounds 10 and 11 and were
   hidden by a reading slack of 0.02em, which put a one step tracking
   difference on a floating point boundary. Task 57 tightened the slack to
   0.01em and the two rows surfaced in the re-rank.

   The second row is the first one inherited. The lane numerals on the map
   heading declare only their colour, so whatever tracking the heading carries
   is the tracking they render at, and a sweep reads them as a run of their
   own. That is why the numeral test here asserts an absence: the span must go
   on declaring no tracking, so one declaration keeps governing both runs. If a
   later edit gives the span its own letter spacing, the two runs can drift
   apart again without either site looking wrong on its own line. */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';

const read = (path) => readFileSync(fileURLToPath(new URL(`../../${path}`, import.meta.url)), 'utf8');

/* Section 5 gives Recursive Mono two roles and no third. */
const LABEL = {weight: '500', tracking: '0.18em'};

function braceBlock(source, open){
  if (open < 0) return null;
  let depth = 0;
  for (let index = open; index < source.length; index += 1){
    if (source[index] === '{') depth += 1;
    if (source[index] === '}'){
      depth -= 1;
      if (depth === 0) return source.slice(open, index + 1);
    }
  }
  return null;
}

/* The lane heading is the element the numeral span sits inside, so its style
   is the nearest one before that span rather than the one before the numeral,
   which is the span's own. */
/* One title voice, 2 October 2026: the lane heading dropped its red numeral,
   so the heading is found by its title run rather than by the numeral span. */
function laneHeadingStyle(source){
  const at = source.indexOf('{lane.title}</div>');
  if (at < 0) return null;
  const open = source.lastIndexOf('style={{', at);
  return open < 0 ? null : braceBlock(source, open + 'style='.length);
}

function roleOfInline(text, where){
  expect(text, `${where} carries a style this test can read`).toBeTruthy();
  expect(text, `${where} names the mono family at the provenance size`).toMatch(/var\(--mono\)[\s\S]*var\(--type-1\)/);
  const tracking = text.match(/letterSpacing:\s*'([^']+)'/);
  const weight = text.match(/fontWeight:\s*'?(\d+)'?/);
  return {tracking: tracking ? tracking[1] : null, weight: weight ? weight[1] : '400'};
}

test('the two uppercase mono headings round 12 measured take the section 5 label role', () => {
  const off = [];
  const hold = (where, measured, role) => {
    if (measured.tracking !== role.tracking || measured.weight !== role.weight){
      off.push(`${where} is ${measured.weight}/${measured.tracking} against ${role.weight}/${role.tracking}`);
    }
  };

  /* Quiet register, 23 Sept 2026: the lane heading is a heading a reader
     reads, not a provenance label, so it leaves the mono capitals. It is held
     to the sans at 16px and weight 600 with no tracking and no case set in
     style; the numeral span below still declares none of its own, so this one
     declaration keeps governing both runs. */
  /* Every page on the Map, 2 October 2026: the lane heading is now the group
     heading, styled in styles/map42.css rather than inline, so the same
     voice is held there: the sans at weight 600, untracked, no case set.
     It takes the section title step, as group headings do on other pages. */
  const map = read('map.jsx');
  const lane = 'map42.css the group heading';
  expect(map, 'map.jsx sets no inline style').not.toMatch(/style=\{\{/);
  const heading = (read('styles/map42.css').match(/\.map42-group-title \{[^}]*\}/) || [null])[0];
  expect(heading, `${lane} carries a rule this test can read`).toBeTruthy();
  if (!/font-family:\s*var\(--sans\)/.test(heading)) off.push(`${lane} is not set in the sans`);
  if (!/font-size:\s*var\(--type-section-title\)/.test(heading)) off.push(`${lane} is not set at the section title step`);
  if (!/font-weight:\s*600/.test(heading)) off.push(`${lane} is not set at weight 600`);
  if (/letter-spacing/.test(heading)) off.push(`${lane} is tracked`);
  if (/text-transform/.test(heading)) off.push(`${lane} sets its case in style`);

  /* Quiet register, 23 Sept 2026: the seeds labels left the mono capitals
     for the quiet label, under the constant LABEL: the sans at 14px and
     weight 400, with no tracking and no case set in style. */
  const seeds = read('seeds.jsx');
  const cap = seeds.match(/const LABEL = \{[\s\S]*?\};/);
  expect(cap, 'the seeds LABEL style').toBeTruthy();
  const seedLabel = cap[0];
  if (!/fontFamily:\s*'var\(--sans\)'/.test(seedLabel)) off.push('seeds.jsx LABEL is not set in the sans');
  if (!/fontSize:\s*'var\(--type-3\)'/.test(seedLabel)) off.push('seeds.jsx LABEL is not set at 14px');
  if (!/fontWeight:\s*'?400'?/.test(seedLabel)) off.push('seeds.jsx LABEL is not set at weight 400');
  if (/letterSpacing/.test(seedLabel)) off.push('seeds.jsx LABEL is tracked');
  if (/textTransform/.test(seedLabel)) off.push('seeds.jsx LABEL sets its case in style');

  expect(off).toEqual([]);
});

/* One title voice, 2 October 2026: the red "01" lane numeral was one of the
   named tells of 23 Sept, so the lane heading is its title alone and one
   declaration governs the whole run, which is what this test held. */
test('the map lane heading is one run under one declaration, with no numeral', () => {
  /* Every page on the Map, 2 October 2026: lanes became groups, so the
     heading is found by the group title run; it is still one run alone in
     its heading element with no numeral beside it. */
  const map = read('map.jsx');
  expect(map, 'no lane numeral is rendered').not.toMatch(/\{lane\.no\}|\bno: '0\d'/);
  const at = map.indexOf('{group.title}</h2>');
  expect(at, 'the group title is rendered from the group record').toBeGreaterThan(0);
  const open = map.lastIndexOf('<h2', at);
  expect(map.slice(open, at), 'the title shares no span with another run').not.toMatch(/<span/);
});

/* Every remaining MONO_CAP site on seeds names a thing rather than reporting a
   measurement, so one constant on the label role is the right shape for all
   five. Held here so a value role reading is not quietly folded into it.
   Quiet register, 23 Sept 2026: the constant is now LABEL, the same sites. */
test('every seeds MONO_CAP site is a label that names a thing', () => {
  const seeds = read('seeds.jsx');
  const uses = [...seeds.matchAll(/style=\{LABEL\}>([^<{]*)/g)].map((found) => found[1].trim());
  expect(uses.length, 'the constant is used on more than one site').toBeGreaterThanOrEqual(3);
  const digits = uses.filter((text) => text && /^[\d\s.,%-]+$/.test(text));
  expect(digits, 'a run of digits is a measured value, not a label').toEqual([]);
});
