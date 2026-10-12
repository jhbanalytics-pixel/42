/* Re-review N5 and N6. The credit confirm keeps every click, key and press
   inside its backdrop, so a handler on whatever opened it never hears one;
   and its scroll lock holds both the html and the body element, then gives
   back whatever overflow each had before it opened. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {act} = React;
const actEnvironment = globalThis.IS_REACT_ACT_ENVIRONMENT;
const {CostConfirm} = await import('../SpikeConfirm.jsx');

let layer = null;
let host = null;
let root = null;
let heard = [];

afterAll(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
  GlobalRegistrator.unregister();
});

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  heard = [];
  layer = document.createElement('div');
  layer.id = 'main-content';
  host = document.createElement('div');
  layer.appendChild(host);
  document.body.appendChild(layer);
  root = createRoot(host);
});

afterEach(async () => {
  await act(async () => root.unmount());
  layer.remove();
  document.documentElement.style.overflow = '';
  document.body.style.overflow = '';
  globalThis.IS_REACT_ACT_ENVIRONMENT = actEnvironment;
});

function Opener({open, onClose}){
  const hear = (event) => heard.push(event.type);
  return (
    <div onClick={hear} onKeyDown={hear} onMouseDown={hear}>
      {open && <CostConfirm title="Ask why?" line="A quick scan, up to 60 credits." confirmWord="Ask" onConfirm={() => {}} onClose={onClose} />}
    </div>
  );
}

const cancel = () => [...document.querySelectorAll('.w42-dialog button')].find((node) => node.textContent.trim() === 'Cancel');

test('no click, key or press inside the confirm reaches the element that opened it', async () => {
  await act(async () => root.render(<Opener open onClose={() => {}} />));
  const button = cancel();
  await act(async () => {
    button.dispatchEvent(new window.KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
    button.dispatchEvent(new window.KeyboardEvent('keydown', {key: 'a', bubbles: true}));
    button.dispatchEvent(new window.MouseEvent('mousedown', {bubbles: true}));
    document.querySelector('.w42-dialog h2').dispatchEvent(new window.MouseEvent('click', {bubbles: true}));
  });
  expect(heard).toEqual([]);
});

test('the scroll lock holds html and body and gives back the overflow each had before', async () => {
  document.documentElement.style.overflow = 'auto';
  document.body.style.overflow = 'scroll';
  let shut = null;
  const close = () => shut();
  function Toggle(){
    const [open, setOpen] = React.useState(true);
    shut = () => setOpen(false);
    return <Opener open={open} onClose={close} />;
  }
  await act(async () => root.render(<Toggle />));
  expect(document.documentElement.style.overflow).toBe('hidden');
  expect(document.body.style.overflow).toBe('hidden');
  await act(async () => { cancel().click(); });
  expect(document.querySelector('.w42-dialog')).toBeNull();
  expect(document.documentElement.style.overflow).toBe('auto');
  expect(document.body.style.overflow).toBe('scroll');
});
