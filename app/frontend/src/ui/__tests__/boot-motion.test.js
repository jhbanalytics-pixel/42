import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import postcss from 'postcss';
import {splitInstrumentStylesheet} from '../../instrumentStylesheetSplit.mjs';

const boot = postcss.parse(readFileSync(fileURLToPath(new URL('../../styles/boot.css', import.meta.url)), 'utf8'));
const app = postcss.parse(readFileSync(fileURLToPath(new URL('../../app.css', import.meta.url)), 'utf8'));
const packageCss = readFileSync(fileURLToPath(new URL('../../../node_modules/ogilvy-intelligence-design-system/dist/style.css', import.meta.url)), 'utf8');
const critical = postcss.parse(splitInstrumentStylesheet(packageCss).critical);
const mainSource = readFileSync(fileURLToPath(new URL('../../main.jsx', import.meta.url)), 'utf8');
const appSource = readFileSync(fileURLToPath(new URL('../../App.jsx', import.meta.url)), 'utf8');

function reducedRule(root, selector){
  let found = null;
  root.walkAtRules('media', (media) => {
    if (!/prefers-reduced-motion\s*:\s*reduce/.test(media.params)) return;
    media.walkRules((rule) => {
      if (rule.selectors.includes(selector)) found = rule;
    });
  });
  return found;
}

function declarations(rule){
  return Object.fromEntries(rule.nodes.filter((node) => node.type === 'decl').map((node) => [node.prop, {value: node.value.trim(), important: Boolean(node.important)}]));
}

test('the first paint critical rule settles the boot wait before deferred styles load', () => {
  const criticalImport = mainSource.indexOf("import 'ogilvy-intelligence-design-system/style.critical.css';");
  const bootImport = mainSource.indexOf("import './styles/boot.css';");
  const deferredImport = mainSource.indexOf("import('ogilvy-intelligence-design-system/style.deferred.css')");
  expect(criticalImport).toBeGreaterThanOrEqual(0);
  expect(bootImport).toBeGreaterThan(criticalImport);
  expect(deferredImport).toBeGreaterThan(bootImport);
  expect(appSource).toContain("document.documentElement.setAttribute('data-motion', query.matches ? 'off' : 'on');");
  let wait = null;
  boot.walkRules((rule) => { if (rule.selector === '.page.route-wait-enter') wait = rule; });
  expect(wait).not.toBeNull();
  if (!wait) return;
  expect(declarations(wait).animation.value).toContain('routeWaitIn');
  let reduced = null;
  critical.walkAtRules('media', (media) => {
    if (!media.params.includes('prefers-reduced-motion')) return;
    media.walkRules((rule) => {
      const selectors = rule.selectors;
      if (selectors.length === 3 && selectors[0] === '*' && selectors[1].endsWith(':before') && selectors[2].endsWith(':after')) reduced = rule;
    });
  });
  expect(reduced).not.toBeNull();
  if (!reduced) return;
  expect(declarations(reduced)).toMatchObject({
    animation: {value: 'none', important: true},
    transition: {value: 'none', important: true},
    'scroll-behavior': {value: 'auto', important: true},
  });
});

test('the live app reduced-motion media and off state stay immediate', () => {
  const reduced = reducedRule(app, '*');
  expect(reduced, 'app.css needs a live reduced-motion rule').not.toBeNull();
  if (!reduced) return;
  expect(declarations(reduced)).toMatchObject({
    'transition-duration': {value: '0s', important: true},
    'transition-delay': {value: '0s', important: true},
    'animation-duration': {value: '0s', important: true},
    'animation-delay': {value: '0s', important: true},
  });
  let off = null;
  app.walkRules((rule) => {
    if (rule.selectors.includes('[data-motion="off"] *')) off = rule;
  });
  expect(off, 'app.css needs an attribute-driven off state').not.toBeNull();
  if (!off) return;
  expect(declarations(off)).toMatchObject({
    animation: {value: 'none', important: true},
    transition: {value: 'none', important: true},
  });
});
