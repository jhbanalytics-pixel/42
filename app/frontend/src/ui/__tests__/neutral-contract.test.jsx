import {afterAll, describe, expect, mock, test} from 'bun:test';
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';

/* Same process wide leak as workspace-navigation: the topic story test below
   stubs api.js, and without this restore the stub follows the run into every
   file bun visits afterwards. */
const realApi = {...(await import('../../api.js'))};

afterAll(() => { mock.module('../../api.js', () => realApi); });

import {briefText} from '../../briefText.js';
import {AudienceUnavailable} from '../AudienceUnavailable.jsx';
import {ResearchPage} from '../../ResearchDocPanel.jsx';
import {
  DocBody,
  OutcomePreviewCard,
  PROGRESS_STEPS,
  ResearchRefineBar,
  migrateResearchDoc,
  outcomePreviewSections,
  progressLabel,
} from '../../researchLib.jsx';
import {TopicDetail} from '../../views.jsx';
import fs from 'node:fs';
import * as researchLib from '../../researchLib.jsx';
import * as workbenchRoute from '../../workbenchRoute.js';
import {GLOSSARY, topicLabel} from '../../model.js';

/* The built UI is checked against the same contract as the source, and bun test
   does not build it. A missing build is still a failure, but it fails with the
   command the reader has not run rather than a bare ENOENT. */
function requireBuilt(relativePath){
  const target = new URL(relativePath, import.meta.url);
  if (!fs.existsSync(target)){
    throw new Error(`${relativePath.replace('../../../../', 'app/')} is missing; run bun run build under app/frontend before bun test`);
  }
  return target;
}

const topic = {
  topic: 'Amapiano',
  region: 'ZA',
  regionName: 'South Africa',
  label: 'Music',
  momentum: 'steady',
  brief: {trend: 'An observed music signal.'},
  voices: [['TikTok', 'A cited voice', '2h']],
  genz: [['TikTok', 'Legacy alias must stay hidden', '1h']],
};

function renderResearchSetup(mobile){
  const hadWindow = Object.prototype.hasOwnProperty.call(globalThis, 'window');
  const originalWindow = globalThis.window;
  globalThis.window = {
    matchMedia: () => ({matches: mobile}),
  };
  try {
    return renderToStaticMarkup(
      <ResearchPage
        routeState={{personaId: '', markets: [], productFrame: ''}}
        onAuth={() => {}}
      />,
    );
  } finally {
    if (hadWindow) globalThis.window = originalWindow;
    else delete globalThis.window;
  }
}

