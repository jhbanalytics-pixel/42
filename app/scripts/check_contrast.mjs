/* WCAG AA contrast gate over the design tokens in frontend/src/tokens.css.
   Text pairs must reach 4.5:1 in both themes. The palette is tuned to this
   gate; if it fails, fix the token, not the gate. Tokens are oklch(); the
   gate converts to sRGB and composites alpha colors over their backdrop. */
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import postcss from '../frontend/node_modules/postcss/lib/postcss.mjs';

const cssPath = fileURLToPath(new URL('../frontend/src/tokens.css', import.meta.url));
const css = readFileSync(cssPath, 'utf8');

/* The token system is two-tier: a :root primitives block holds the raw oklch
   scales, and each [data-dir] semantic block references them by var(--p-...).
   The accent-family override blocks still carry oklch literals directly. To
   check contrast we resolve one level of var() indirection against the
   primitives before parsing, so the gate reads the same resolved values the
   browser would. */
function parsePrimitives(){
  const m = css.match(/:root\s*\{([\s\S]*?)\n\}/);
  const prims = {};
  if (!m) return prims;
  const re = /(--p-[a-z0-9-]+)\s*:\s*(oklch\([^)]*\))\s*;/g;
  let t;
  while ((t = re.exec(m[1])) !== null) prims[t[1]] = t[2];
  return prims;
}
const primitives = parsePrimitives();

function resolveValue(raw){
  const ref = raw.match(/^var\(\s*(--[a-z0-9-]+)\s*\)$/);
  if (ref && primitives[ref[1]]) return primitives[ref[1]];
  return raw;
}

function parseBlock(re, label){
  const m = css.match(re);
  if (!m){
    console.error('FAIL: could not find the ' + label + ' token block in frontend/src/tokens.css');
    process.exit(1);
  }
  const tokens = {};
  /* accept an oklch literal, a var(--p-...) reference to a primitive, or a
     var(--x) reference to another semantic token already declared earlier in
     this same block (e.g. --header-muted: var(--muted) in midnight) */
  const tokRe = /--([a-z0-9-]+)\s*:\s*(oklch\([^)]*\)|var\(\s*--[a-z0-9-]+\s*\))\s*;/g;
  let t;
  while ((t = tokRe.exec(m[1])) !== null){
    const raw = t[2];
    const ref = raw.match(/^var\(\s*(--[a-z0-9-]+)\s*\)$/);
    if (ref && !primitives[ref[1]] && tokens[ref[1].slice(2)]){
      tokens[t[1]] = tokens[ref[1].slice(2)];
    } else {
      tokens[t[1]] = resolveValue(raw);
    }
  }
  return tokens;
}

const midnight = parseBlock(/\[data-dir="midnight"\]\s*\{([\s\S]*?)\}/, 'midnight');
const daylight = parseBlock(/\[data-dir="daylight"\]\s*\{([\s\S]*?)\}/, 'daylight');

/* oklch() string -> gamma-encoded sRGB components 0..1 plus alpha */
function parseOklch(str){
  const m = str.match(/oklch\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s*\/\s*([\d.]+%?))?\s*\)/);
  if (!m) return null;
  const L = parseFloat(m[1]), C = parseFloat(m[2]), H = parseFloat(m[3]);
  let alpha = 1;
  if (m[4]) alpha = m[4].endsWith('%') ? parseFloat(m[4]) / 100 : parseFloat(m[4]);
  const hr = (H * Math.PI) / 180;
  const a = C * Math.cos(hr), b = C * Math.sin(hr);
  const l_ = L + 0.3963377774 * a + 0.2158037573 * b;
  const m_ = L - 0.1055613458 * a - 0.0638541728 * b;
  const s_ = L - 0.0894841775 * a - 1.2914855480 * b;
  const l3 = l_ ** 3, m3 = m_ ** 3, s3 = s_ ** 3;
  const lin = [
    +4.0767416621 * l3 - 3.3077115913 * m3 + 0.2309699292 * s3,
    -1.2684380046 * l3 + 2.6097574011 * m3 - 0.3413193965 * s3,
    -0.0041960863 * l3 - 0.7034186147 * m3 + 1.7076147010 * s3,
  ];
  const enc = v => {
    v = Math.min(1, Math.max(0, v));
    return v <= 0.0031308 ? 12.92 * v : 1.055 * Math.pow(v, 1 / 2.4) - 0.055;
  };
  return {r: enc(lin[0]), g: enc(lin[1]), b: enc(lin[2]), alpha: alpha};
}

