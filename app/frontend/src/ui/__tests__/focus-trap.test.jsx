/* The confirm dialogs keep Tab inside themselves. Cancel still takes first
   focus, Tab from the last control wraps to the first and Shift+Tab from the
   first wraps to the last, Tab between them is left to the browser, Escape
   still closes, and closing still hands focus back to the opener. */
import {GlobalRegistrator} from '@happy-dom/global-registrator';
import {afterAll, afterEach, beforeEach, expect, test} from 'bun:test';
import React, {useState} from 'react';

GlobalRegistrator.register();

const {createRoot} = await import('react-dom/client');
const {flushSync} = await import('react-dom');
const {CostConfirm, SpikeConfirm} = await import('../SpikeConfirm.jsx');
const {useFocusTrap} = await import('../useFocusTrap.js');

let host = null;
let root = null;

const dialog = () => host.querySelector('[role="dialog"]');
const button = (scope, label) => [...scope.querySelectorAll('button')].find((b) => b.textContent.trim() === label);
const click = (el) => flushSync(() => el.dispatchEvent(new MouseEvent('click', {bubbles: true})));
const tab = (shiftKey = false) => {
  const event = new KeyboardEvent('keydown', {key: 'Tab', shiftKey, bubbles: true, cancelable: true});
  flushSync(() => document.activeElement.dispatchEvent(event));
  return event;
};
const label = () => document.activeElement && document.activeElement.textContent.trim();

beforeEach(() => {
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  if (root) flushSync(() => root.unmount());
  root = null;
  if (host) host.remove();
  host = null;
});

afterAll(() => {
  GlobalRegistrator.unregister();
});

function Opener({children}){
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>Open</button>
      {open && children(() => setOpen(false))}
    </>
  );
}

const cost = (props = {}) => (close) => (
  <CostConfirm title="Ask why?" where="Somewhere" line="Up to 60 credits." confirmWord="Ask" onConfirm={() => {}} onClose={close} {...props} />
);

test('the cost confirm wraps Tab from its last control to its first and Shift+Tab back, with Cancel still first', () => {
  flushSync(() => root.render(<Opener>{cost()}</Opener>));
  click(button(host, 'Open'));
  expect(label()).toBe('Cancel');
  const forward = tab();
  expect(forward.defaultPrevented).toBe(true);
  expect(label()).toBe('Ask');
  const back = tab(true);
  expect(back.defaultPrevented).toBe(true);
  expect(label()).toBe('Cancel');
  expect(dialog().contains(document.activeElement)).toBe(true);
});

test('the cost confirm leaves Tab between its controls to the browser', () => {
  flushSync(() => root.render(<Opener>{cost()}</Opener>));
  click(button(host, 'Open'));
  const back = tab(true);
  expect(back.defaultPrevented).toBe(false);
  expect(label()).toBe('Cancel');
  button(dialog(), 'Ask').focus();
  expect(tab().defaultPrevented).toBe(false);
});

test('the cost confirm still closes on Escape and hands focus back to its opener', () => {
  flushSync(() => root.render(<Opener>{cost()}</Opener>));
  const open = button(host, 'Open');
  open.focus();
  click(open);
  tab();
  flushSync(() => document.activeElement.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})));
  expect(dialog()).toBeNull();
  expect(document.activeElement).toBe(open);
});

test('the spike confirm wraps Tab and Shift+Tab inside itself', () => {
  const day = {date: '2026-09-28', words: 'Ubuntu stories'};
  flushSync(() => root.render(<Opener>{(close) => <SpikeConfirm day={day} itemId="item_ubuntu" market="ZA" onClose={close} />}</Opener>));
  click(button(host, 'Open'));
  expect(label()).toBe('Cancel');
  expect(tab().defaultPrevented).toBe(true);
  expect(label()).toBe('Ask');
  expect(tab(true).defaultPrevented).toBe(true);
  expect(label()).toBe('Cancel');
});

function Trapped(){
  const box = React.useRef(null);
  useFocusTrap(box);
  return (
    <div ref={box} role="dialog">
      <button type="button" hidden>Hidden first</button>
      <button type="button">One</button>
      <button type="button">Two</button>
      <button type="button" hidden>Hidden</button>
      <div hidden><button type="button">Inside hidden</button></div>
      <button type="button" style={{display: 'none'}}>Not displayed</button>
      <div style={{display: 'none'}}><button type="button">Inside not displayed</button></div>
    </div>
  );
}

test('hidden controls are not counted as the first or last stop', () => {
  flushSync(() => root.render(<Trapped />));
  button(dialog(), 'Two').focus();
  expect(tab().defaultPrevented).toBe(true);
  expect(label()).toBe('One');
  expect(tab(true).defaultPrevented).toBe(true);
  expect(label()).toBe('Two');
});

test('with every control disabled while the ask is on its way, Tab is left alone', () => {
  flushSync(() => root.render(<CostConfirm title="Ask why?" where="Somewhere" line="Up to 60 credits." confirmWord="Ask" busy onConfirm={() => {}} onClose={() => {}} />));
  for (const shiftKey of [false, true]){
    const event = new KeyboardEvent('keydown', {key: 'Tab', shiftKey, bubbles: true, cancelable: true});
    flushSync(() => dialog().dispatchEvent(event));
    expect(event.defaultPrevented).toBe(false);
  }
});
