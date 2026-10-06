/* Copy audit over every user facing string under src.

   Three faults are read from the source files rather than from a render,
   because a render only shows the branch its fixture took. A retired
   collection vendor may be named only as history: the word historical or
   retired has to sit on the same line. Generation language is an audience
   assumption the engine never measured, so it fails wherever it appears
   outside the immutable historical keys named in the allowlist. A raw engine
   identifier (a snake_case token, a contract version, a reason code) is not
   copy: it fails in a JSX text run, in a copy attribute, in a sentence
   literal, and when a state field is rendered bare as the only child of an
   element that carries no label of its own. A code element after a message
   and a dd after its dt are labelled by construction and pass.

   A false positive is answered by an allowlist entry that names the file
   and the reason, never by loosening a pattern. */
import {expect, test} from 'bun:test';
import {readFileSync, readdirSync, statSync} from 'node:fs';
import {join, relative} from 'node:path';
import {fileURLToPath} from 'node:url';

const sourceRoot = fileURLToPath(new URL('../../', import.meta.url));

function sources(directory){
  const files = [];
  for (const entry of readdirSync(directory)){
    if (entry === '__tests__' || entry === 'node_modules') continue;
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) files.push(...sources(path));
    else if (/\.(?:jsx|js)$/.test(entry) && !/\.test\./.test(entry)) files.push(path);
  }
  return files;
}

const FILES = sources(sourceRoot).map((path) => ({
  file: relative(sourceRoot, path).replace(/\\/g, '/'),
  text: readFileSync(path, 'utf8'),
}));

const RETIRED_VENDOR = /\b(?:ensemble ?data|brand ?24)\b/i;
const HISTORICAL = /\b(?:historical|retired)\b/i;
const GENERATION = /\bgen ?z\b|genz|\byouth\b|millennial|boomer/i;
const SNAKE = /\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b/;
const VERSION = /\b[a-z][a-z0-9_]*_v\d+\b/;
const STATE_FIELDS = 'state|evidence_state|kill_state|attribution_state|funding_math_status|observed_state|quality_state|route_status|mode|status';
const BARE_STATE = new RegExp(`<([a-zA-Z][\\w-]*)(?:\\s[^<>]*)?>\\{\\s*(?:[A-Za-z_$][\\w$]*\\.)*(?:${STATE_FIELDS})\\s*\\}</`, 'g');
const LABELLED_BY_CONSTRUCTION = new Set(['code', 'dd']);
const COPY_ATTRIBUTE = /\b(?:aria-label|label|title|placeholder|alt|caption|heading|kicker)=(["'])((?:(?!\1).)*)\1/g;
const SENTENCE = /(['"`])([A-Z][^'"`\n]{12,})\1/g;
const TEXT_RUN = />([^<>{}]+)</g;
const CODE_SHAPE = /[()=;]|=>|\?|&&|\|\|/;

/* Each entry names one file and the reason its match is not a fault. */
const ALLOWLIST = [
  {file: 'model.js', match: /genz_lifestyle|genz_sheng|'genz_'/, reason: 'immutable historical taxonomy keys and their prefix; the labels Lifestyle and Sheng carry no audience claim'},
  {file: 'researchLib.jsx', match: /voice_of_genz/, reason: 'legacy persona migration branch that renames the stored key on read'},
];

function allowed(file, line){
  return ALLOWLIST.some((entry) => entry.file === file && entry.match.test(line));
}

function lineAt(text, index){
  let line = 1;
  for (let at = 0; at < index; at += 1) if (text.charCodeAt(at) === 10) line += 1;
  return line;
}

function identifier(copy){
  const token = copy.match(VERSION) || copy.match(SNAKE);
  return token ? token[0] : null;
}

function audit(){
  const findings = [];
  for (const {file, text} of FILES){
    const lines = text.split('\n');
    lines.forEach((line, index) => {
      const at = `${file}:${index + 1}`;
      if (RETIRED_VENDOR.test(line) && !HISTORICAL.test(line)) findings.push(`${at}: retired vendor named as live: ${line.trim().slice(0, 120)}`);
      if (GENERATION.test(line) && !allowed(file, line)) findings.push(`${at}: generation language: ${line.trim().slice(0, 120)}`);
    });
    for (const match of text.matchAll(TEXT_RUN)){
      const run = match[1];
      if (!/[A-Za-z]/.test(run) || CODE_SHAPE.test(run)) continue;
      const token = identifier(run);
      if (token) findings.push(`${file}:${lineAt(text, match.index)}: raw identifier in text: ${token}`);
    }
    for (const match of text.matchAll(COPY_ATTRIBUTE)){
      const token = identifier(match[2]);
      if (token) findings.push(`${file}:${lineAt(text, match.index)}: raw identifier in copy attribute: ${token}`);
    }
    for (const match of text.matchAll(SENTENCE)){
      /* A candidate that opens an interpolation it never closes is a nested
         template split on an inner backtick, not a sentence. */
      if ((match[2].match(/\$\{/g) || []).length !== (match[2].match(/\}/g) || []).length) continue;
      const token = identifier(match[2].replace(/\$\{[^}]*\}/g, ''));
      if (token) findings.push(`${file}:${lineAt(text, match.index)}: raw identifier in sentence: ${token}`);
    }
    for (const match of text.matchAll(BARE_STATE)){
      if (LABELLED_BY_CONSTRUCTION.has(match[1])) continue;
      findings.push(`${file}:${lineAt(text, match.index)}: state rendered bare in ${match[1]}: ${match[0].replace(/\s+/g, ' ').slice(0, 100)}`);
    }
  }
  return findings;
}

test('the audit reads the whole source tree', () => {
  expect(FILES.length).toBeGreaterThan(40);
  expect(FILES.some(({file}) => file === 'App.jsx')).toBe(true);
  expect(FILES.some(({file}) => file.includes('__tests__'))).toBe(false);
});

test('every allowlist entry still matches the line it exempts', () => {
  for (const entry of ALLOWLIST){
    const source = FILES.find(({file}) => file === entry.file);
    expect(source, entry.file).toBeDefined();
    expect(entry.match.test(source.text), `${entry.file}: ${entry.reason}`).toBe(true);
  }
});

test('no user facing string names a retired vendor as live, assumes a generation, or shows a raw engine identifier', () => {
  expect(audit()).toEqual([]);
});
