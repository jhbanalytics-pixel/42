import {afterEach, expect, test} from 'bun:test';
import {parse} from '@babel/parser';
import {mkdtempSync, mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync} from 'node:fs';
import {dirname, extname, join, relative, resolve} from 'node:path';
import {tmpdir} from 'node:os';
import {fileURLToPath} from 'node:url';

const testPath = fileURLToPath(import.meta.url);
const testDir = dirname(testPath);
const srcDir = resolve(testDir, '../..');
const packageName = 'ogilvy-intelligence-design-system';
const packageExports = new Set(Object.keys(await import(packageName)));
const scratchDirs = [];

afterEach(() => {
  while (scratchDirs.length) rmSync(scratchDirs.pop(), {recursive: true, force: true});
});

function sourceFiles(dir){
  return readdirSync(dir, {withFileTypes: true}).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    return ['.js', '.jsx', '.mjs'].includes(extname(entry.name)) ? [path] : [];
  });
}

function parseModule(source){
  return parse(source, {sourceType: 'module', plugins: ['jsx']});
}

function nodeName(node){
  if (!node) return null;
  if (node.type === 'Identifier' || node.type === 'StringLiteral') return node.name ?? node.value;
  return null;
}

function declarationNames(declaration){
  if (!declaration) return [];
  if ((declaration.type === 'FunctionDeclaration' || declaration.type === 'ClassDeclaration') && declaration.id) {
    return [declaration.id.name];
  }
  if (declaration.type === 'VariableDeclaration') {
    return declaration.declarations.flatMap(({id}) => id.type === 'Identifier' ? [id.name] : []);
  }
  return [];
}

function packageOwnedExports(ast){
  const exports = [];
  for (const statement of ast.program.body) {
    if (statement.type === 'ExportDefaultDeclaration') {
      const name = nodeName(statement.declaration) ?? nodeName(statement.declaration?.id);
      if (name && packageExports.has(name)) exports.push({name, publicName: 'default'});
      continue;
    }
    if (statement.type === 'ExportAllDeclaration') {
      if (statement.source.value === packageName) {
        for (const name of packageExports) exports.push({name, publicName: name});
      }
      continue;
    }
    if (statement.type !== 'ExportNamedDeclaration') continue;
    if (statement.source) continue;
    for (const name of declarationNames(statement.declaration)) {
      if (packageExports.has(name)) exports.push({name, publicName: name});
    }
    for (const specifier of statement.specifiers) {
      const localName = nodeName(specifier.local);
      const exportedName = nodeName(specifier.exported);
      const name = packageExports.has(exportedName) ? exportedName
        : packageExports.has(localName) ? localName
          : null;
      if (name) exports.push({name, publicName: exportedName});
    }
  }
  return exports;
}

function resolveModule(fromPath, specifier, availableFiles){
  if (!specifier.startsWith('.')) return null;
  const base = resolve(dirname(fromPath), specifier);
  const candidates = extname(base)
    ? [base]
    : [base, `${base}.js`, `${base}.jsx`, `${base}.mjs`, join(base, 'index.js'), join(base, 'index.jsx')];
  return candidates.find((candidate) => availableFiles.has(candidate)) ?? null;
}

function walk(node, visit){
  if (!node || typeof node !== 'object') return;
  visit(node);
  for (const [key, value] of Object.entries(node)) {
    if (key === 'loc' || key === 'start' || key === 'end') continue;
    if (Array.isArray(value)) value.forEach((child) => walk(child, visit));
    else if (value && typeof value.type === 'string') walk(value, visit);
  }
}

function moduleReferences(path, ast, availableFiles){
  const references = [];
  for (const statement of ast.program.body) {
    if (statement.type === 'ImportDeclaration') {
      const target = resolveModule(path, statement.source.value, availableFiles);
      if (!target) continue;
      if (statement.specifiers.length === 0) references.push({target, name: '*'});
      for (const specifier of statement.specifiers) {
        references.push({
          target,
          name: specifier.type === 'ImportDefaultSpecifier' ? 'default'
            : specifier.type === 'ImportNamespaceSpecifier' ? '*'
              : nodeName(specifier.imported),
        });
      }
    }
    if ((statement.type === 'ExportNamedDeclaration' || statement.type === 'ExportAllDeclaration') && statement.source) {
      const target = resolveModule(path, statement.source.value, availableFiles);
      if (!target) continue;
      if (statement.type === 'ExportAllDeclaration') references.push({target, name: '*'});
      else for (const specifier of statement.specifiers) references.push({target, name: nodeName(specifier.local)});
    }
  }
  walk(ast.program, (node) => {
    const source = node.type === 'ImportExpression' ? node.source
      : node.type === 'CallExpression' && node.callee.type === 'Import' ? node.arguments[0]
        : null;
    if (source?.type !== 'StringLiteral') return;
    const target = resolveModule(path, source.value, availableFiles);
    if (target) references.push({target, name: '*'});
  });
  return references;
}