/* composite a (possibly translucent) color over an opaque backdrop */
function over(fg, bg){
  const a = fg.alpha;
  return {
    r: fg.r * a + bg.r * (1 - a),
    g: fg.g * a + bg.g * (1 - a),
    b: fg.b * a + bg.b * (1 - a),
    alpha: 1
  };
}

function luminance(c){
  const lin = v => (v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4));
  return 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
}
function ratio(a, b){
  const la = luminance(a), lb = luminance(b);
  const hi = Math.max(la, lb), lo = Math.min(la, lb);
  return (hi + 0.05) / (lo + 0.05);
}

let failures = 0;
function check(themeLabel, tokens){
  const col = name => {
    const raw = tokens[name];
    if (!raw){ console.error('FAIL [' + themeLabel + '] missing token: --' + name); failures += 1; return null; }
    const c = parseOklch(raw);
    if (!c){ console.error('FAIL [' + themeLabel + '] unparseable token: --' + name + ' = ' + raw); failures += 1; return null; }
    return c;
  };
  const ink = col('ink'), canvas = col('canvas'), surface = col('surface');
  const surface2 = col('surface-2'), surfaceInset = col('surface-inset');
  const accent = col('accent'), accentSoft = col('accent-soft');
  const muted = col('muted'), up = col('up'), upSoft = col('up-soft');
  const headerMuted = col('header-muted'), headerBg = col('header-bg');
  const building = col('building'), steady = col('steady');
  if (!ink || !canvas || !surface || !accent || !accentSoft || !muted || !up || !upSoft) return;

  const pairs = [
    [ink, canvas, 'ink on canvas'],
    [ink, surface, 'ink on surface'],
    /* the accent-soft pill is translucent accent over a surface */
    [accent, over(accentSoft, surface), 'accent text on accent-soft pill'],
    /* DigestChip neutral state: muted text on the plain surface background */
    [muted, surface, 'digest chip neutral text on surface'],
    /* DigestChip hover/focus state swaps in the accent pair, same fill as the
       accent-soft pill above, so accent-on-surface (the chip's flat state,
       border-only, no tint) also needs its own check since the border itself
       carries no text contrast requirement but the hovered label color does */
    [accent, surface, 'digest chip hover text on surface'],
    /* DigestChip rising state: up text on the translucent up-soft fill,
       composited over surface the same way the accent-soft pill is */
    [up, over(upSoft, surface), 'digest chip rising text on up-soft fill'],
    [building, surface, 'building text on surface'],
    [building, surfaceInset, 'building text on surface-inset'],
    [steady, surface, 'steady text on surface'],
    [steady, canvas, 'steady text on canvas'],
    [steady, surface2, 'steady text on surface-2'],
    [steady, surfaceInset, 'steady text on surface-inset'],
  ];
  /* FlowBar's pending step sits on --flowbar-bg, which both themes alias to
     --header-bg. --faint on header-bg fails AA in daylight (3.41:1), so the
     pending step reads --header-muted instead, a token already tuned for
     header-bg. Check that actual pair rather than the failing faint one. */
  if (headerMuted && headerBg) pairs.push([headerMuted, headerBg, 'flowbar pending step text on header-bg']);
  for (const [fg, bg, what] of pairs){
    const r = ratio(fg, bg);
    const ok = r >= 4.5;
    const line = (ok ? 'PASS' : 'FAIL') + ' [' + themeLabel + '] ' + what + ' = ' + r.toFixed(2) + ':1 (needs 4.5:1)';
    if (ok) console.log(line); else { console.error(line); failures += 1; }
  }
}

