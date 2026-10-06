/* Round 10 of the 42 in Black programme measured the whole shell for the first
   time, and the widened matrix reached nine consumer routes the redesign had
   never visited: map, method, network, listen, lexicon, board, browse, seeds
   and seedpath. This file holds the consumer half of that run, one test per
   class of defect, each written so it fails when its fix is taken away.

   Everything here reads source. The sheets are read as text and resolved
   through the package palette the way the cascade resolves them, because
   tokens.css no longer loads and every colour on these routes arrives from the
   package bridge. */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import postcss from 'postcss';

const read = (path) => readFileSync(fileURLToPath(new URL(`../../${path}`, import.meta.url)), 'utf8');
const packageCss = readFileSync(
  fileURLToPath(new URL('../../../node_modules/ogilvy-intelligence-design-system/dist/style.css', import.meta.url)),
  'utf8',
);

/* Every consumer sheet a route the shell reaches can load. app.css carries the
   legacy routes, the four route sheets carry the surfaces named above, and
   ui.css carries the shared components they all mount. */
const LEGACY_SHEETS = [
  'app.css',
  'ui/ui.css',
  'ogilvy-intelligence.css',
  'styles/boardviews.css',
  'styles/console.css',
  'styles/dossier.css',
  'styles/empty-states.css',
  'styles/fieldwork.css',
  'styles/graphs.css',
  'styles/lexlisten.css',
  'styles/loop.css',
  'styles/topic.css',
  'styles/workspaces.css',
];

/* ===== the palette, resolved the way the page resolves it ===== */

function packageScopes(theme){
  const merge = (selector) => {
    const merged = {};
    let from = 0;
    for (;;){
      const start = packageCss.indexOf(`${selector}{`, from);
      if (start < 0) break;
      const open = packageCss.indexOf('{', start);
      const close = packageCss.indexOf('}', open);
      const declaration = /--([a-z0-9-]+)\s*:\s*([^;}]+)/g;
      let found;
      while ((found = declaration.exec(packageCss.slice(open + 1, close))) !== null){
        merged[`--${found[1]}`] = found[2].trim();
      }
      from = close + 1;
    }
    return merged;
  };
  return [merge(`[data-dir=${theme}]`), merge('[data-dir]'), merge('.oi-product'), merge(':root')];
}

/* The consumer's own lane block outranks the package bridge, so it is read
   first. Reading it from app.css rather than restating it here means the test
   measures the shipped values. */
function consumerLanes(theme){
  const lane = {};
  postcss.parse(read('app.css')).walkRules((rule) => {
    if (rule.parent.type !== 'root') return;
    if (!rule.selectors.some((selector) => selector.trim() === `:root[data-dir="${theme}"]`)) return;
    rule.walkDecls((declaration) => { lane[declaration.prop] = declaration.value.trim(); });
  });
  return lane;
}

function hexColour(value){
  let hex = value.slice(1);
  if (hex.length === 3 || hex.length === 4) hex = [...hex].map((c) => c + c).join('');
  const channel = (at) => parseInt(hex.slice(at, at + 2), 16) / 255;
  return {r: channel(0), g: channel(2), b: channel(4), alpha: hex.length === 8 ? channel(6) : 1};
}

function splitTop(value){
  const parts = [];
  let depth = 0;
  let current = '';
  for (const character of value){
    if (character === '(') depth += 1;
    if (character === ')') depth -= 1;
    if (character === ',' && depth === 0){ parts.push(current.trim()); current = ''; } else current += character;
  }
  if (current.trim()) parts.push(current.trim());
  return parts;
}

function makeResolver(chain){
  const lookup = (name) => chain.map((scope) => scope[name]).find((value) => value !== undefined);
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
      const a = part(first);
      const b = part(second);
      if (!a.colour || !b.colour) return null;
      const shareA = a.share !== null ? a.share : (b.share !== null ? 1 - b.share : 0.5);
      const shareB = 1 - shareA;
      const alpha = a.colour.alpha * shareA + b.colour.alpha * shareB;
      if (alpha === 0) return {r: 0, g: 0, b: 0, alpha: 0};
      const channel = (key) => (a.colour[key] * a.colour.alpha * shareA + b.colour[key] * b.colour.alpha * shareB) / alpha;
      return {r: channel('r'), g: channel('g'), b: channel('b'), alpha};
    }
    return null;
  };
  return {evaluate, lookup};
}

