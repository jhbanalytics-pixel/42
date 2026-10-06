import {describe, expect, test} from 'bun:test';
import {existsSync, readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

const componentNames = [
  'SignalLead',
  'SignalComparisonRow',
  'EvidenceDrawer',
  'EvidenceReadiness',
];

function componentUrl(name){
  return new URL(`../${name}.jsx`, import.meta.url);
}

async function loadComponent(name){
  const url = componentUrl(name);
  expect(existsSync(fileURLToPath(url)), `${name} must exist`).toBe(true);
  return import(url.href);
}

function source(name){
  return readFileSync(fileURLToPath(componentUrl(name)), 'utf8');
}

describe('42 dossier grammar', () => {
  test('SignalLead renders one complete decision path', async () => {
    const {SignalLead} = await loadComponent('SignalLead');
    const markup = renderToStaticMarkup(
      <SignalLead
        eyebrow="Emerging signal"
        title="Neighbourhood running clubs"
        movement="Rising across three source families"
        whyNow="Participation moved from posts into weekly meetups."
        proof="Eight cited examples across ZA and KE."
        response="Test a locally hosted route with community captains."
        evidence={[
          {family: 'video', direction: 'rising'},
          {family: 'forum', direction: 'rising'},
        ]}
        onEvidence={() => {}}
      />,
    );

    expect(markup).toContain('<article');
    expect(markup).toContain('aria-label="Neighbourhood running clubs"');
    expect(markup).toContain('Why now');
    expect(markup).toContain('Proof');
    expect(markup).toContain('Possible response');
    expect(markup.match(/<h3 class="dossier-section__label"/g)).toHaveLength(3);
    expect(markup).not.toContain('<div class="dossier-section__label"');
    /* Ready is earned, not declared: two independent families agreeing in
       direction. Handing the component a bare state can only demote. */
    expect(markup).toContain('Evidence readiness: Ready');
    expect(markup).toContain('2 independent families agree');
    expect(markup).toContain('Rising across three source families');
    expect(markup).toContain('<button');
  });

  test('SignalComparisonRow keeps movement and proof cues visible', async () => {
    const {SignalComparisonRow} = await loadComponent('SignalComparisonRow');
    const markup = renderToStaticMarkup(
      <SignalComparisonRow
        signal="Community football"
        comparison="ZA leads NG on source breadth"
        movement="Building"
        proof="Four independent source families"
        readiness="thin"
        onEvidence={() => {}}
      />,
    );

    expect(markup).toContain('role="group"');
    expect(markup).toContain('Community football');
    expect(markup).toContain('ZA leads NG on source breadth');
    expect(markup).toContain('Building');
    expect(markup).toContain('Four independent source families');
    expect(markup).toContain('Evidence readiness: Thin');
  });

  test.each([
    ['ready', 'Ready', 'Supporting receipts meet the evidence standard'],
    ['thin', 'Thin', 'Some support exists, but important coverage is missing'],
    ['contradictory', 'Contradictory', 'Credible sources disagree'],
    ['unchecked', 'Unchecked', 'Evidence has not been reviewed'],
  ])('EvidenceReadiness labels %s without relying on colour', async (state, label, detail) => {
    const {EvidenceReadiness} = await loadComponent('EvidenceReadiness');
    const markup = renderToStaticMarkup(<EvidenceReadiness state={state} />);

    expect(markup).toContain(`data-state="${state}"`);
    expect(markup).toContain(`Evidence readiness: ${label}. ${detail}`);
    expect(markup).toContain(`>${label}<`);
    expect(markup).toMatch(/aria-hidden="true">[^<]+</);
  });

  test('EvidenceDrawer uses a native modal boundary and restores invoking focus', async () => {
    const {EvidenceDrawer, requestEvidenceClose, restoreEvidenceFocus} = await loadComponent('EvidenceDrawer');
    const reasons = [];
    const event = {preventDefault() { reasons.push('prevented'); }};
    requestEvidenceClose((reason) => reasons.push(reason), 'keyboard', event);
    requestEvidenceClose((reason) => reasons.push(reason), 'button');
    expect(reasons).toEqual(['prevented', 'keyboard', 'button']);

    const focusTarget = {isConnected: true, focus() { reasons.push('restored'); }};
    restoreEvidenceFocus(focusTarget);
    expect(reasons.at(-1)).toBe('restored');

    const markup = renderToStaticMarkup(
      <EvidenceDrawer
        id="signal-proof"
        mode="dialog"
        title="Evidence for neighbourhood running clubs"
        readiness="contradictory"
        onClose={() => {}}
      >
        <p>Receipts</p>
      </EvidenceDrawer>,
    );
    expect(markup).toContain('<dialog');
    expect(markup).toContain('role="dialog"');
    expect(markup).toContain('aria-modal="true"');
    expect(markup).toContain('id="signal-proof-title"');
    expect(markup).toContain('aria-label="Close evidence"');
    expect(markup).toContain('Evidence readiness: Contradictory');

    const jsx = source('EvidenceDrawer');
    expect(jsx.indexOf('useRef(')).toBeLessThan(jsx.indexOf('if (!open)'));
    expect(jsx).toMatch(/showModal\(\)/);
    expect(jsx).toMatch(/document\.activeElement/);
    expect(jsx).toMatch(/if \(dialog\?\.open\) dialog\.close\(\)/);
    expect(jsx).toMatch(/restoreEvidenceFocus\(previousFocus\)/);
    expect(jsx).toMatch(/onCancel=/);
    expect(jsx).toMatch(/requestEvidenceClose\(onClose, 'keyboard'/);
    expect(jsx).toMatch(/requestEvidenceClose\(onClose, 'button'/);
  });

  test('dialog mode without a close contract falls back to a labelled region', async () => {
    const {EvidenceDrawer} = await loadComponent('EvidenceDrawer');
    const markup = renderToStaticMarkup(
      <EvidenceDrawer id="unclosable" mode="dialog" title="Evidence"><p>Receipts</p></EvidenceDrawer>,
    );

    expect(markup).toContain('<aside');
    expect(markup).toContain('role="region"');
    expect(markup).not.toContain('aria-modal');
  });

  test('dossier CSS enforces the approved radius, target, focus, and motion limits', () => {
    const cssUrl = new URL('../../styles/dossier.css', import.meta.url);
    expect(existsSync(fileURLToPath(cssUrl)), 'dossier.css must exist').toBe(true);
    const css = readFileSync(fileURLToPath(cssUrl), 'utf8');
    const tokens = readFileSync(fileURLToPath(new URL('../../tokens.css', import.meta.url)), 'utf8');

    expect(css).not.toMatch(/26px|gradient|glow|text-shadow|filter\s*:|background-image/i);
    expect(css).not.toMatch(/animation(?:-name)?\s*:/i);
    expect(css).toMatch(/min-height:\s*var\(--target-min\)/);
    expect(css).toMatch(/:focus-visible/);
    expect(css).toMatch(/@media\s*\(prefers-reduced-motion:\s*reduce\)/);
    expect(css).toMatch(/transition:\s*none\s*!important/);
    /* The shape law holds every host radius at zero; the dossier's own radius
       and pill tokens stay defined but no rule names them any more. */
    expect(css).toMatch(/border-radius:\s*0/);
    expect(css).not.toMatch(/border-radius:\s*var\(--dossier-(?:radius|radius-large|pill)\)/);
    expect(tokens).toMatch(/--target-min:\s*48px;/);
    expect(tokens).toMatch(/--dossier-radius:\s*var\(--r\);/);
    expect(tokens).toMatch(/--dossier-radius-large:\s*var\(--r-lg\);/);
    expect(tokens).toMatch(/--dossier-pill:\s*999px;/);
  });

  test('component files contain no raw visual effects or font sizes', () => {
    for (const name of componentNames) {
      const jsx = source(name);
      expect(jsx).not.toMatch(/fontSize|font-size|gradient|glow|boxShadow|textShadow|animation/i);
    }
  });
});
