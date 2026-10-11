import {execFileSync} from 'node:child_process';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {defineConfig, normalizePath} from 'vite';
import react from '@vitejs/plugin-react';

import {splitInstrumentStylesheet} from './src/instrumentStylesheetSplit.mjs';

const require = createRequire(import.meta.url);
const installedStylesheet = require.resolve('ogilvy-intelligence-design-system/style.css');
const installedDist = dirname(installedStylesheet);

/* The critical half is addressed as a file inside the package dist directory
   so the font url() values in it keep resolving against the package's own
   fonts folder; it is never written to disk, load() answers with the split
   text. The deferred half carries no url() and is addressed as the placeholder
   file the route modules import statically, so their chunks list it as a
   dependency and main.jsx's deferred import resolves to the same module. */
const CRITICAL_SOURCE = 'ogilvy-intelligence-design-system/style.critical.css';
const DEFERRED_SOURCE = 'ogilvy-intelligence-design-system/style.deferred.css';
const criticalId = normalizePath(join(installedDist, 'style.critical.css'));
const deferredId = normalizePath(fileURLToPath(new URL('./src/styles/instrument-route-surfaces.css', import.meta.url)));

function instrumentStylesheetSplit(){
  let halves = null;
  const read = () => {
    if (!halves) halves = splitInstrumentStylesheet(readFileSync(installedStylesheet, 'utf8'));
    return halves;
  };
  return {
    name: 'instrument-stylesheet-split',
    enforce: 'pre',
    resolveId(id){
      if (id === CRITICAL_SOURCE) return criticalId;
      if (id === DEFERRED_SOURCE) return deferredId;
      return null;
    },
    load(id){
      if (id === criticalId) return read().critical;
      if (normalizePath(id) === deferredId) return read().deferred;
      return null;
    },
  };
}

/* The release the build carries (11 October 2026): VERSION and CHANGELOG.md
   at the repository root and the 12 character sha f42-api reports as its
   version. The sha comes from git first, so a stale F42_VERSION cannot name
   the build; F42_VERSION is read only where there is no git, as in an image
   build. A missing VERSION leaves the version out, and the menu then shows no
   line; a malformed one stops the build. */
const REPO_ROOT = fileURLToPath(new URL('../../', import.meta.url));
const SHA = /^[0-9a-f]{7,40}$/;

function readOptional(path){
  try {
    return readFileSync(path, 'utf8');
  } catch (error){
    if (error.code === 'ENOENT') return null;
    throw error;
  }
}

function gitHead(root){
  return execFileSync('git', ['rev-parse', 'HEAD'], {cwd: root, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore']});
}

function shortSha(value){
  const sha = String(value || '').trim().toLowerCase();
  return SHA.test(sha) ? sha.slice(0, 12) : null;
}

export function releaseFacts({root = REPO_ROOT, env = process.env, git = gitHead} = {}){
  const versionText = readOptional(join(root, 'VERSION'));
  const version = versionText === null ? null : versionText.trim();
  if (version !== null && !/^\d+\.\d+\.\d+$/.test(version)) throw new Error(`VERSION must be major.minor.patch, found ${JSON.stringify(version)}`);
  let sha = null;
  try {
    sha = shortSha(git(root));
  } catch (error){
    sha = shortSha(env.F42_VERSION);
  }
  return {version, sha, changelog: readOptional(join(root, 'CHANGELOG.md'))};
}

export default defineConfig({
  plugins: [instrumentStylesheetSplit(), react()],
  base: '/',
  define: {__F42_RELEASE__: JSON.stringify(releaseFacts())},
  build: {
    outDir: '../web/dist',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks(id){
          if (id.includes('node_modules/chart.js')) return 'chart';
          if (id.includes('node_modules/three')) return 'globe';
          if (id.includes('node_modules/react-dom') || id.includes('node_modules/react/')) return 'react-vendor';
        }
      }
    }
  }
});
