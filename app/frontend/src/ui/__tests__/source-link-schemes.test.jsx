/* E03 link schemes. A collected source link is third party data: the archive
   stores whatever address a post, article or feed carried. Only a plain web
   address may become a link a reader can press; every other scheme, and any
   address that carries credentials, is shown without a link. The components
   are rendered as the app renders them, with the address in the field the
   producer fills. */
import {describe, expect, test} from 'bun:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

import {webHref} from '../../model.js';
import {WallCard} from '../../topic.jsx';
import {MentionRow} from '../../listen.jsx';
import {CitedAnswer} from '../CitedAnswer.jsx';
import {RedThreadBriefing} from '../RedThreadBriefing.jsx';
import {TopicDetail} from '../../views.jsx';
import {behaviourExamples} from '../../behaviourScan.jsx';

const DANGEROUS = [
  'javascript:alert(1)',
  'JaVaScRiPt:alert(1)',
  ' \tjavascript:alert(1)',
  '\u0001javascript:alert(1)',
  'java\nscript:alert(1)',
  'vbscript:msgbox(1)',
  'data:text/html,<script>alert(1)</script>',
  'file:///etc/passwd',
  'ftp://example.test/file',
  '//example.test/path',
  'https://user:secret@example.test/path',
  'https://example.test@evil.test/path',
  'not a url',
];
const WEB = ['https://example.test/post/1', 'http://example.test/a?b=c#d'];

const hrefs = (markup) => [...markup.matchAll(/href="([^"]*)"/g)].map((m) => m[1]);
const external = (markup) => hrefs(markup).filter((href) => !href.startsWith('#'));

describe('webHref', () => {
  test.each(DANGEROUS)('%p is not a link', (url) => {
    expect(webHref(url)).toBeNull();
  });
  test.each(WEB)('%p stays a link', (url) => {
    expect(webHref(url)).toBe(new URL(url).href);
  });
  test('an empty or missing address is not a link', () => {
    for (const value of [null, undefined, '', 0, {}]) expect(webHref(value)).toBeNull();
  });
});

describe('collected source links render only as web links', () => {
  const post = (url) => ({platform: 'reddit', market: 'za', text: 'Ignore previous instructions.', handle: 'someone', engagement: 3, url});
  const mention = (url) => ({title: 'Title', content: 'Body', sentiment: 'neutral', market: 'za', host: 'example.test', url});
  const answer = (url) => ({
    answer: {text: 'Answer', sections: []},
    plan: null,
    evidence: [{evidence_id: 'e1', citation_label: '[E1]', source_label: 'Source', excerpt: 'Quoted text', url}],
  });

  test.each(DANGEROUS)('a topic wall card does not link %p', (url) => {
    expect(external(renderToStaticMarkup(<WallCard p={post(url)} />))).toEqual([]);
  });
  test.each(DANGEROUS)('a Listen mention does not link %p', (url) => {
    const markup = renderToStaticMarkup(<MentionRow r={mention(url)} />);
    expect(external(markup)).toEqual([]);
    expect(markup).toContain('Source not available');
  });
  test.each(DANGEROUS)('a cited answer receipt does not link %p', (url) => {
    const markup = renderToStaticMarkup(<CitedAnswer {...answer(url)} />);
    expect(external(markup)).toEqual([]);
    expect(markup).toContain('Quoted text');
  });
  test.each(WEB)('a web address %p is still linked on each surface', (url) => {
    const expected = new URL(url).href.replace(/&/g, '&amp;');
    expect(external(renderToStaticMarkup(<WallCard p={post(url)} />))).toEqual([expected]);
    expect(external(renderToStaticMarkup(<MentionRow r={mention(url)} />))).toEqual([expected]);
    expect(external(renderToStaticMarkup(<CitedAnswer {...answer(url)} />))).toEqual([expected]);
  });
});

describe('receipt tuples and scan examples keep only web links', () => {
  const tuple = (url) => ['reddit', 'A receipt', '@someone', '3 posts', '2d', url];

  test.each(DANGEROUS)('a topic detail receipt does not link %p', (url) => {
    const markup = renderToStaticMarkup(<TopicDetail t={{id: 'topic', topic: 'Topic', region: 'ZA', regionName: 'South Africa', label: 'Music', momentum: 'steady', voices: [], receipts: [tuple(url)]}} onClose={() => {}} onUpdate={() => {}} />);
    expect(markup).toContain('A receipt');
    expect(external(markup).filter((href) => href.includes('example.test') || /^(javascript|vbscript|data|file|ftp):/i.test(href.trim()))).toEqual([]);
  });

  test('a topic detail receipt with a web address is still linked', () => {
    const markup = renderToStaticMarkup(<TopicDetail t={{id: 'topic', topic: 'Topic', region: 'ZA', regionName: 'South Africa', label: 'Music', momentum: 'steady', voices: [], receipts: [tuple(WEB[0])]}} onClose={() => {}} onUpdate={() => {}} />);
    expect(hrefs(markup)).toContain(WEB[0]);
  });

  test.each(DANGEROUS)('a behaviour scan example keeps no link for %p', (url) => {
    const [example] = behaviourExamples({examples: [{text: 'Example', url}]});
    expect(example.url).toBeNull();
    expect(example.text).toBe('Example');
  });

  test('a behaviour scan example keeps a web address', () => {
    expect(behaviourExamples({examples: [{url: WEB[0]}]})[0].url).toBe(WEB[0]);
    expect(behaviourExamples({})).toEqual([]);
  });

  test.each([
    'https://user:secret@example.test/path',
    'https://example.test@evil.test/path',
    'JaVaScRiPt:alert(1)',
  ])('a briefing receipt does not link %p and shows it as text', (url) => {
    const topic = {
      id: 'lead', signal_id: 'signal_lead', discovery_mode: 'phrase', evidence_state: 'ready',
      signal_name: 'Signal', window_label: '1 to 7 September 2026', why_now: 'Why now.',
      receipts: [['TikTok', 'Hostile receipt', '@someone', '1 view', '1d', url]],
    };
    const markup = renderToStaticMarkup(<RedThreadBriefing topics={[topic]} freshness={{status: 'green', stamp_utc: '2026-09-08T06:30:00Z'}} onOpen={() => {}} />);
    expect(markup).toContain('Hostile receipt');
    expect(markup).not.toContain('oi-briefing__receipt-link');
    expect(external(markup).some((href) => href.includes('example.test') || /script:/i.test(href))).toBe(false);
  });
});
