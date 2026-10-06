import {afterAll, expect, mock, test} from 'bun:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

/* The Intelligence Console reads the desk through the api hook on every open.
   A vite build never checks a free identifier, so the page has to be rendered
   once, with the hook stubbed, to prove the hook the console calls is the one
   it imports. Snapshot the real module and restore it when this file is done,
   because mock.module is process wide. */
const realApi = {...(await import('../../api.js'))};

mock.module('../../api.js', () => ({
  ...realApi,
  useApi: () => [{state: 'loading'}, () => {}],
  apiGetFresh: () => Promise.reject(new Error('not called during static render')),
  apiPost: () => Promise.reject(new Error('not called during static render')),
}));

afterAll(() => { mock.module('../../api.js', () => realApi); });

const {ChatPage} = await import('../../chat.jsx');

test('the console renders on a fresh open without throwing', () => {
  let markup = '';
  expect(() => {
    markup = renderToStaticMarkup(
      <ChatPage region="ZA" session="test" onAuth={() => {}} embedded={false} initialThreadId={null} />,
    );
  }).not.toThrow();
  expect(markup.length).toBeGreaterThan(0);
});

test('the console renders the all-markets desk without throwing', () => {
  expect(() => renderToStaticMarkup(
    <ChatPage region="all" session="test" onAuth={() => {}} initialThreadId={null} />,
  )).not.toThrow();
});

/* The rail and the dock take the page's entrance reveal and the Ask card does
   not, so the card is painted from the first frame. The class is what the
   motion rules key on; data-motion="off" still shows every reveal at once. */
function classesOf(markup, pattern){
  const match = pattern.exec(markup);
  return match ? match[1].split(' ') : null;
}

/* The dock is the nearest element with a class around the question box. */
function dockClasses(markup){
  const before = markup.slice(0, markup.indexOf('<textarea'));
  return classesOf(before.slice(before.lastIndexOf('<div class="')), /^<div class="([^"]*)"/);
}

test('the rail and the dock reveal while the Ask card is painted at once', () => {
  const markup = renderToStaticMarkup(
    <ChatPage region="ZA" session="test" onAuth={() => {}} embedded={false} initialThreadId={null} />,
  );
  expect(classesOf(markup, /<aside class="([^"]*console-rail[^"]*)"/)).toContain('reveal');
  expect(dockClasses(markup)).toContain('reveal');
  expect(classesOf(markup, /<div class="([^"]*m-stack[^"]*)"/)).not.toContain('reveal');
});

test('the embedded console has no rail and its dock still reveals', () => {
  const markup = renderToStaticMarkup(
    <ChatPage region="ZA" session="test" onAuth={() => {}} embedded initialThreadId={null} />,
  );
  expect(markup).not.toContain('console-rail');
  expect(dockClasses(markup)).toContain('reveal');
});