/* Entry screen. The gate paints the instrument's ink planes: the field and
   masthead sit on the canvas, the access column on the lit panel. It pins
   those planes to the package's colour roles rather than to --oi-ink and
   --oi-paper, which are text and surface aliases whose values swap between
   midnight and daylight; painted as pigments they inverted the whole gate
   under midnight. Colours resolve through the signed package stylesheet, so a
   package palette move carries the gate with it. Every pair below is measured
   again on the built page, in both themes, by
   frontend/src/ui/__tests__/gate-contrast.test.jsx. */
const gateCss = readFileSync(fileURLToPath(new URL('../frontend/src/styles/gate.css', import.meta.url)), 'utf8');
const packageCssPath = fileURLToPath(new URL('../frontend/node_modules/ogilvy-intelligence-design-system/dist/style.css', import.meta.url));

function packageDeclarations(source, selector){
  const start = source.indexOf(selector);
  if (start < 0) return {};
  const open = source.indexOf('{', start);
  const close = source.indexOf('}', open);
  const declarations = {};
  const re = /--([a-z0-9-]+)\s*:\s*([^;}]+)/g;
  let match;
  while ((match = re.exec(source.slice(open + 1, close))) !== null){
    declarations['--' + match[1]] = match[2].trim();
  }
  return declarations;
}

