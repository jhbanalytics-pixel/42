import {expect, test} from 'bun:test';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, mkdirSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';

/* Release D, premium UI. The design token gate (scripts/check_design_tokens.mjs) is held at zero FAIL lines on the
   real tree and runs in CI. The fixture tests below pin what the gate judges, so a gate that stopped looking, or
   that took a value from the sheet it is judging, fails here instead of going quiet. */

const gate = fileURLToPath(new URL('../../../scripts/check_design_tokens.mjs', import.meta.url));

function run(args){
  const result = spawnSync('node', [gate, ...args], {encoding: 'utf8'});
  expect(result.error).toBeUndefined();
  const lines = `${result.stdout}\n${result.stderr}`.split('\n');
  return {status: result.status, fails: lines.filter((line) => line.startsWith('FAIL')), lines};
}

function judge(files){
  const root = mkdtempSync(join(tmpdir(), 'gate-fixture-'));
  try {
    for (const [name, text] of Object.entries(files)){
      const path = join(root, name);
      mkdirSync(dirname(path), {recursive: true});
      writeFileSync(path, text);
    }
    return run(['--root', root]);
  } finally {
    rmSync(root, {recursive: true, force: true});
  }
}

test('the design token gate has zero FAIL lines on the real tree', () => {
  const real = run([]);
  expect(real.fails).toEqual([]);
  expect(real.status).toBe(0);
});

test('a shadow named through a token is judged by what the token declares', () => {
  const drop = judge({'a.css': ':root { --lift: 0 4px 8px rgba(0, 0, 0, .2); }\n.a { box-shadow: var(--lift); }\n'});
  expect(drop.status).toBe(1);
  expect(drop.fails.length).toBe(1);
  expect(drop.fails[0]).toContain('a.css');
  expect(drop.fails[0]).toContain('.a');

  const inset = judge({'a.css': ':root { --grip: inset 0 2px 0 red, inset 0 -2px 0 red; }\n.a { box-shadow: var(--grip); }\n'});
  expect(inset.fails).toEqual([]);
  expect(inset.status).toBe(0);
});

test('a token declared in two places must be inset in both', () => {
  const split = judge({
    'a.css': ':root { --grip: inset 0 2px 0 red; }\n.a { box-shadow: var(--grip); }\n',
    'b.css': '.b { --grip: 0 6px 12px black; }\n',
  });
  expect(split.status).toBe(1);
  expect(split.fails.length).toBe(1);
});

test('a shadow token declared nowhere is a FAIL, not a pass', () => {
  const missing = judge({'a.css': '.a { box-shadow: var(--nowhere); }\n'});
  expect(missing.status).toBe(1);
  expect(missing.fails.length).toBe(1);
});

test('a token that names another token is followed to its declaration', () => {
  const chained = judge({'a.css': ':root { --inner: 0 3px 3px gray; --outer: var(--inner); }\n.a { box-shadow: var(--outer); }\n'});
  expect(chained.status).toBe(1);
  const flat = judge({'a.css': ':root { --inner: inset 0 3px 0 gray; --outer: var(--inner); }\n.a { box-shadow: var(--outer); }\n'});
  expect(flat.fails).toEqual([]);
});

test('a literal drop shadow still fails', () => {
  const literal = judge({'a.css': '.a { box-shadow: 0 2px 4px black; }\n'});
  expect(literal.status).toBe(1);
  expect(literal.fails.length).toBe(1);
});

test('the three shape tokens pass at their pinned values and nowhere else', () => {
  const shapes = ':root { --shape-control: 6px; --shape-default: 8px; --shape-card: 10px; }\n';
  const ok = judge({'a.css': `${shapes}button { border-radius: var(--shape-control); }\n.card { border-radius: var(--shape-card); }\n.pad { border-radius: var(--shape-default); }\n`});
  expect(ok.fails).toEqual([]);
  expect(ok.status).toBe(0);

  const drifted = judge({'a.css': ':root { --shape-control: 12px; }\nbutton { border-radius: var(--shape-control); }\n'});
  expect(drifted.status).toBe(1);
  expect(drifted.fails.length).toBe(1);

  const overridden = judge({'a.css': `${shapes}button { border-radius: var(--shape-control); }\n.x { --shape-control: 20px; }\n`});
  expect(overridden.status).toBe(1);

  const flat = judge({'a.css': `${shapes}.lex { --shape-control: 0; }\nbutton { border-radius: var(--shape-control); }\n`});
  expect(flat.fails).toEqual([]);

  const indirect = judge({'a.css': ':root { --shape-control: var(--round); --round: 6px; }\nbutton { border-radius: var(--shape-control); }\n'});
  expect(indirect.status).toBe(1);
});

test('a token outside the three shape tokens does not buy a radius', () => {
  const other = judge({'a.css': ':root { --pill: 6px; }\nbutton { border-radius: var(--pill); }\n'});
  expect(other.status).toBe(1);
  const undeclared = judge({'a.css': 'button { border-radius: var(--shape-control); }\n'});
  expect(undeclared.status).toBe(1);
  const literal = judge({'a.css': '.card { border-radius: 6px; }\n'});
  expect(literal.status).toBe(1);
});

test('a raw font size in a sheet without a baseline entry still fails', () => {
  const raw = judge({'fresh.css': '.a { font-size: 11px; }\n'});
  expect(raw.status).toBe(1);
  expect(raw.fails.length).toBe(1);
  expect(raw.fails[0]).toContain('fresh.css');
});