describe('audience neutral V3 contract', () => {
  test('staging defaults and built UI contain no retired audience or product framing', () => {
    const fallbackHtml = fs.readFileSync(new URL('../../../../web/index.html', import.meta.url), 'utf8');
    const readme = fs.readFileSync(new URL('../../../../README.md', import.meta.url), 'utf8');
    const blueprint = fs.readFileSync(new URL('../../../../BLUEPRINT.md', import.meta.url), 'utf8');
    const acceptance = fs.readFileSync(new URL('../../../../docs/jo-research-acceptance.md', import.meta.url), 'utf8');
    const frontendHtml = fs.readFileSync(new URL('../../../index.html', import.meta.url), 'utf8');
    const topicPickerSource = fs.readFileSync(new URL('../../TopicPicker.jsx', import.meta.url), 'utf8');
    const backendMainSource = fs.readFileSync(new URL('../../../../src/api/main.py', import.meta.url), 'utf8');
    const builtHtml = fs.readFileSync(requireBuilt('../../../../web/dist/index.html'), 'utf8');
    const assets = requireBuilt('../../../../web/dist/assets/');
    const builtJs = fs.readdirSync(assets)
      .filter((name) => name.endsWith('.js'))
      .map((name) => fs.readFileSync(new URL(name, assets), 'utf8'))
      .join('\n');
    const visibleDefault = /\bGen(?:\s+|-)Z\b/i;
    const retiredProductName = /Listening Post|\bLP\b/i;

    for (const productText of [fallbackHtml, readme, blueprint, acceptance, frontendHtml, topicPickerSource, backendMainSource, builtHtml, builtJs]) {
      expect(productText).not.toMatch(visibleDefault);
      expect(productText).not.toMatch(retiredProductName);
    }
    expect(topicLabel('genz_lifestyle')).toBe('Lifestyle');
    expect(GLOSSARY.arbantone).not.toMatch(visibleDefault);
    expect(builtJs).not.toMatch(visibleDefault);
  });

  test('copied briefs use voices without a genz fallback', () => {
    const copied = briefText(topic);
    const legacyOnly = briefText({...topic, voices: [], genz: topic.genz});

    expect(copied).toContain('A cited voice');
    expect(copied).not.toContain('Legacy alias must stay hidden');
    expect(legacyOnly).not.toContain('Legacy alias must stay hidden');
  });

  test('topic detail renders voices without a genz fallback', () => {
    const markup = renderToStaticMarkup(<TopicDetail t={topic} onClose={() => {}} />);

    expect(markup).toContain('A cited voice');
    expect(markup).not.toContain('Legacy alias must stay hidden');
  });

  test('historical research docs migrate voice without mutating source data', () => {
    const legacy = {
      grounded: {voice_of_genz: ['A grounded historical voice']},
    };

    const migrated = migrateResearchDoc(legacy);

    expect(migrated.grounded.voice_read).toEqual(['A grounded historical voice']);
    expect(migrated.grounded).not.toHaveProperty('voice_of_genz');
    expect(legacy.grounded).toHaveProperty('voice_of_genz');
  });

  test('topic story renders voices without a genz fallback', async () => {
    mock.module('../../api.js', () => ({
      useApi: () => [{state: 'ready', data: {...topic, id: 'music_amapiano'}}],
      apiGetFresh: () => Promise.resolve({}),
    }));
    const {TopicStory} = await import('../../topic.jsx');

    const markup = renderToStaticMarkup(
      <TopicStory id="music_amapiano" region="ZA" session="test" />,
    );

    expect(markup).toContain('A cited voice');
    expect(markup).not.toContain('Legacy alias must stay hidden');
  });

  test('audience availability renders only for the approved API state', () => {
    const approved = renderToStaticMarkup(
      <AudienceUnavailable
        audience={{state: 'unavailable', reason: 'approved_measurement_not_connected'}}
        marketLabel="ZA"
      />,
    );
    const unknown = renderToStaticMarkup(
      <AudienceUnavailable audience={{state: 'ready'}} marketLabel="ZA" />,
    );

    expect(approved).toContain('Audience measurement not connected');
    expect(unknown).toBe('');
  });

  test('research progress keeps retired source names out of strategist copy', () => {
    expect(PROGRESS_STEPS.map((step) => step.label)).toEqual([
      'Initial observations',
      'Engine evidence',
      'Editorial synthesis',
    ]);
  });

  test('research preview defaults to an evidence-bounded possible response', () => {
    const preview = outcomePreviewSections('', ['za', 'ng']);
    const rendered = JSON.stringify(preview);

    expect(preview.lanes[2]).toEqual({
      id: 'activation',
      label: 'Possible response',
      title: 'Test for a credible brand role',
      summary: 'Will test user friction, brand permission, execution and available proof.',
      items: ['User friction', 'Brand role', 'Creative execution'],
    });
    expect(rendered).not.toContain('Google');
    expect(rendered).not.toContain('AI Mode');
  });

  test('research preview retains an explicitly selected product frame', () => {
    const preview = outcomePreviewSections('Mobile banking service', ['ke']);

    expect(preview.lanes[2].title).toBe('Test the role of Mobile banking service');
    expect(preview.lanes[2].summary).toBe(
      'Will test user friction, brand permission, execution and available proof.',
    );
  });

  test('market route changes preserve a custom or explicitly blank frame', () => {
    const personas = [{id: 'audience_neutral', product_frame: 'Legacy default'}];
    const route = {personaId: 'audience_neutral', markets: ['za', 'ke']};

    expect(typeof workbenchRoute.resolveResearchRouteUpdate).toBe('function');
    expect(workbenchRoute.resolveResearchRouteUpdate(
      route, personas, 'audience_neutral', 'Custom role',
    )).toEqual({
      personaId: 'audience_neutral', productFrame: 'Custom role', markets: ['za', 'ke'],
    });
    expect(workbenchRoute.resolveResearchRouteUpdate(
      route, personas, 'audience_neutral', '',
    ).productFrame).toBe('');
  });

  test('research requests preserve an explicit blank product frame', () => {
    expect(typeof researchLib.normalizeResearchProductFrame).toBe('function');
    expect(researchLib.normalizeResearchProductFrame('  Mobile service  ')).toBe('Mobile service');
    expect(researchLib.normalizeResearchProductFrame('')).toBe('');
    expect(researchLib.normalizeResearchProductFrame(undefined)).toBe('');
  });

  test('the pre-run blueprint is a static evidence-required plan', () => {
    const markup = renderToStaticMarkup(
      <OutcomePreviewCard productFrame="" markets={['za']} />,
    );

    expect(markup).toContain('Planned structure · evidence required');
    expect(markup).toContain('role="region"');
    expect(markup).toContain('aria-label="Research plan preview"');
    expect(markup).not.toContain('role="status"');
    expect(markup).not.toContain('activation-ready');
    expect(markup).not.toContain('Cited ·');
  });

  test('mobile research places the evidence-required blueprint before selection controls', () => {
    const markup = renderResearchSetup(true);
    const blueprint = markup.indexOf('aria-label="Research plan preview"');
    const controls = markup.indexOf('class="research-controls"');
    const persona = markup.indexOf('Persona lens');

    expect(blueprint).toBeGreaterThan(-1);
    expect(controls).toBeGreaterThan(-1);
    expect(blueprint).toBeLessThan(controls);
    expect(persona).toBeGreaterThan(-1);
    expect(blueprint).toBeLessThan(persona);
    expect(markup.match(/aria-label="Research plan preview"/g)).toHaveLength(1);
  });

  test('desktop research keeps the evidence-required blueprint after selection controls', () => {
    const markup = renderResearchSetup(false);
    const blueprint = markup.indexOf('aria-label="Research plan preview"');
    const markets = markup.indexOf('>Markets<');

    expect(blueprint).toBeGreaterThan(markets);
    expect(markup.match(/aria-label="Research plan preview"/g)).toHaveLength(1);
  });

  test('legacy activation fields render as neutral possible responses', () => {
    const markup = renderToStaticMarkup(
      <DocBody
        json={{
          grounded: {},
          inference: {},
          activation: {
            google_prompts: [{
              market: 'za',
              prompt: 'Show the evidence-led response.',
              ai_mode_role: 'Help people compare the proof.',
            }],
          },
        }}
        markdown=""
        sources={[]}
        signalQuality={{}}
        onRef={() => {}}
      />,
    );

    expect(markup).toContain('Possible responses');
    expect(markup).toContain('Brand role');
    expect(markup).not.toContain('Google AI Mode');
    expect(markup).not.toContain('AI Mode role');
  });

  test('malformed historical activation shapes remain an honest empty section', () => {
    for (const googlePrompts of [[null], {}]) {
      const markup = renderToStaticMarkup(
        <DocBody
          json={{grounded: {}, inference: {}, activation: {google_prompts: googlePrompts}}}
          markdown=""
          sources={[]}
          signalQuality={{}}
          onRef={() => {}}
        />,
      );

      expect(markup).not.toContain('Possible responses');
      expect(markup).not.toContain('Google AI Mode');
    }
  });

  test('research controls contain no retired product framing', () => {
    const refine = renderToStaticMarkup(
      <ResearchRefineBar artifactId="artifact_1" busy={false} onRefine={() => {}} />,
    );
    const panelSource = fs.readFileSync(new URL('../../ResearchDocPanel.jsx', import.meta.url), 'utf8');

    expect(refine).toContain('Tighter evidence path');
    expect(refine).not.toContain('Google AI Mode');
    expect(panelSource).not.toContain('Google AI Mode (positioning lens for activation prompts)');
    expect(panelSource).not.toContain('Google AI Model');
  });

  test('research route primary controls have a 48 pixel target floor', () => {
    const css = fs.readFileSync(new URL('../../app.css', import.meta.url), 'utf8');

    expect(css).toContain(`:is(
  .skip-link,
  .research-persona-btn,
  .research-market-btn,
  .research-frame-chip,
  .research-product-frame-input,
  .research-generate-btn,
  .research-cancel-btn,
  .research-back,
  .research-ask-bridge-clear,
  .research-copy-btn,
  .research-evidence-card
) {
  min-height: 48px;
}`);
    expect(css).toContain('.research-market-btn { min-width: 48px; }');
  });
});
