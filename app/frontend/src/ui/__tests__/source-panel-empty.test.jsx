/* Demo polish, 2 October 2026: before a source is picked, the Ask answer
   kept a 320 px column beside it holding one muted hint. Nothing shows there
   now until a source is picked, and the answer takes the full width; the
   second column opens only while a source sits in it, and the phone layout
   stays one column either way. */
import {expect, test} from 'bun:test';
import {renderToStaticMarkup} from 'react-dom/server';
import {readFileSync} from 'node:fs';
import postcss from 'postcss';
import {SourcePanel} from '../SourcePanel.jsx';

const rules = (selector, media) => {
  const css = postcss.parse(readFileSync(new URL('../../styles/ask42.css', import.meta.url), 'utf8'));
  const found = {};
  css.walkRules((rule) => {
    const inMedia = rule.parent && rule.parent.type === 'atrule' ? rule.parent.params : null;
    if (rule.selectors.includes(selector) && inMedia === media) rule.walkDecls((d) => { found[d.prop] = d.value; });
  });
  return found;
};

test('with nothing picked the source panel renders nothing', () => {
  const html = renderToStaticMarkup(<SourcePanel evidence={null} quotes={[]} onClose={() => {}} />);
  expect(html).toBe('');
});

test('the answer uses the full width until a source is pinned beside it', () => {
  expect(rules('.ask42-answer-layout', null)['grid-template-columns']).toBe('minmax(0, 1fr)');
  expect(rules('.ask42-answer-layout:has(> .ask42-source)', null)['grid-template-columns']).toBe('minmax(0, 1fr) 320px');
  expect(rules('.ask42-answer-layout:has(> .ask42-source)', '(max-width: 900px)')['grid-template-columns']).toBe('1fr');
  expect(rules('.ask42-source-empty', null)).toEqual({});
});
