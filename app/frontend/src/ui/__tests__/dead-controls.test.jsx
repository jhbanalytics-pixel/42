/* No dead control on a route.

   Every button, link and role button written in the thirteen route
   components is read from source, because a static render drops the handler
   that makes a button live. A button is live when it carries an onClick or
   submits a form the file handles; a link is live when its href is a route
   the router serves, a builder from router.js, or a URL the producer
   supplied; a disabled control is honest when the enabled case has a handler
   and the control names itself. A no-op handler, a bare hash and a href of
   javascript are the shapes a dead control takes, and each fails by name. */
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';

import {resolveHostRoute} from '../../App.jsx';
import {buildScopedReadHash, buildTopicHash, buildWorkspaceHash, parseWorkspaceHash} from '../../router.js';
import {buildWorkbenchHash, parseWorkbenchRoute} from '../../workbenchRoute.js';
import {SERVED_VIEWS} from '../../redesignContract.js';

/* The route components App.jsx mounts. RedThreadBriefing is not here: no route
   renders it (it has its own contract tests in briefing-contract and
   state-matrix), so it is not a route component and its dead Queue guard is
   not a shipped control. listen, topic, board and views are route modules
   App.jsx lazily imports, so they are inventoried like the rest. */
const ROUTE_COMPONENTS = [
  'App.jsx', 'today.jsx', 'explore.jsx', 'compare.jsx', 'creator.jsx', 'network.jsx', 'sourceLab.jsx',
  'fieldwork.jsx', 'chat.jsx', 'evidenceRoom.jsx', 'ResearchDocPanel.jsx', 'historicalWorkspace.jsx',
  'listen.jsx', 'topic.jsx', 'board.jsx', 'views.jsx',
];

/* The design system list covers redesigned views. App.jsx also serves its
   native Ask route directly, so include that one explicit host route here. */
/* Coverage is served by App itself (App.jsx maps the raw coverage view), like
   Ask; Fieldwork's hand-off links to it (added 2 October 2026). */
const NATIVE_HOST_VIEWS = new Set(['ask', 'coverage']);
const VIEWS = new Set([...SERVED_VIEWS, ...NATIVE_HOST_VIEWS]);
const BUILDERS = /\b(?:buildTopicHash|buildWorkspaceHash|buildScopedReadHash|buildWorkbenchHash|buildWorkbenchPath|creatorHref|resolveNextOperation)\b/;
const NO_OP = /onClick[=:]\s*\{?\s*(?:\(\)\s*=>\s*\{\s*\}|undefined|null)\s*\}?/;

function read(file){
  return readFileSync(fileURLToPath(new URL(`../../${file}`, import.meta.url)), 'utf8');
}

function lineAt(text, index){
  let line = 1;
  for (let at = 0; at < index; at += 1) if (text.charCodeAt(at) === 10) line += 1;
  return line;
}

/* The opening tag from its start to the closing angle bracket at brace depth
   zero, so an arrow inside a handler or a nested JSX prop never ends it early. */
function openingTag(text, start){
  let depth = 0, quote = null;
  for (let at = start; at < text.length; at += 1){
    const char = text[at];
    if (quote){ if (char === quote) quote = null; continue; }
    if (char === '"' || char === "'") { quote = char; continue; }
    if (char === '`'){ quote = '`'; continue; }
    if (char === '{') depth += 1;
    else if (char === '}') depth -= 1;
    else if (char === '>' && depth === 0 && text[at - 1] !== '=') return text.slice(start, at + 1);
  }
  return text.slice(start);
}