function unownedLocalPackageExports(root, excludedPaths = []){
  const excluded = new Set(excludedPaths.map((path) => resolve(path)));
  const files = sourceFiles(root).filter((path) => !excluded.has(resolve(path)));
  const availableFiles = new Set(files);
  const modules = files.map((path) => ({path, ast: parseModule(readFileSync(path, 'utf8'))}));
  const references = modules.flatMap(({path, ast}) => moduleReferences(path, ast, availableFiles));
  const implementations = new Map();
  for (const {path, ast} of modules) {
    for (const {name, publicName} of packageOwnedExports(ast)) {
      const key = `${path}\0${name}`;
      if (!implementations.has(key)) implementations.set(key, {name, path, publicNames: new Set()});
      implementations.get(key).publicNames.add(publicName);
    }
  }
  return [...implementations.values()]
    .filter(({path, publicNames}) => !references.some((reference) =>
      reference.target === path && (reference.name === '*' || publicNames.has(reference.name))))
    .map(({name, path}) => `${name}:${relative(root, path).replaceAll('\\', '/')}`)
    .sort();
}

function fixture(files){
  const root = mkdtempSync(join(tmpdir(), 'ownership-gate-'));
  scratchDirs.push(root);
  for (const [path, source] of Object.entries(files)) {
    const target = join(root, path);
    mkdirSync(dirname(target), {recursive: true});
    writeFileSync(target, source);
  }
  return root;
}

// Parsing every source file once takes about 3 s alone and over 5 s on a loaded machine, so this scan gets its own timeout.
test('Listening Post contains no unowned package component implementation', () => {
  expect(unownedLocalPackageExports(srcDir, [testPath])).toEqual([]);
  const source = readFileSync(join(srcDir, 'main.jsx'), 'utf8');
  const ast = parseModule(source);
  expect(ast.program.body.some((node) => node.type === 'ImportDeclaration'
    && node.source.value === `${packageName}/style.critical.css`)).toBe(true);
  expect(source).toContain(`import('${packageName}/style.deferred.css')`);
}, 20000);

test.each([
  ['nested copied component', {'nested/Banner.jsx': 'export function Banner(){ return null; }'}, 'Banner:nested/Banner.jsx'],
  ['default export', {'Banner.jsx': 'export default function Banner(){ return null; }'}, 'Banner:Banner.jsx'],
  ['later named export', {'HeldNotice.jsx': 'function HeldNotice(){ return null; }\nexport {HeldNotice};'}, 'HeldNotice:HeldNotice.jsx'],
  ['alias export', {'MeasurementBands.jsx': 'function LocalBands(){ return null; }\nexport {LocalBands as MeasurementBands};'}, 'MeasurementBands:MeasurementBands.jsx'],
  ['comment-only pseudo consumer', {
    'Toast.jsx': 'export function Toast(){ return null; }',
    'consumer.test.jsx': "// import {Toast} from './Toast.jsx';",
  }, 'Toast:Toast.jsx'],
])('%s cannot hide an unowned package implementation', (_label, files, expected) => {
  expect(unownedLocalPackageExports(fixture(files))).toEqual([expected]);
});

test('a package re-export remains package authority rather than a copied implementation', () => {
  const root = fixture({'index.jsx': `export {SourceInventoryRow} from '${packageName}';`});
  expect(unownedLocalPackageExports(root)).toEqual([]);
});

test('real static dynamic and re-export consumers own the imported module', () => {
  const root = fixture({
    'Banner.jsx': 'export function Banner(){ return null; }',
    'HeldNotice.jsx': 'export default function HeldNotice(){ return null; }',
    'Toast.jsx': 'export function Toast(){ return null; }',
    'SourceInventoryRow.jsx': 'export function SourceInventoryRow(){ return null; }',
    'static.jsx': "import {Banner} from './Banner.jsx';",
    'side-effect.jsx': "import './SourceInventoryRow.jsx';",
    'dynamic.jsx': "export const load = () => import('./HeldNotice.jsx');",
    'barrel.jsx': "export {Toast} from './Toast.jsx';",
  });
  expect(unownedLocalPackageExports(root)).toEqual([]);
});