const toHex = (colour) => '#' + ['r', 'g', 'b'].map((key) => Math.round(colour[key] * 255).toString(16).padStart(2, '0')).join('');

function relativeLuminance(colour){
  const channel = (value) => (value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
  return 0.2126 * channel(colour.r) + 0.7152 * channel(colour.g) + 0.0722 * channel(colour.b);
}

function contrast(a, b){
  const first = relativeLuminance(a);
  const second = relativeLuminance(b);
  const light = Math.max(first, second);
  const dark = Math.min(first, second);
  return (light + 0.05) / (dark + 0.05);
}

function resolverFor(theme){
  return makeResolver([consumerLanes(theme), ...packageScopes(theme)]);
}

/* Section 12 names these by their role, and the role is a lane's, not a hex's.
   On ink, faint and the ink rule are boundaries; on paper the rule colour is
   the boundary and the same dark neutral that is faint on ink is simply a dark
   ink, which is why the daylight lane can take it as its dim text and reads
   5.51 or better on every daylight ground. The plane red is a plane on both.
   The run measured each of these on the lane it is a boundary on. */
const RULED_OUT_AS_TEXT = {
  midnight: {
    '#5c554f': 'faint',
    '#2a2521': 'the ink rule',
    '#e41424': 'the plane red',
  },
  daylight: {
    '#70665c': 'the paper rule',
    '#e41424': 'the plane red',
  },
};

function textColourDeclarations(){
  const found = [];
  for (const name of LEGACY_SHEETS){
    postcss.parse(read(name)).walkRules((rule) => {
      rule.walkDecls((declaration) => {
        if (declaration.prop !== 'color') return;
        found.push({
          sheet: name,
          line: declaration.source.start.line,
          selector: rule.selector.replace(/\s+/g, ' '),
          value: declaration.value.trim(),
        });
      });
    });
  }
  return found;
}

test('no consumer rule paints text in a colour section 12 reserves for a plane or a boundary', () => {
  const offenders = [];
  for (const theme of ['midnight', 'daylight']){
    const resolver = resolverFor(theme);
    for (const declaration of textColourDeclarations()){
      const colour = resolver.evaluate(declaration.value);
      if (!colour || colour.alpha !== 1) continue;
      const role = RULED_OUT_AS_TEXT[theme][toHex(colour)];
      if (!role) continue;
      offenders.push(`${theme} ${declaration.sheet}:${declaration.line} ${declaration.selector} -> ${declaration.value} is ${role}`);
    }
  }
  expect(offenders).toEqual([]);
});

test('the red a reader reads as type clears AA on every ground the two lanes paint behind it', () => {
  const grounds = {
    daylight: ['#F5EEE4', '#E8DED2', '#FFFAF2'],
    midnight: ['#070606', '#0B0A09', '#15120F', '#1C1815'],
  };
  const thin = [];
  for (const theme of ['midnight', 'daylight']){
    const resolver = resolverFor(theme);
    const text = resolver.evaluate('var(--accent-text)');
    expect(text, `${theme} carries a red text token`).toBeTruthy();
    for (const ground of grounds[theme]){
      const ratio = contrast(text, hexColour(ground));
      if (ratio < 4.5) thin.push(`${theme} ${toHex(text)} on ${ground} = ${ratio.toFixed(2)}`);
    }
  }
  expect(thin).toEqual([]);
});

test('the one dim text colour each lane carries clears AA on every ground that lane paints behind it', () => {
  const grounds = {
    daylight: ['#F5EEE4', '#E8DED2', '#FFFAF2'],
    midnight: ['#070606', '#0B0A09', '#15120F', '#1C1815'],
  };
  const thin = [];
  for (const theme of ['midnight', 'daylight']){
    const resolver = resolverFor(theme);
    for (const token of ['--muted', '--faint']){
      const text = resolver.evaluate(`var(${token})`);
      expect(text, `${theme} resolves ${token}`).toBeTruthy();
      for (const ground of grounds[theme]){
        const ratio = contrast(text, hexColour(ground));
        if (ratio < 4.5) thin.push(`${theme} ${token} ${toHex(text)} on ${ground} = ${ratio.toFixed(2)}`);
      }
    }
  }
  expect(thin).toEqual([]);
});

/* ===== the width law ===== */

/* A word laid across two lines is the plainest sign of a box the design never
   measured, and a date read as one value across two lines is the same fault
   with a hyphen for an excuse. The rows the run measured are a label, a
   control word and three sentences, and every one of them broke because a
   rule further up asked the browser to break anywhere it liked. */
test('no consumer rule lets a word break anywhere it likes', () => {
  const offenders = [];
  for (const name of LEGACY_SHEETS){
    postcss.parse(read(name)).walkRules((rule) => {
      rule.walkDecls((declaration) => {
        if (declaration.prop === 'overflow-wrap' && /anywhere/.test(declaration.value)){
          offenders.push(`${name}:${declaration.source.start.line} ${rule.selector.replace(/\s+/g, ' ')}`);
        }
        if (declaration.prop === 'word-break' && /break-all/.test(declaration.value)){
          offenders.push(`${name}:${declaration.source.start.line} ${rule.selector.replace(/\s+/g, ' ')}`);
        }
      });
    });
  }
  expect(offenders).toEqual([]);
});

test('the three short labels the run measured broken are held on one line', () => {
  const rules = new Map();
  for (const name of ['ui/ui.css', 'styles/boardviews.css', 'styles/lexlisten.css', 'app.css']){
    postcss.parse(read(name)).walkRules((rule) => {
      if (rule.parent.type !== 'root') return;
      for (const selector of rule.selectors){
        const key = selector.trim();
        if (!rules.has(key)) rules.set(key, {});
        rule.walkDecls((declaration) => { rules.get(key)[declaration.prop] = declaration.value.trim(); });
      }
    });
  }
  for (const selector of ['.ui-momentum-pill', '.brw-search-btn', '.map-node-route']){
    const declarations = rules.get(selector) || {};
    expect(declarations['white-space'], `${selector} holds its label on one line`).toBe('nowrap');
  }
});

/* ===== the pointer ===== */

/* Section 7 gives a control the shell's own hover: the pointer is answered on
   a named duration and a named easing, never on all 0s, and a legacy chip or
   link takes that pattern rather than inventing one. */
test('every legacy class the run found answering nothing now answers the pointer on a section 7 duration', () => {
  const sheets = ['app.css', 'styles/console.css', 'styles/dossier.css', 'styles/lexlisten.css', 'styles/loop.css']
    .map((name) => read(name)).join('\n');
  const hovers = new Map();
  const resting = new Map();
  postcss.parse(sheets).walkRules((rule) => {
    for (const selector of rule.selectors){
      const declarations = {};
      rule.walkDecls((declaration) => { declarations[declaration.prop] = declaration.value.trim(); });
      const hover = selector.match(/^(.*?):(?:hover|focus-visible)$/);
      if (hover){
        const key = hover[1].trim();
        hovers.set(key, {...(hovers.get(key) || {}), ...declarations});
      } else {
        const key = selector.trim();
        resting.set(key, {...(resting.get(key) || {}), ...declarations});
      }
    }
  });
  /* Ask redesign, 23 Sept 2026: the question field is the one framed control
     and takes the form control radius, so its rules name the textarea. */
  for (const selector of ['.evidence-ledger__row a', 'textarea.workbench-ask-field', '.workbench-ask-send']){
    const answer = hovers.get(selector);
    expect(answer, `${selector} answers a pointer`).toBeTruthy();
    const changed = ['color', 'border-color', 'background', 'box-shadow', 'text-decoration-color']
      .filter((property) => answer[property] !== undefined);
    expect(changed.length, `${selector} changes something under the pointer`).toBeGreaterThan(0);
    const transition = (resting.get(selector) || {}).transition || '';
    expect(transition, `${selector} rests on a named duration`).toMatch(/var\(--motion-(control|row|ribbon)\)/);
  }
});

/* ===== the accessibility floor ===== */

test('every control the run measured under the floor now carries the floor', () => {
  const rules = new Map();
  for (const name of ['styles/console.css', 'styles/dossier.css', 'styles/lexlisten.css']){
    postcss.parse(read(name)).walkRules((rule) => {
      if (rule.parent.type !== 'root') return;
      for (const selector of rule.selectors){
        const key = selector.trim();
        if (!rules.has(key)) rules.set(key, {});
        rule.walkDecls((declaration) => { rules.get(key)[declaration.prop] = declaration.value.trim(); });
      }
    });
  }
  const floors = [
    '.evidence-ledger__row a',
    '.workbench-ask-send',
    /* Ask redesign, 23 Sept 2026: the field's rules name the textarea. */
    'textarea.workbench-ask-field',
    '.workbench-ask-suggestion',
    '.listen-search input',
  ];
  for (const selector of floors){
    const declarations = rules.get(selector) || {};
    const declared = declarations['min-height'];
    expect(declared, `${selector} declares a floor`).toBeTruthy();
    const pixels = declared.match(/^(\d+)px$/);
    expect(pixels && Number(pixels[1]) >= 44, `${selector} floor ${declared} is at least 44px`).toBe(true);
  }
});

/* ===== the provenance family ===== */

/* Recursive Mono and Recursive Sans are one variable font under two family
   names, so an element naming the mono family renders the sans cut until the
   MONO axis is asked for. The package asks for it on its own classes; the
   consumer has to ask for it on its own, in one rule rather than route by
   route. */
test('one consumer rule asks for the mono axis, and it reaches the classes and the inline styles alike', () => {
  const axis = /font-variation-settings:\s*"MONO"\s+1/;
  let carrier = null;
  postcss.parse(read('app.css')).walkRules((rule) => {
    if (rule.parent.type !== 'root') return;
    const declarations = rule.nodes.filter((node) => node.type === 'decl');
    if (!declarations.some((node) => axis.test(`${node.prop}: ${node.value}`))) return;
    if (!rule.selector.includes('[style*="var(--mono)"]')) return;
    carrier = rule.selector.replace(/\s+/g, ' ');
  });
  expect(carrier, 'the shared mono rule reaches inline styles').toBeTruthy();
  /* Quiet register, 23 Sept 2026: the legacy chip and the Listen filter chip
     are words a reader reads, so they left this rule for the sans and are held
     to it in the quiet list below. */
  for (const named of [
    '.es-sep', '.loop-rail-arrow', '.map-node-route',
  ]){
    expect(carrier.includes(`${named},`) || carrier.includes(`${named} `) || carrier.endsWith(named), `${carrier} names ${named}`).toBe(true);
  }
  /* Quiet register, 23 Sept 2026: these labels now read in the sans in
     sentence case, so the shared rule must not hand them the mono cut. */
  /* Quiet register, 23 Sept 2026: the eyebrow and the page hero eyebrow are
     labels a reader reads and the package sets .eyebrow in the sans, so they
     left the shared rule too and are held to the sans in this list. */
  for (const quiet of ['.legacy-next-label', '.loop-stage-eyebrow', '.map-node-go', '.es-status', '.legacy-chip', '.listen-filter-chip', '.eyebrow', '.ui-page-hero-eyebrow']){
    expect(carrier.split(',').map((one) => one.trim()), `${carrier} leaves ${quiet} in the sans`).not.toContain(quiet);
  }
});

/* ===== the type scale and the weights ===== */

/* Section 5 gives Newsreader 400 in every row of the type role table and the
   sans 400 to 600, so nothing on these surfaces is set at 700. The eight rows
   the run measured are named one by one rather than swept, because the PULSE
   desk routes the shell no longer offers carry their own weights and this task
   closes the rows the matrix reached, not the routes it did not. */
test('the eight rules the run measured at a weight off the scale are back on it', () => {
  const heavy = /(?:^|\s)(?:700|800|900)(?:\s|$)/;
  const cases = [
    ['styles/workspaces.css', '.evidence-room-grid h3'],
    /* Restated 2 October 2026: the retired operation title is now the roster's source name. */
    ['styles/fieldwork.css', '.fieldwork-source__name'],
    ['app.css', '.map-node-label'],
    ['app.css', '.signal-mark'],
    ['styles/dossier.css', '.dossier-eyebrow'],
  ];
  for (const [sheet, selector] of cases){
    let declarations = null;
    postcss.parse(read(sheet)).walkRules((rule) => {
      if (rule.parent.type !== 'root') return;
      if (!rule.selectors.some((one) => one.trim() === selector)) return;
      declarations = declarations || {};
      rule.walkDecls((declaration) => { declarations[declaration.prop] = declaration.value.trim(); });
    });
    expect(declarations, `${sheet} carries ${selector}`).toBeTruthy();
    const weight = declarations['font-weight'] || (declarations.font || '').match(/^(\d{3})\s/)?.[1] || '';
    expect(heavy.test(` ${weight} `), `${selector} at ${weight || 'inherit'} is off 700`).toBe(false);
  }
  /* The map and network legacy headings render through bare elements, so the
     reset that holds them is read as a rule rather than as a class. */
  const map = read('map.jsx') + read('network.jsx');
  expect(map, 'no route module sets a bare 700 inline').not.toMatch(/fontWeight:\s*'?700/);
});

test('the four rules the run measured off the type scale are back on it', () => {
  const cases = [
    /* Restated 2 October 2026: the retired operation title is now the roster's source name. */
    ['styles/fieldwork.css', '.fieldwork-source__name'],
    ['styles/console.css', '.workbench-ask-send'],
    /* Ask redesign, 23 Sept 2026: the field's rules name the textarea. */
    ['styles/console.css', 'textarea.workbench-ask-field'],
    ['styles/workspaces.css', '.evidence-room-grid h3'],
  ];
  for (const [sheet, selector] of cases){
    let declarations = null;
    postcss.parse(read(sheet)).walkRules((rule) => {
      if (rule.parent.type !== 'root') return;
      if (!rule.selectors.some((one) => one.trim() === selector)) return;
      declarations = {};
      rule.walkDecls((declaration) => { declarations[declaration.prop] = declaration.value.trim(); });
    });
    expect(declarations, `${sheet} carries ${selector}`).toBeTruthy();
    const sizes = [declarations['font-size'], declarations.font].filter(Boolean).join(' ');
    expect(sizes, `${selector} sizes from the scale`).toMatch(/var\(--type-[1-8]\)/);
    expect(sizes, `${selector} names no raw pixel size`).not.toMatch(/\d+(?:\.\d+)?px/);
  }
});

/* ===== the shape law ===== */

/* Section 8 keeps radius at 0, with 2 pixels for a form control and the 999
   pixel pill for a true binary state. A 50 percent radius is neither, and the
   three marks the run measured are a map key dot, a momentum dot and the dock
   foot dot on the Console. */
test('the three marks the run measured at a 50 percent radius are square again', () => {
  const round = /50%/;
  for (const [sheet, selector] of [['app.css', '.map-dot'], ['ui/ui.css', '.ui-momentum-dot']]){
    let radius = null;
    postcss.parse(read(sheet)).walkRules((rule) => {
      if (rule.parent.type !== 'root') return;
      if (!rule.selectors.some((one) => one.trim() === selector)) return;
      rule.walkDecls((declaration) => { if (declaration.prop === 'border-radius') radius = declaration.value.trim(); });
    });
    expect(radius, `${sheet} carries ${selector}`).toBeTruthy();
    expect(round.test(radius), `${selector} radius ${radius}`).toBe(false);
  }
  /* Ask redesign, 23 Sept 2026: the dock foot dots on either side of the
     lens select are gone, so the third mark is held absent rather than
     square. */
  const chat = read('chat.jsx');
  expect(chat, 'the dock foot dot is gone').not.toMatch(/dockFootDotStyle/);
});

/* ===== one red plane to a workspace ===== */

/* The loading drum and the map key paint small marks, and a mark is not a
   decision, so neither may take the plane red. Section 6 gives a workspace one
   plane and these routes were carrying four and six. */
test('the loading drum and the map key paint no plane red', () => {
  const offenders = [];
  const resolver = resolverFor('midnight');
  for (const [name, selectors] of [
    ['styles/empty-states.css', ['.ld-drum .ld-bar', '.ld-orbit .ld-sat', '.ld-rings .ld-core']],
    ['app.css', ['.map-dot']],
  ]){
    postcss.parse(read(name)).walkRules((rule) => {
      if (rule.parent.type !== 'root') return;
      if (!rule.selectors.some((selector) => selectors.includes(selector.trim()))) return;
      rule.walkDecls((declaration) => {
        if (declaration.prop !== 'background' && declaration.prop !== 'background-color') return;
        const colour = resolver.evaluate(declaration.value);
        if (colour && colour.alpha === 1 && toHex(colour) === '#e41424'){
          offenders.push(`${name}:${declaration.source.start.line} ${rule.selector}`);
        }
      });
    });
  }
  expect(offenders).toEqual([]);
});

/* ===== the focus ring ===== */

test('no consumer rule takes a focus ring away without putting one back', () => {
  const offenders = [];
  for (const name of LEGACY_SHEETS){
    postcss.parse(read(name)).walkRules((rule) => {
      if (!/:focus-visible/.test(rule.selector)) return;
      const declarations = {};
      rule.walkDecls((declaration) => { declarations[declaration.prop] = declaration.value.trim(); });
      const outline = declarations.outline;
      if (outline && /^none$/.test(outline) && !declarations['box-shadow']){
        offenders.push(`${name}:${rule.source.start.line} ${rule.selector.replace(/\s+/g, ' ')}`);
      }
    });
  }
  expect(offenders).toEqual([]);
});

/* ===== the accessible name ===== */

test('the browse search field is named by its label rather than by a bare element', () => {
  const source = read('views.jsx');
  const label = source.match(/<label[^>]*className="brw-search-label[^>]*>/);
  expect(label, 'the browse search carries a label element').toBeTruthy();
  expect(label[0], 'the label points at the field').toMatch(/htmlFor=/);
  expect(source, 'the field carries the id the label points at').toMatch(/id="brw-search-input"/);
});

/* ===== one count, one source ===== */

test('the rail does not upgrade admitted candidates without evidence summaries', async () => {
  const {railSummaryForTopics} = await import('../../instrumentAdapters.js');
  const row = (over) => ({
    contract_version: 'desk_dynamic_signal_v2',
    signal_id: 'sig_repair_routine',
    discovery_mode: 'emergent',
    evidence_state: 'unchecked',
    signal_name: 'Repair routine',
    why_now: 'Repair shops are posting their own teardowns.',
    possible_response: 'Brief the repair angle.',
    market: 'ZA',
    ...over,
  });
  const summary = railSummaryForTopics([row()]);
  expect(summary, 'one admitted signal gives the rail a summary').toBeTruthy();
  const total = summary.ready + summary.thin + summary.contradictory;
  expect(total, 'admission without measured evidence is not a ready state').toBe(0);

  const three = railSummaryForTopics([row(), row({signal_id: 'sig_b', evidence_state: 'thin'}), row({signal_id: 'sig_c', evidence_state: 'contradictory'})]);
  expect(three.ready + three.thin + three.contradictory, 'raw state labels cannot substitute for evidence summaries').toBe(0);
});

/* ===== the candidate prompt ===== */

test('the pick prompt is held back where the route offers one candidate', () => {
  const explore = read('explore.jsx');
  expect(explore, 'the route states how many candidates it offered').toMatch(/data-candidate-count=/);
  const sheet = read('ogilvy-intelligence.css');
  expect(sheet, 'a single candidate suppresses the prompt')
    .toMatch(/\[data-candidate-count="1"\][\s\S]{0,120}discover-instrument__next/);
});
