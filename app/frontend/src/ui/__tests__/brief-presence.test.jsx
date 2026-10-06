import {expect, test} from 'bun:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {briefText} from '../../briefText.js';
import {TopicDetail, CopyBriefBtn} from '../../views.jsx';

const base = {id: 'music_amapiano', topic: 'Amapiano', region: 'ZA', regionName: 'South Africa', label: 'Music', momentum: 'steady', voices: []};
const empty = {trend: '', relevance: ' ', idea: {tool: 'Editorial response', text: ''}, prompt: {nano: 'A prompt', lyria: ''}, opportunity: 'A cached opportunity'};

test('an empty constructed brief cannot produce a titled export or copy action', () => {
  expect(briefText({...base, brief: empty})).toBe('');
  expect(renderToStaticMarkup(<CopyBriefBtn t={{...base, brief: empty}} />)).toBe('');
});
test('the topic modal does not label an empty container as a signal brief', () => {
  const html = renderToStaticMarkup(<TopicDetail t={{...base, brief: empty}} onClose={() => {}} />);
  expect(html).not.toContain('Signal brief');
  expect(html).not.toContain('A cached opportunity');
});
test('genuine partial brief text remains readable and exportable', () => {
  const topic = {...base, brief: {...empty, trend: 'An observed cultural signal.'}};
  expect(briefText(topic)).toContain('An observed cultural signal.');
  expect(renderToStaticMarkup(<CopyBriefBtn t={topic} />)).toContain('Copy brief');
  expect(renderToStaticMarkup(<TopicDetail t={topic} onClose={() => {}} />)).toContain('An observed cultural signal.');
});
