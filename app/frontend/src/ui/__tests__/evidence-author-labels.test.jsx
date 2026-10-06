import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {EvidenceChip} = await import('../EvidenceChip.jsx');
const {PostStrip} = await import('../PostStrip.jsx');
const {SourcePanel} = await import('../SourcePanel.jsx');

let host = null;
let root = null;

beforeEach(() => {
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  window.__evidenceAuthorInjected = 0;
});

afterEach(() => {
  if (root) flushSync(() => root.unmount());
  root = null;
  if (host) host.remove();
  host = null;
  delete window.__evidenceAuthorInjected;
});

afterAll(() => {
  GlobalRegistrator.unregister();
});

const render = (view) => flushSync(() => root.render(view));

test('YouTube channel IDs use neutral labels across chip, strip and source panel while excerpts stay text', () => {
  const channelId = 'UC' + 'a'.repeat(22);
  const lowerChannelId = channelId.toLowerCase();
  const hostile = '<img src=x onerror="window.__evidenceAuthorInjected=1"> &lt;b&gt;';
  const evidence = {
    id: 'youtube-source',
    platform: 'youtube',
    handle: '  @@' + lowerChannelId + '  ',
    url: 'https://example.invalid/post',
    text: hostile,
    transcript_span: {start_s: 2, text: hostile},
  };
  const stripEvidence = {...evidence, handle: '@' + lowerChannelId};
  const sourceEvidence = {...evidence, handle: ' ' + lowerChannelId + ' ', thumbnail_url: 'https://example.invalid/source.png'};
  render(
    <div>
      <EvidenceChip evidence={evidence} quotes={[]} pinned={false} />
      <PostStrip evidence={[stripEvidence]} />
      <SourcePanel evidence={sourceEvidence} quotes={[]} onClose={() => {}} />
    </div>,
  );

  const chip = host.querySelector('.ask42-chip');
  expect(chip.querySelector('.ask42-chip-handle').textContent).toBe('YouTube channel');
  expect(chip.getAttribute('aria-label')).toBe('Source: YouTube post');
  flushSync(() => chip.focus());
  expect(host.querySelector('.ask42-popover-text').textContent).toBe(hostile);

  const stripPost = host.querySelector('.ask42-post');
  expect(stripPost.querySelector('.ask42-post-creator').textContent).toBe('YouTube channel');
  /* UI polish, 2 October 2026: a post with no still is a text row whose link names the post. */
  expect(stripPost.querySelector('.ask42-post-open').getAttribute('aria-label')).toBe('Open the YouTube post');
  expect(stripPost.querySelector('.ask42-post-line').textContent).toBe('0:02 ' + hostile);

  expect(host.querySelector('.ask42-source-title').textContent).toBe('YouTube post');
  expect(host.querySelector('.ask42-source-thumb').getAttribute('alt')).toBe('Still from the YouTube post');
  expect(host.querySelector('.ask42-source-text').textContent).toBe(hostile);
  expect([...host.querySelectorAll('[aria-label]')].some((node) => /uc[A-Za-z0-9_-]{22}/i.test(node.getAttribute('aria-label')))).toBe(false);
  expect(host.textContent).not.toMatch(/uc[A-Za-z0-9_-]{22}/i);
  expect(host.querySelectorAll('script, [onerror]')).toHaveLength(0);
  expect(host.querySelectorAll('img')).toHaveLength(1);
  expect(host.textContent).toContain('&lt;b&gt;');
  expect(window.__evidenceAuthorInjected).toBe(0);
});

test('readable YouTube names and handles and TikTok ID shaped handles remain unchanged', () => {
  const channelId = 'UC' + 'a'.repeat(22);
  const youtubeHandle = {id: 'youtube-handle', platform: 'youtube', handle: '@fixture_creator', text: 'Handle text'};
  const youtubeName = {id: 'youtube-name', platform: 'youtube', handle: 'Amara Ndlovu', text: 'Named text'};
  const tiktokIdShaped = {id: 'tiktok-channel-like', platform: 'tiktok', handle: channelId,
    url: 'https://example.invalid/tiktok', text: 'TikTok text'};
  render(
    <div>
      <EvidenceChip evidence={youtubeHandle} quotes={[]} pinned={false} />
      <EvidenceChip evidence={tiktokIdShaped} quotes={[]} pinned={false} />
      <PostStrip evidence={[youtubeHandle, youtubeName, tiktokIdShaped]} />
      <SourcePanel evidence={youtubeName} quotes={[]} onClose={() => {}} />
      <SourcePanel evidence={tiktokIdShaped} quotes={[]} onClose={() => {}} />
    </div>,
  );

  expect(host.querySelector('.ask42-chip-handle').textContent).toBe('@fixture_creator');
  expect(host.querySelector('.ask42-chip').getAttribute('aria-label')).toBe('Source: YouTube post by @fixture_creator');
  expect(host.querySelectorAll('.ask42-chip-handle').item(1).textContent).toBe(channelId);
  expect(host.querySelectorAll('.ask42-chip').item(1).getAttribute('aria-label')).toBe('Source: TikTok post by ' + channelId);
  expect([...host.querySelectorAll('.ask42-post-creator')].map((node) => node.textContent)).toEqual([
    '@fixture_creator', 'Amara Ndlovu', channelId,
  ]);
  /* UI polish, 2 October 2026: posts with no still are text rows; the row link names the post. */
  expect(host.querySelector('.ask42-post-open[aria-label]').getAttribute('aria-label')).toBe('Open the TikTok post by ' + channelId);
  expect([...host.querySelectorAll('.ask42-source-title')].map((node) => node.textContent)).toEqual([
    'YouTube post by Amara Ndlovu', 'TikTok post by ' + channelId,
  ]);
});
