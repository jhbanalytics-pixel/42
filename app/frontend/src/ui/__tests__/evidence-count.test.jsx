/* The evidence bank heading counts its sources, and a bank of one source read
   "1 sources". The count is written through countOf so the noun agrees. */
import {describe, expect, test} from 'bun:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {EvidenceBank} from '../../researchLib.jsx';

const search = (n) => ({
  ref_type: 'google_trends_rising',
  market: 'za',
  query_group: 'food_rituals_braai',
  headline: 'Rising search: Braai ' + n,
  text: 'Search velocity 1.20 on Braai, reading ' + n,
});

const badgeOf = (sources) => {
  const markup = renderToStaticMarkup(<EvidenceBank sources={sources} onRef={() => {}} />);
  const found = markup.match(/class="research-evidence-mix-badge">([^<]*)</);
  return found ? found[1] : null;
};

describe('the evidence bank count agrees with its noun', () => {
  test('one source reads in the singular', () => {
    expect(badgeOf([search(1)])).toMatch(/^1 source · /);
  });

  test('two sources read in the plural', () => {
    expect(badgeOf([search(1), search(2)])).toMatch(/^2 sources · /);
  });
});