function gateColour(){
  let packageCss;
  try {
    packageCss = readFileSync(packageCssPath, 'utf8');
  } catch {
    console.error('FAIL [gate] the signed package stylesheet is not installed; run bun install in frontend');
    failures += 1;
    return null;
  }
  const scopes = [
    packageDeclarations(packageCss, '.oi-product'),
    packageDeclarations(packageCss, '[data-dir=daylight]'),
    packageDeclarations(packageCss, ':root'),
  ];
  const resolve = (name, depth = 0) => {
    if (depth > 8) return null;
    if (/^#[0-9a-f]{3,8}$/i.test(name)) return name;
    const raw = scopes.map((scope) => scope[name]).find(Boolean);
    if (!raw) return null;
    const reference = raw.match(/^var\(\s*(--[a-z0-9-]+)\s*\)$/);
    return reference ? resolve(reference[1], depth + 1) : (/^#[0-9a-f]{3,8}$/i.test(raw) ? raw : null);
  };
  return resolve;
}

function hexColour(hex){
  const value = hex.replace('#', '');
  const full = value.length === 3 ? value.split('').map((c) => c + c).join('') : value;
  return {
    r: parseInt(full.slice(0, 2), 16) / 255,
    g: parseInt(full.slice(2, 4), 16) / 255,
    b: parseInt(full.slice(4, 6), 16) / 255,
    alpha: 1,
  };
}

/* last winning declaration of `property` for an exact selector in a flat
   stylesheet. The gate is one flat file with no nesting, so an exact head
   match plus last-one-wins is the same answer the cascade gives. */
function declaredProperty(source, selector, property){
  const css = source.replace(/\/\*[\s\S]*?\*\//g, '');
  let found = null;
  let index = 0;
  while (index < css.length){
    const open = css.indexOf('{', index);
    if (open < 0) break;
    const close = css.indexOf('}', open);
    if (close < 0) break;
    const heads = css.slice(index, open).split(',').map((part) => part.trim());
    if (heads.includes(selector)){
      const declaration = css.slice(open + 1, close).split(';')
        .map((part) => part.trim())
        .find((part) => part.startsWith(property + ':'));
      if (declaration) found = declaration.slice(declaration.indexOf(':') + 1).trim();
    }
    index = close + 1;
  }
  return found;
}

/* The gate declares its own plane tokens on .gate-v4 and points each one at a
   package colour role, so a gate token resolves in two hops: gate.css for the
   role, then the package for the value. */
function gateToken(name){
  const value = declaredProperty(gateCss, '.gate-v4', name);
  if (!value) return null;
  const reference = value.match(/^var\(\s*(--[a-z0-9-]+)\s*\)$/);
  return reference ? reference[1] : value;
}

function checkGate(){
  const resolve = gateColour();
  if (!resolve) return;
  const colour = (token) => {
    if (!token) return null;
    const gate = token.startsWith('--gate-') ? gateToken(token) : token;
    return gate ? resolve(gate) : null;
  };
  const plane = (selector) => {
    const declared = declaredProperty(gateCss, selector, 'background');
    if (!declared) return null;
    const reference = declared.match(/^var\(\s*(--[a-z0-9-]+)\s*\)$/);
    return colour(reference ? reference[1] : declared);
  };
  const canvas = plane('.gate-v4');
  const panel = plane('.gate-v4-access');
  if (!canvas || !panel){
    console.error('FAIL [gate] could not resolve the gate planes from frontend/src/styles/gate.css');
    failures += 1;
    return;
  }
  /* Every text node the built gate paints, per plane, at the size threshold
     WCAG gives it. The hero numeral, the masthead wordmark and the system
     folios are large text at 3:1; everything else is body text at 4.5:1. */
  const pairs = [
    ['.gate-v4', canvas, 4.5, 'entry screen body text on the field'],
    ['.gate-v4-mast strong', canvas, 3, 'entry screen masthead wordmark on the field'],
    ['.gate-v4-mast p:first-child span', canvas, 4.5, 'entry screen masthead name on the field'],
    ['.gate-v4-mast p:last-child span', canvas, 4.5, 'entry screen masthead market on the field'],
    ['.gate-v4-mast p:last-child strong', canvas, 4.5, 'entry screen masthead label on the field'],
    ['.gate-v4-number', canvas, 3, 'entry screen hero numeral on the field'],
    ['.gate-v4-kicker', canvas, 4.5, 'entry screen kicker on the field'],
    ['.gate-v4-deck', canvas, 4.5, 'entry screen deck on the field'],
    ['.gate-v4-system dt', canvas, 3, 'entry screen system folio on the field'],
    ['.gate-v4-system span', canvas, 4.5, 'entry screen system note on the field'],
    ['.gate-v4-access', panel, 4.5, 'entry screen access body text on the access panel'],
    ['.gate-v4-folio', panel, 4.5, 'entry screen access folio on the access panel'],
    ['.gate-v4-access-copy', panel, 4.5, 'entry screen access copy on the access panel'],
    ['.gate-v4-form input', panel, 4.5, 'entry screen key field text on the access panel'],
    ['.gate-v4-status', panel, 4.5, 'entry screen status on the access panel'],
    ['.gate-v4-status.is-error', panel, 4.5, 'entry screen error status on the access panel'],
    ['.gate-v4-foot', panel, 4.5, 'entry screen footer on the access panel'],
    ['.gate-v4-submit', plane('.gate-v4-submit'), 4.5, 'entry screen submit label on its own plane'],
    ['.gate-v4-submit:hover', plane('.gate-v4-submit:hover'), 4.5, 'entry screen hovered submit label on the red plane'],
  ];
  for (const [selector, background, need, what] of pairs){
    const rule = declaredProperty(gateCss, selector, 'color');
    if (!rule){
      console.error('FAIL [gate] no colour declaration for ' + selector + ' in frontend/src/styles/gate.css');
      failures += 1;
      continue;
    }
    const reference = rule.match(/^var\(\s*(--[a-z0-9-]+)\s*\)$/);
    const hex = colour(reference ? reference[1] : rule);
    if (!hex || !background){
      console.error('FAIL [gate] could not resolve ' + selector + ' colour ' + rule);
      failures += 1;
      continue;
    }
    const r = ratio(hexColour(hex), hexColour(background));
    const ok = r >= need;
    const line = (ok ? 'PASS' : 'FAIL') + ' [gate] ' + what + ' ' + hex + ' on ' + background
      + ' = ' + r.toFixed(2) + ':1 (needs ' + need + ':1)';
    if (ok) console.log(line); else { console.error(line); failures += 1; }
  }
  /* The key field's underline is the only thing identifying that control, so
     it carries the 3:1 that non-text contrast requires. */
  const underline = declaredProperty(gateCss, '.gate-v4-form input', 'border-bottom');
  const underlineReference = underline && underline.match(/var\(\s*(--[a-z0-9-]+)\s*\)/);
  const underlineHex = underlineReference ? colour(underlineReference[1]) : null;
  if (!underlineHex){
    console.error('FAIL [gate] could not resolve the key field underline colour');
    failures += 1;
  } else {
    const r = ratio(hexColour(underlineHex), hexColour(panel));
    const ok = r >= 3;
    const line = (ok ? 'PASS' : 'FAIL') + ' [gate] entry screen key field underline ' + underlineHex
      + ' on ' + panel + ' = ' + r.toFixed(2) + ':1 (needs 3:1)';
    if (ok) console.log(line); else { console.error(line); failures += 1; }
  }
}


/* The console workbench. The workbench is host chrome drawn by three consumer
   sheets, ui.css, app.css and styles/console.css, and it resolves every colour
   through the package palette, since tokens.css is no longer loaded. The
   audit found ninety-two failing text runs on this route, faint and the plane
   red painted as text and one label painted in its own background colour, so
   this walk reads the three sheets as source, resolves each console rule's
   winning colour the way the cascade would, and holds it to the contrast the
   addendum's section 12 table gives it, in both themes.

   The console sheet may restate an inherited rule under the .workbench scope,
   which outranks the shared sheet by specificity. A rule's identity here is
   its selector with that scope stripped, so the restatement wins the same way
   in this walk as it does on the page. Scope blocks on .workbench itself, and
   on [data-dir="daylight"] .workbench, carry token moves that the resolver
   layers over the package scopes. */
const consoleSheets = ['ui/ui.css', 'app.css', 'styles/console.css'].map((name) => ({
  name,
  css: readFileSync(fileURLToPath(new URL('../frontend/src/' + name, import.meta.url)), 'utf8'),
}));
/* Anchored at the start of a compound rather than of the selector, so a rule
   that reaches a console class behind an attribute or an element is walked
   too. A rule scoped to daylight is read under daylight alone. */
const CONSOLE_SELECTOR = /(?:^|[\s>+~])\.(?:workbench|bscan|research-|ui-flowbar|ui-page-hero|ui-market|investigation|workspace-)/;
const RULED_OUT_AS_TEXT = {'#5c554f': 'faint', '#2a2521': 'rule', '#e41424': 'the plane red'};
const WORKBENCH_SCOPE = /^\.workbench\s+/;
const DAYLIGHT_SCOPE = /^\[data-dir="daylight"\]\s+/;

function specificity(selector){
  const ids = (selector.match(/#[a-z0-9_-]+/gi) || []).length;
  const classes = (selector.match(/\.[a-z0-9_-]+|\[[^\]]+\]|(?<!:):(?!:)[a-z-]+(?:\([^)]*\))?/gi) || []).length;
  const elements = (selector.match(/(?:^|[\s>+~(])[a-z][a-z0-9-]*/gi) || []).length;
  return ids * 100 + classes * 10 + elements;
}

/* Every declaration in every console rule, in cascade order, keyed by the
   scope-stripped selector. Media blocks are left out: the walk measures the
   desk composition, and the built page measurement covers the phone. */
function consoleRules(theme){
  const rules = new Map();
  const scopes = {workbench: {}, daylight: {}, flowbar: {}};
  let order = 0;
  for (const sheet of consoleSheets){
    postcss.parse(sheet.css).walkRules((rule) => {
      if (rule.parent.type !== 'root') return;
      for (const selector of rule.selectors){
        const declarations = {};
        rule.walkDecls((declaration) => { declarations[declaration.prop] = declaration.value.trim(); });
        if (selector === '.workbench'){ Object.assign(scopes.workbench, declarations); continue; }
        if (selector === '[data-dir="daylight"] .workbench'){ Object.assign(scopes.daylight, declarations); continue; }
        if (selector === '[data-dir="daylight"] .workbench .ui-flowbar'){ Object.assign(scopes.flowbar, declarations); continue; }
        if (DAYLIGHT_SCOPE.test(selector) && theme !== 'daylight') continue;
        const key = selector.replace(DAYLIGHT_SCOPE, '').replace(WORKBENCH_SCOPE, '');
        if (!CONSOLE_SELECTOR.test(key)) continue;
        if (!rules.has(key)) rules.set(key, {});
        const entry = rules.get(key);
        const weight = specificity(selector);
        order += 1;
        for (const [property, value] of Object.entries(declarations)){
          const current = entry[property];
          if (!current || weight > current.weight || (weight === current.weight && order > current.order)){
            entry[property] = {value, weight, order, sheet: sheet.name, line: rule.source.start.line};
          }
        }
      }
    });
  }
  return {rules, scopes};
}

/* The package's own scopes, innermost first. A selector may head more than
   one block in the package sheet, so every block is merged. */
function packageScopes(theme){
  const packageCss = readFileSync(packageCssPath, 'utf8');
  const all = (selector) => {
    const merged = {};
    let from = 0;
    while (true){
      const start = packageCss.indexOf(selector + '{', from);
      if (start < 0) break;
      const open = packageCss.indexOf('{', start);
      const close = packageCss.indexOf('}', open);
      const re = /--([a-z0-9-]+)\s*:\s*([^;}]+)/g;
      let match;
      while ((match = re.exec(packageCss.slice(open + 1, close))) !== null) merged['--' + match[1]] = match[2].trim();
      from = close + 1;
    }
    return merged;
  };
  return [all('[data-dir=' + theme + ']'), all('[data-dir]'), all('.oi-product'), all(':root')];
}

function splitTop(value){
  const parts = [];
  let depth = 0;
  let current = '';
  for (const char of value){
    if (char === '(') depth += 1;
    if (char === ')') depth -= 1;
    if (char === ',' && depth === 0){ parts.push(current.trim()); current = ''; } else current += char;
  }
  if (current.trim()) parts.push(current.trim());
  return parts;
}

/* A colour value, resolved through the scope chain to sRGB with alpha, or
   null where the value is not a single colour (a gradient, inherit, none). */
function makeResolver(scopeChain){
  const lookup = (name) => scopeChain.map((scope) => scope[name]).find((value) => value !== undefined);
  const evaluate = (raw, depth = 0) => {
    if (depth > 12 || !raw) return null;
    const value = raw.trim();
    if (/^#[0-9a-f]{3,8}$/i.test(value)) return hexColour(value);
    if (value === 'transparent') return {r: 0, g: 0, b: 0, alpha: 0};
    const reference = value.match(/^var\(\s*(--[a-z0-9-]+)\s*(?:,\s*([\s\S]*))?\)$/);
    if (reference){
      const declared = lookup(reference[1]);
      if (declared !== undefined) return evaluate(declared, depth + 1);
      return reference[2] ? evaluate(reference[2], depth + 1) : null;
    }
    const mix = value.match(/^color-mix\(\s*in\s+[a-z-]+\s*,([\s\S]*)\)$/);
    if (mix){
      const [first, second] = splitTop(mix[1]);
      const part = (term) => {
        const share = term.match(/^([\s\S]*?)\s+([\d.]+)%$/);
        return {colour: evaluate(share ? share[1] : term, depth + 1), share: share ? parseFloat(share[2]) / 100 : null};
      };
      const a = part(first), b = part(second);
      if (!a.colour || !b.colour) return null;
      const pa = a.share !== null ? a.share : (b.share !== null ? 1 - b.share : 0.5);
      const pb = 1 - pa;
      const alpha = a.colour.alpha * pa + b.colour.alpha * pb;
      if (alpha === 0) return {r: 0, g: 0, b: 0, alpha: 0};
      const channel = (k) => (a.colour[k] * a.colour.alpha * pa + b.colour[k] * b.colour.alpha * pb) / alpha;
      return {r: channel('r'), g: channel('g'), b: channel('b'), alpha};
    }
    return null;
  };
  return {evaluate, lookup};
}

function toHex(colour){
  return '#' + ['r', 'g', 'b'].map((k) => Math.round(colour[k] * 255).toString(16).padStart(2, '0')).join('');
}

function pixels(resolver, raw){
  if (!raw) return null;
  const value = raw.trim();
  const reference = value.match(/^var\(\s*(--[a-z0-9-]+)\s*\)$/);
  if (reference){
    const declared = resolver.lookup(reference[1]);
    return declared ? pixels(resolver, declared) : null;
  }
  const clamp = value.match(/^clamp\(\s*([\d.]+)px/);
  if (clamp) return parseFloat(clamp[1]);
  const px = value.match(/^([\d.]+)px$/);
  return px ? parseFloat(px[1]) : null;
}

/* Controls whose boundary is the only thing identifying them. Section 12:
   that boundary carries muted or the plane red, at 3:1 against its plane. */
const IDENTIFYING_BOUNDARIES = [
  ['.ui-flowbar-chip', 'border'],
  ['.workbench-mode-tabs button', 'border'],
  ['.workbench-back', 'border'],
  ['.workbench-new-brief', 'border'],
  ['.workbench-action-card', 'border'],
  ['.research-persona-card', 'border'],
  ['.bscan-approve', 'border'],
  ['.bscan-reject', 'border'],
  ['.investigation-field textarea', 'border-bottom'],
  ['.investigation-frame__submit', 'border'],
  /* the brief flow behind the landing */
  ['.research-market-btn', 'border'],
  ['.research-frame-chip', 'border'],
  ['.research-chip', 'border'],
  ['.research-topic-chip', 'border'],
  ['.research-copy-btn', 'border'],
  ['.research-cancel-btn', 'border'],
  ['.research-back', 'border'],
  ['.research-inline-action', 'border'],
  ['.research-product-frame-input', 'border'],
  ['.research-ask-bridge-clear', 'border'],
  ['.research-brief-tab', 'border'],
  ['.bscan-btn', 'border'],
  ['.bscan-filter-chip', 'border'],
  ['.bscan-more', 'border'],
];

function checkConsole(){
  let runs = 0;
  let ruledOut = 0;
  let failing = 0;
  for (const theme of ['midnight', 'daylight']){
    const {rules, scopes} = consoleRules(theme);
    const themeScopes = theme === 'daylight' ? [scopes.daylight, scopes.workbench] : [scopes.workbench];
    const base = makeResolver([...themeScopes, ...packageScopes(theme)]);
    const flowbar = makeResolver([...(theme === 'daylight' ? [scopes.flowbar] : []), ...themeScopes, ...packageScopes(theme)]);
    const planes = new Map();
    for (const token of ['--surface', '--surface-inset', '--surface-2', '--flowbar-bg']){
      const resolver = token === '--flowbar-bg' ? flowbar : base;
      const colour = resolver.evaluate('var(' + token + ')');
      if (colour && colour.alpha === 1) planes.set(toHex(colour), token);
    }
    if (planes.size === 0){
      console.error('FAIL [console/' + theme + '] could not resolve the workbench planes');
      failures += 1;
      continue;
    }
    const label = '[console/' + theme + ']';
    for (const [selector, entry] of rules){
      const resolver = selector.startsWith('.ui-flowbar') ? flowbar : base;
      const colourRule = entry.color;
      if (!colourRule) continue;
      const foreground = resolver.evaluate(colourRule.value);
      if (!foreground) continue;
      const place = colourRule.sheet + ':' + colourRule.line;
      const name = RULED_OUT_AS_TEXT[toHex(foreground)];
      if (foreground.alpha === 1 && name){
        console.error('FAIL ' + label + ' ' + selector + ' paints text in ' + name + ' ' + toHex(foreground) + ' (' + place + ')');
        ruledOut += 1;
        failures += 1;
      }
      const size = pixels(resolver, entry['font-size'] ? entry['font-size'].value : null);
      const weight = entry['font-weight'] ? parseInt(entry['font-weight'].value, 10) || 400 : 400;
      const large = size !== null && (size >= 24 || (size >= 18.66 && weight >= 700));
      const need = large ? 3 : 4.5;
      const backgroundRule = entry['background-color'] || entry.background;
      const declared = backgroundRule ? resolver.evaluate(backgroundRule.value) : null;
      const backdrops = declared && declared.alpha === 1
        ? [[declared, backgroundRule.value]]
        : [...planes.entries()].map(([hex, token]) => [declared ? over(declared, hexColour(hex)) : hexColour(hex), token + (declared ? ' under ' + backgroundRule.value : '')]);
      for (const [backdrop, what] of backdrops){
        const text = foreground.alpha === 1 ? foreground : over(foreground, backdrop);
        const r = ratio(text, backdrop);
        const ok = r >= need;
        runs += 1;
        const line = (ok ? 'PASS' : 'FAIL') + ' ' + label + ' ' + selector + ' ' + toHex(text) + ' on ' + toHex(backdrop)
          + ' (' + what + ') = ' + r.toFixed(2) + ':1 (needs ' + need + ':1, ' + place + ')';
        if (ok) console.log(line); else { console.error(line); failing += 1; failures += 1; }
      }
    }
    for (const [selector, property] of IDENTIFYING_BOUNDARIES){
      const entry = rules.get(selector);
      const resolver = selector.startsWith('.ui-flowbar') ? flowbar : base;
      const rule = entry && (entry[property + '-color'] || entry[property]);
      const term = rule && (rule.value.match(/var\([^)]*\)|#[0-9a-f]{3,8}/i) || [])[0];
      const colour = term ? resolver.evaluate(term) : null;
      if (!colour || colour.alpha === 0){
        console.error('FAIL ' + label + ' ' + selector + ' has no resolvable ' + property + ' boundary');
        failures += 1;
        continue;
      }
      for (const [hex, token] of planes){
        const r = ratio(colour, hexColour(hex));
        const ok = r >= 3;
        runs += 1;
        const line = (ok ? 'PASS' : 'FAIL') + ' ' + label + ' ' + selector + ' boundary ' + toHex(colour) + ' on ' + hex + ' (' + token + ') = ' + r.toFixed(2) + ':1 (needs 3:1)';
        if (ok) console.log(line); else { console.error(line); failing += 1; failures += 1; }
      }
    }
  }
  console.log('console: ' + runs + ' runs, ' + failing + ' below threshold, ' + ruledOut + ' painting text in a colour ruled out of text');
}

check('midnight', midnight);
check('daylight', daylight);
checkGate();
checkConsole();

/* the accent picker swaps a [data-accent] family; each one re-tunes the accent
   tokens only, so merge the override over the base theme and re-run the accent
   pair. Blue is the base block, already covered above. */
function parseAccentBlock(dir, family){
  /* The compound selector may head a comma-separated group: the V3 lane wiring
     added a descendant-form twin ([data-dir] [data-accent]) right after each
     same-element compound rule so a route-scoped <main> lane matches too. Allow
     any selector text between the compound selector and the block's opening
     brace so the group form parses the same as the bare compound. */
  const re = new RegExp('\\[data-dir="' + dir + '"\\]\\[data-accent="' + family + '"\\][^{]*\\{([\\s\\S]*?)\\}');
  const m = css.match(re);
  if (!m){
    console.error('FAIL: missing [data-dir="' + dir + '"][data-accent="' + family + '"] block in frontend/src/tokens.css');
    failures += 1;
    return null;
  }
  const tokens = {};
  const tokRe = /--([a-z0-9-]+)\s*:\s*(oklch\([^)]*\))\s*;/g;
  let t;
  while ((t = tokRe.exec(m[1])) !== null) tokens[t[1]] = t[2];
  return tokens;
}
/* Accent families are retired: red is the base accent in both theme blocks,
   already covered by the standard pair checks above. */
for (const family of []){
  for (const [dir, base] of [['midnight', midnight], ['daylight', daylight]]){
    const over = parseAccentBlock(dir, family);
    if (over) check(dir + '/' + family, {...base, ...over});
  }
}

if (failures > 0){
  console.error('\n' + failures + ' contrast pair(s) below WCAG AA. Tune the token in frontend/src/tokens.css, not the gate.');
  process.exit(1);
}
console.log('\nAll token pairs meet WCAG AA in both themes.');
