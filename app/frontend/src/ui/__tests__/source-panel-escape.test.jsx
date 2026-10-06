/* The pinned Ask source closes with Escape as well as Close, and the panel
   ref is on the aside, so moving between sources keeps the original opener
   and closing returns focus to it (product review SHL-03). */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, expect, test} from 'bun:test';
import React, {useState} from 'react';

const registered = typeof document === 'undefined';
if (registered) GlobalRegistrator.register();
afterAll(() => { if (registered) GlobalRegistrator.unregister(); });

const {createRoot} = await import('react-dom/client');
const {act} = await import('react');
const {SourcePanel} = await import('../SourcePanel.jsx');

const A = {id: 'a', platform: 'tiktok', handle: 'one', text: 'First post', url: 'https://www.tiktok.com/@one/video/1'};
const B = {id: 'b', platform: 'tiktok', handle: 'two', text: 'Second post', url: 'https://www.tiktok.com/@two/video/2'};

test('Escape closes the source and focus returns to the opener, even after switching sources', async () => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  let pick = null;
  function Harness(){
    const [picked, setPicked] = useState(null);
    pick = setPicked;
    return <div><button type="button" id="opener">Open</button><SourcePanel evidence={picked} quotes={[]} onClose={() => setPicked(null)} /></div>;
  }
  await act(async () => root.render(<Harness />));
  host.querySelector('#opener').focus();
  await act(async () => pick(A));
  expect(document.activeElement.textContent).toContain('one');
  await act(async () => pick(B));
  const aside = host.querySelector('aside.ask42-source');
  expect(aside.textContent).toContain('Second post');
  await act(async () => { aside.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})); });
  expect(host.querySelector('aside.ask42-source')).toBeNull();
  expect(document.activeElement.id).toBe('opener');
  await act(async () => root.unmount());
  host.remove();
});
