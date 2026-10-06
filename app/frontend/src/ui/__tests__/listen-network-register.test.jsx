/* Quiet register, 23 Sept 2026: Listen and Network state their figures in
   words, so the words have to be true. The split counts each sentiment the
   producer sends rather than taking neutral as the leftover, ages read as a
   person would say them on a fixed clock, and the notes keep the capped,
   rolling window the producer really measures. The sheets are read as
   source for the few rules that decide colour and alignment. */
import {describe, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import postcss from 'postcss';
import {renderToStaticMarkup} from 'react-dom/server';
import {ListenPage, SentimentSplit, mentionAge, sentimentSplit} from '../../listen.jsx';
import {coverageWords, engagementWords, networkFinding} from '../../network.jsx';

const read = (path) => readFileSync(fileURLToPath(new URL(`../../${path}`, import.meta.url)), 'utf8');
const plain = (html) => html.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim();
const rows = (spec) => Object.entries(spec).flatMap(([sentiment, n]) => Array.from({length: n}, () => ({sentiment: sentiment === 'none' ? null : sentiment})));

describe('the Listen sentiment split', () => {
  test('four neutral rows of nine read 44%, each share rounded on its own count', () => {
    const split = sentimentSplit(rows({positive: 3, neutral: 4, negative: 2}));
    expect(split).toEqual({total: 9, positive: 33, neutral: 44, negative: 22, unrated: 0});
  });

  test('a row with no sentiment is not counted as neutral; it is counted apart', () => {
    const split = sentimentSplit(rows({positive: 1, negative: 1, none: 1}));
    /* Demo polish, 2 October 2026: the shares are now taken over the rated
       posts only, so one positive and one negative of two rated read 50% and
       50% rather than 33% and 33% of three, which summed to 66% on screen.
       The unrated row is still counted apart and the object is still matched
       whole. */
    expect(split).toEqual({total: 3, positive: 50, neutral: 0, negative: 50, unrated: 1});
  });

  test('shares are taken over the rated posts only, so the demo feed reads 33% and 67%, not 25% and 50%', () => {
    const split = sentimentSplit(rows({positive: 1, negative: 2, none: 1}));
    expect(split).toEqual({total: 4, positive: 33, neutral: 0, negative: 67, unrated: 1});
    expect(split.positive + split.neutral + split.negative).toBe(100);
  });

  test('no rated post gives no share at all rather than a row of zeros', () => {
    const split = sentimentSplit(rows({none: 2}));
    expect(split).toEqual({total: 2, positive: 0, neutral: 0, negative: 0, unrated: 2});
    expect(plain(renderToStaticMarkup(<SentimentSplit rows={rows({none: 2})} />))).toBe('None of the 2 posts loaded has a sentiment.');
  });

  test('the sentence says how many posts were rated and what the shares are of', () => {
    expect(plain(renderToStaticMarkup(<SentimentSplit rows={rows({positive: 1, negative: 2, none: 1})} />)))
      .toBe('4 posts loaded. Of the 3 with a sentiment, 33% are positive, 0% neutral and 67% negative. 1 has none.');
    expect(plain(renderToStaticMarkup(<SentimentSplit rows={rows({positive: 3, neutral: 4, negative: 2})} />)))
      .toBe('9 posts loaded, all with a sentiment: 33% positive, 44% neutral and 22% negative.');
    expect(plain(renderToStaticMarkup(<SentimentSplit rows={rows({positive: 1, none: 2})} />)))
      .toBe('3 posts loaded. Of the 1 with a sentiment, 100% are positive, 0% neutral and 0% negative. 2 have none.');
  });

  test('the page draws the split from that count, never from a leftover', () => {
    const source = read('listen.jsx');
    expect(source).not.toMatch(/100 - pp - np/);
    expect(source).toMatch(/sentimentSplit\(rows\)/);
  });
});

describe('the Listen ages on a fixed clock', () => {
  const now = Date.parse('2026-09-23T08:00:00Z');
  test.each([
    ['2026-09-23T07:59:30Z', 'just now'],
    ['2026-09-23T07:59:00Z', '1 minute ago'],
    ['2026-09-23T07:15:00Z', '45 minutes ago'],
    ['2026-09-23T07:00:00Z', '1 hour ago'],
    ['2026-09-22T08:00:00Z', '1 day ago'],
    ['2026-09-11T08:00:00Z', '12 days ago'],
    ['2026-09-11T10:00:00+02:00', '12 days ago'],
  ])('%s reads %s', (date, words) => {
    expect(mentionAge(date, now)).toBe(words);
  });

  test('a missing or unreadable date carries no age', () => {
    expect(mentionAge(null, now)).toBe('');
    expect(mentionAge('not a date', now)).toBe('');
  });
});

describe('the Listen states and marks', () => {
  test('the market tabs are written as market names, not codes', () => {
    const html = renderToStaticMarkup(<ListenPage region="NG" setRegion={() => {}} session={0} onAuth={() => {}} />);
    /* Shell consistency, 2 October 2026: the chips are Today tabs (t42-tab)
       rather than legacy chips; they stay toggles with aria-pressed. */
    const tabs = [...html.matchAll(/<button[^>]*class="listen-filter-chip t42-tab"[^>]*aria-pressed="(true|false)"[^>]*>([^<]*)<\/button>/g)].map((m) => [m[2], m[1]]);
    expect(tabs.slice(0, 4)).toEqual([['South Africa', 'false'], ['Nigeria', 'true'], ['Kenya', 'false'], ['All', 'false']]);
    expect(html).toContain('placeholder="Filter these posts…"');
    expect(html).toContain('aria-label="Filter mentions"');
  });

  test('a failed or loading read shows no count in the filter box', () => {
    const source = read('listen.jsx');
    expect(source).toMatch(/first\.state === 'ready' && q && <span className="listen-search-count/);
  });

  /* Shell consistency, 2 October 2026: every filter row is the Today tab row,
     so the chosen market, sentiment and category all take the Today mark, an
     ink underline under ink words at 600. No chip on Listen is marked in red. */
  test('the chosen market, sentiment and category take the Today tab mark in ink, never red', () => {
    const source = read('listen.jsx');
    expect(source.match(/className="listen-filter-row listen-filter-row--refine"/g) || []).toHaveLength(2);
    const marks = {};
    let red = [];
    postcss.parse(read('styles/lexlisten.css')).walkRules((rule) => {
      for (const one of rule.selectors.map((s) => s.trim())){
        if (one.startsWith('.listen-filter-chip.t42-tab[aria-pressed="true"]')) rule.walkDecls((declaration) => { marks[one + ' ' + declaration.prop] = declaration.value; });
        if (one.includes('.listen-filter-chip')) rule.walkDecls((declaration) => { if (/red|--accent\)/.test(declaration.value) && declaration.prop !== 'outline') red.push(one + ' ' + declaration.prop); });
      }
    });
    expect(marks['.listen-filter-chip.t42-tab[aria-pressed="true"] color']).toBe('var(--ink)');
    expect(marks['.listen-filter-chip.t42-tab[aria-pressed="true"]::after opacity']).toBe('1');
    expect(red).toEqual([]);
    let underline = null;
    postcss.parse(read('styles/today42.css')).walkRules((rule) => {
      if (rule.selectors.includes('.t42-tab::after')) rule.walkDecls('background', (declaration) => { underline = declaration.value; });
    });
    expect(underline).toBe('var(--ink)');
  });
});

describe('the Network notes and plot', () => {
  test('engagement stays capped and its window stays the rolling 30 days', () => {
    const source = read('network.jsx');
    expect(source).toMatch(/capped engagement/);
    expect(source).toMatch(/rolling 30-day window/);
    expect(source).not.toMatch(/last 30 days/);
  });

  test('the finding is one plain sentence that agrees in number', () => {
    expect(networkFinding(5, 1)).toBe('1 voice connects two or more topics');
    expect(networkFinding(5, 3)).toBe('3 voices connect two or more topics');
    expect(networkFinding(5, 0)).toBe('Each of the 5 voices here is linked to one topic only');
    expect(networkFinding(1, 0)).toBe('The one voice here is linked to one topic only');
    expect(networkFinding(0, 0)).toBe('');
  });

  test('a voice line says engagement plainly and an unmeasured one says so', () => {
    expect(engagementWords(182000)).toBe('182k engagement');
    expect(engagementWords(null)).toBe('Engagement not measured');
  });

  test('the count line names what is shown once, and the matching total only when it differs', () => {
    expect(coverageWords({displayedTopics: 4, displayedCreators: 5, matchedTopics: 4, matchedCreators: 5})).toBe('Showing 4 topics and 5 voices.');
    expect(coverageWords({displayedTopics: 7, displayedCreators: 12, matchedTopics: 9, matchedCreators: 14})).toBe('Showing 7 of 9 matching topics and 12 of 14 matching voices.');
    expect(coverageWords({displayedTopics: 1, displayedCreators: 1, matchedTopics: 1, matchedCreators: 1})).toBe('Showing 1 topic and 1 voice.');
  });

  test('topic labels and their column head are left aligned', () => {
    const found = {};
    postcss.parse(read('styles/graphs.css')).walkRules((rule) => {
      for (const selector of ['.network-page .net-node--topic', '.network-page .net-column-heads > :first-child']){
        if (rule.selectors.some((one) => one.trim() === selector)) rule.walkDecls('text-align', (declaration) => { found[selector] = declaration.value; });
      }
    });
    expect(found).toEqual({'.network-page .net-node--topic': 'left', '.network-page .net-column-heads > :first-child': 'left'});
  });

  test('no comment in the graph sheet carries a run of hyphens', () => {
    const comments = read('styles/graphs.css').match(/\/\*[\s\S]*?\*\//g) || [];
    expect(comments.filter((comment) => comment.includes('--'))).toEqual([]);
  });
});