function controls(file, text){
  const found = [];
  for (const match of text.matchAll(/<(button|a|summary|[A-Za-z][\w.]*)(?=[\s/>])/g)){
    const tag = openingTag(text, match.index);
    const kind = match[1];
    const isRoleButton = /role=["']button["']/.test(tag);
    if (!['button', 'a'].includes(kind) && !isRoleButton) continue;
    found.push({file, line: lineAt(text, match.index), kind: isRoleButton && !['button', 'a'].includes(kind) ? 'role button' : kind, tag: tag.replace(/\s+/g, ' ')});
  }
  /* The state frames and the shell render their controls from action
     objects; the object is the control's source, so it is inventoried too. */
  for (const match of text.matchAll(/\{id: '[\w-]+', label: '[^']*',\s*onClick: [^}]*\}/g)){
    found.push({file, line: lineAt(text, match.index), kind: 'action', tag: match[0].replace(/\s+/g, ' ')});
  }
  return found;
}

function hrefLeadsSomewhere(tag){
  const literal = tag.match(/\bhref=(["'])([^"']*)\1/);
  if (literal){
    const href = literal[2];
    if (!href || href === '#' || /^javascript:/i.test(href)) return `href "${href}" goes nowhere`;
    if (href.startsWith('#/')) return VIEWS.has(href.slice(2).split(/[/?]/)[0]) ? null : `href "${href}" is not a served view`;
    return /^https?:\/\//.test(href) ? null : `href "${href}" is neither a route nor a URL`;
  }
  const dynamic = tag.match(/\bhref=\{([^]*?)\}(?=\s|\/?>)/);
  if (!dynamic) return 'has no href';
  const expression = dynamic[1].trim();
  if (BUILDERS.test(expression)) return null;
  if (/^['"`]#\//.test(expression)){
    const view = expression.slice(3).split(/[/?'"`]/)[0];
    return VIEWS.has(view) ? null : `href ${expression} is not a served view`;
  }
  /* A bare identifier, member read or indexed read is a producer URL or a
     computed route: `row.url`, `r[5]` (the url column of a receipt tuple),
     `first.data.href`. The component that computes it is read below. Anything
     with an operator, a call or a concatenation is not accepted here. */
  return /^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*|\[\s*['"]?[\w$-]+['"]?\s*\])*$/.test(expression)
    ? null : `href ${expression} is not a builder, a route or a supplied URL`;
}

/* The onClick expression carried by a tag, read to the matching brace so a
   nested arrow body is read whole. */
function clickExpression(tag){
  const at = tag.search(/\bonClick=\{/);
  if (at < 0) return null;
  const start = tag.indexOf('{', at) + 1;
  let depth = 1, quote = null;
  for (let i = start; i < tag.length; i += 1){
    const char = tag[i];
    if (quote){ if (char === quote && tag[i - 1] !== '\\') quote = null; continue; }
    if (char === '"' || char === "'" || char === '`'){ quote = char; continue; }
    if (char === '{') depth += 1;
    else if (char === '}'){ depth -= 1; if (depth === 0) return tag.slice(start, i).trim(); }
  }
  return tag.slice(start).trim();
}

/* A handler bound by name is a no-op when the name it reads is declared in the
   file as an empty arrow or empty function: `const noop = () => {}` and
   `onClick={noop}` is as dead as `onClick={() => {}}` written inline. */
function bindsNoOp(text, name){
  if (!/^[A-Za-z_$][\w$]*$/.test(name)) return false;
  const arrow = new RegExp(`\\b(?:const|let|var)\\s+${name}\\s*=\\s*(?:async\\s*)?\\([^)]*\\)\\s*=>\\s*(?:\\{\\s*\\}|undefined|void 0)`);
  const fn = new RegExp(`\\bfunction\\s+${name}\\s*\\([^)]*\\)\\s*\\{\\s*\\}`);
  return arrow.test(text) || fn.test(text);
}

/* A bare `disabled` attribute (never `disabled={...}`) disables the control on
   every render, so a click handler beside it can never fire. */
function permanentlyDisabled(tag){
  return /\bdisabled(?=[\s/>])/.test(tag) && !/\bdisabled\s*=/.test(tag);
}

function fault(control, text){
  const {kind, tag} = control;
  if (NO_OP.test(tag)) return 'has a no-op handler';
  const click = clickExpression(tag);
  if (click && bindsNoOp(text, click)) return 'has a no-op handler';
  if (kind === 'a') return hrefLeadsSomewhere(tag);
  const handled = /\bonClick[=:]\s*\{?/.test(tag) || (/type=["']submit["']/.test(tag) && /\bonSubmit=\{/.test(text));
  if (!handled) return 'has no handler and is not disabled with a reason';
  if (permanentlyDisabled(tag) && /\bonClick[=:]/.test(tag)) return 'is permanently disabled but carries a click handler';
  /* A control that can be disabled has to say what it is: a label, a title,
     a description, or the text a non self-closing tag carries. */
  if (/\bdisabled(?:=|\s|>|\/)/.test(tag) && !/\b(?:aria-label|title|aria-describedby)=/.test(tag) && /\/>$/.test(tag)) return 'is disabled without naming itself';
  return null;
}

/* A control written behind a guard that is false on every render is dead: it
   is never mounted, so no render test can see it. `{false && <button ...>}`
   and `{shipped && <button ...>}` where `const shipped = false` are both this
   shape. The guard is read from the `{<expr> &&` that opens the control. */
const CONSTANT_FALSE_GUARD = /\{\s*(false|[A-Za-z_$][\w$]*)\s*&&\s*(?:\(\s*)?<(?:button|a|summary)\b/g;

function deadGuards(text){
  const findings = [];
  for (const match of text.matchAll(CONSTANT_FALSE_GUARD)){
    const guard = match[1];
    if (guard === 'false' || new RegExp(`\\b(?:const|let|var)\\s+${guard}\\s*=\\s*false\\b`).test(text)){
      findings.push({line: lineAt(text, match.index), guard});
    }
  }
  return findings;
}

const INVENTORY = ROUTE_COMPONENTS.flatMap((file) => controls(file, read(file)).map((control) => ({...control, text: read(file)})));

test('the inventory reads every route component and finds its controls', () => {
  expect(new Set(INVENTORY.map((control) => control.file)).size).toBe(ROUTE_COMPONENTS.length);
  expect(INVENTORY.length).toBeGreaterThan(60);
});

test('every button, link and role button on a route has a live handler, a real destination, or an honest disabled state', () => {
  const findings = [];
  for (const control of INVENTORY){
    const reason = fault(control, control.text);
    if (reason) findings.push(`${control.file}:${control.line} ${control.kind} ${reason}: ${control.tag.slice(0, 110)}`);
  }
  expect(findings).toEqual([]);
});

test('no control on a route is written behind a guard that is false on every render', () => {
  const findings = [];
  for (const file of ROUTE_COMPONENTS){
    for (const guard of deadGuards(read(file))){
      findings.push(`${file}:${guard.line} a control is mounted behind the constant-false guard ${guard.guard}`);
    }
  }
  expect(findings).toEqual([]);
});

test('every static route target written by a route component is a served view', () => {
  const findings = [];
  for (const file of ROUTE_COMPONENTS){
    const text = read(file);
    for (const match of text.matchAll(/\b(?:go\(|route: )(['"])\/([a-z-]+)/g)){
      if (!VIEWS.has(match[2])) findings.push(`${file}:${lineAt(text, match.index)} targets /${match[2]}, which the router does not serve`);
    }
  }
  expect(findings).toEqual([]);
});

test('the route builders the links use round trip through the router', () => {
  expect(parseWorkspaceHash(buildTopicHash('sig_fixture', 'ZA')).view).toBe('topic');
  expect(parseWorkspaceHash(buildScopedReadHash('listen', 'term', 'ZA')).view).toBe('listen');
  expect(parseWorkspaceHash(buildWorkspaceHash({view: 'historical', investigationId: 'inv_fixture', mode: 'analogue'})).investigationId).toBe('inv_fixture');
  expect(parseWorkbenchRoute(buildWorkbenchHash({work: 'ask', requestId: '00000000-0000-4000-8000-000000000001'})).requestId).toBe('00000000-0000-4000-8000-000000000001');
});

test('native Ask is served by the host route resolver', () => {
  expect(resolveHostRoute('ask')).toBe('ask');
  expect(VIEWS.has('ask')).toBe(true);
});

/* A control can also be dead because the name its handler or its enabling
   guard reads is never bound in the file: `typeof setRegion === 'function'`
   is false forever when the component never receives setRegion, so the
   action it guards is written but can never be rendered, and no render test
   sees the branch that is missing. Every name a handler or a guard reads has
   to be bound in its own file, as an import, a declaration, a destructured
   prop or a parameter, or be one of the browser globals below. */
const GLOBALS = new Set([
  'window', 'document', 'globalThis', 'localStorage', 'sessionStorage', 'navigator', 'location', 'history',
  'console', 'fetch', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'requestAnimationFrame',
  'cancelAnimationFrame', 'getComputedStyle', 'matchMedia', 'performance', 'crypto', 'structuredClone',
  'Math', 'JSON', 'Object', 'Array', 'Number', 'String', 'Boolean', 'Date', 'Promise', 'Set', 'Map', 'WeakMap',
  'URL', 'URLSearchParams', 'Error', 'RegExp', 'Symbol', 'Intl', 'Infinity', 'NaN', 'undefined', 'null', 'true', 'false',
  'encodeURIComponent', 'decodeURIComponent', 'parseInt', 'parseFloat', 'isNaN', 'isFinite', 'AbortController',
  'ResizeObserver', 'IntersectionObserver', 'MutationObserver', 'Blob', 'FileReader', 'TextEncoder', 'CSS',
  'scrollTo', 'atob', 'btoa', 'alert', 'confirm', 'prompt', 'queueMicrotask', 'this', 'typeof', 'function',
  'return', 'new', 'void', 'delete', 'in', 'of', 'if', 'else', 'await', 'async', 'const', 'let', 'var', 'try', 'catch',
]);

/* The expressions a control reads: its handlers, and the guard that decides
   whether the control is written at all. */
const HANDLER = /\b(?:onClick|onChange|onSubmit|onInput|onKeyDown|onToggle|onSelect)[=:]\s*(\{)?/g;
const TYPEOF_GUARD = /typeof\s+([A-Za-z_$][\w$]*)\s*===?\s*['"]function['"]/g;

/* The expression that starts at index, read to its matching brace or to the
   end of the property, so a nested handler body is read whole. */
function expressionAt(text, start){
  let depth = 0, quote = null;
  for (let at = start; at < text.length; at += 1){
    const char = text[at];
    if (quote){ if (char === quote && text[at - 1] !== '\\') quote = null; continue; }
    if (char === '"' || char === "'" || char === '`'){ quote = char; continue; }
    if ('{(['.includes(char)) depth += 1;
    else if ('})]'.includes(char)){ depth -= 1; if (depth <= 0) return text.slice(start, at); }
    else if (char === ',' && depth === 0) return text.slice(start, at);
  }
  return text.slice(start, start + 400);
}

function boundNames(text){
  const bound = new Set();
  for (const match of text.matchAll(/\b(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)/g)) bound.add(match[1]);
  /* Imports, destructured declarations, destructured parameters and arrow or
     function parameter lists all bind every plain name they list. */
  for (const match of text.matchAll(/import\s+([^;]+?)\s+from/g)){
    for (const name of match[1].matchAll(/[A-Za-z_$][\w$]*/g)) bound.add(name[0]);
  }
  for (const match of text.matchAll(/\{([^{}]*)\}\s*(?:=[^=>]|\)|=>)/g)){
    for (const name of match[1].matchAll(/([A-Za-z_$][\w$]*)\s*(?:[,}:=]|$)/g)) bound.add(name[1]);
  }
  for (const match of text.matchAll(/\[([^[\]]*)\]\s*=[^=]/g)){
    for (const name of match[1].matchAll(/[A-Za-z_$][\w$]*/g)) bound.add(name[0]);
  }
  for (const match of text.matchAll(/(?:function\s*[\w$]*\s*)?\(([^()]*)\)\s*(?:=>|\{)/g)){
    for (const name of match[1].matchAll(/[A-Za-z_$][\w$]*/g)) bound.add(name[0]);
  }
  for (const match of text.matchAll(/(?:^|[^\w$.])([A-Za-z_$][\w$]*)\s*=>/gm)) bound.add(match[1]);
  for (const match of text.matchAll(/catch\s*\(\s*([A-Za-z_$][\w$]*)/g)) bound.add(match[1]);
  return bound;
}

/* The names an expression reads: plain identifiers, never property names, JSX
   attribute names, object keys or string contents. */
function readNames(expression){
  const stripped = expression
    .replace(/'(?:[^'\\]|\\.)*'/g, "''")
    .replace(/"(?:[^"\\]|\\.)*"/g, '""')
    .replace(/`(?:[^`\\]|\\.)*`/g, '``')
    .replace(/\.\s*[A-Za-z_$][\w$]*/g, '')
    .replace(/([A-Za-z_$][\w$]*)\s*:/g, '');
  return [...stripped.matchAll(/[A-Za-z_$][\w$]*/g)].map((match) => match[0]);
}

/* A control can be dead across the file boundary: a child reads a callback
   prop in a handler, but the parent that mounts it never passes that prop, so
   the handler calls undefined and the control is dead although the child binds
   the name as a parameter. `typeof` and file-local binding checks never catch
   this, because the child file is self-consistent. Every callback prop a route
   child declares without a default and uses has to be passed where App.jsx
   mounts that child. */
const CALLBACK_PROP = /^(?:set[A-Z]|on[A-Z])/;

function mountedComponents(appText){
  const map = new Map();
  for (const match of appText.matchAll(/const\s+([A-Za-z_$][\w$]*)\s*=\s*lazy\(\(\)\s*=>\s*import\('\.\/([\w./]+)'\)\.then\(\(m\)\s*=>\s*\(\{default:\s*m\.([A-Za-z_$][\w$]*)\}\)\)\)/g)){
    map.set(match[1], {file: match[2], exportName: match[3]});
  }
  return map;
}

/* The callback props the mounted export declares without a default, in the
   order they are destructured, provided the component uses each one. */
function requiredCallbackProps(text, exportName){
  const sig = text.match(new RegExp(`(?:export\\s+)?(?:function\\s+${exportName}\\s*\\(|const\\s+${exportName}\\s*=\\s*\\(?)\\s*\\{([^}]*)\\}`));
  if (!sig) return [];
  const props = [];
  for (const part of sig[1].split(',')){
    const name = part.trim().match(/^([A-Za-z_$][\w$]*)/);
    if (!name) continue;
    if (/=/.test(part)) continue; // has a default; the parent may omit it
    if (!CALLBACK_PROP.test(name[1])) continue;
    const used = new RegExp(`[^\\w$.]${name[1]}\\b`).test(text.slice(sig.index + sig[0].length));
    if (used) props.push(name[1]);
  }
  return props;
}

function renderTag(text, componentName){
  const at = text.search(new RegExp(`<${componentName}(?=[\\s/>])`));
  if (at < 0) return null;
  return openingTag(text, at);
}

test('every callback prop a route child uses is passed where App.jsx mounts it', () => {
  const appText = read('App.jsx');
  const mounted = mountedComponents(appText);
  const findings = [];
  for (const [name, {file, exportName}] of mounted){
    const tag = renderTag(appText, name);
    if (!tag) continue; // declared but not mounted in App.jsx
    if (/\{\s*\.\.\./.test(tag)) continue; // spread passes props wholesale
    let childText;
    try { childText = read(file); } catch { continue; }
    for (const prop of requiredCallbackProps(childText, exportName)){
      if (!new RegExp(`\\b${prop}\\s*=`).test(tag)){
        findings.push(`App.jsx mounts ${name} (${file}) without passing ${prop}, which ${exportName} uses in a handler`);
      }
    }
  }
  expect(findings).toEqual([]);
});

test('every handler and every enabling guard reads a name its own file binds', () => {
  const findings = [];
  for (const file of ROUTE_COMPONENTS){
    const text = read(file);
    const bound = boundNames(text);
    const expressions = [];
    for (const match of text.matchAll(HANDLER)) expressions.push({at: match.index, source: expressionAt(text, match.index + match[0].length)});
    for (const match of text.matchAll(TYPEOF_GUARD)) expressions.push({at: match.index, source: match[1]});
    for (const {at, source} of expressions){
      for (const name of readNames(source)){
        if (GLOBALS.has(name) || bound.has(name)) continue;
        findings.push(`${file}:${lineAt(text, at)} reads ${name}, which the file never binds: ${source.replace(/\s+/g, ' ').slice(0, 90)}`);
      }
    }
  }
  expect(findings).toEqual([]);
});
