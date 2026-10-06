/* JSX gate: parses every frontend/src module that ships in the Vite build.
   A parse error here is the blank-screen bug; the gate blocks it. The live UI
   is the Vite build from frontend/src (main.py serves web/dist first), not the
   legacy web/index.html, so the gate must read the real source or it waves
   through broken JSX (the bug this replaces). */
import {readFileSync, readdirSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {createRequire} from 'node:module';

const require = createRequire(import.meta.url);

let Babel;
try {
  Babel = require('@babel/standalone');
} catch (e){
  console.error('FAIL: @babel/standalone is not installed. Run: bun install @babel/standalone (or npm install @babel/standalone)');
  process.exit(1);
}

const srcDir = fileURLToPath(new URL('../frontend/src', import.meta.url));
const files = readdirSync(srcDir).filter((f) => f.endsWith('.jsx') || f.endsWith('.js'));
if (!files.length){
  console.error('FAIL: no frontend/src JSX modules found (expected the Vite source)');
  process.exit(1);
}

let totalLines = 0;
for (const f of files){
  const code = readFileSync(`${srcDir}/${f}`, 'utf8');
  try {
    const out = Babel.transform(code, {presets: ['react'], sourceType: 'module', filename: f});
    if (!out || !out.code){
      console.error('FAIL: Babel returned no output for ' + f);
      process.exit(1);
    }
    totalLines += code.split('\n').length;
  } catch (e){
    console.error('FAIL: JSX parse error in frontend/src/' + f);
    console.error(e.message);
    process.exit(1);
  }
}
console.log('PASS: ' + files.length + ' frontend/src modules compile clean (' + totalLines + ' source lines)');
