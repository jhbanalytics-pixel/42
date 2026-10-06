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

export default defineConfig({
  plugins: [instrumentStylesheetSplit(), react()],
  base: '/',
  build: {
    outDir: '../web/dist',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks(id){
          if (id.includes('node_modules/chart.js')) return 'chart';
          if (id.includes('node_modules/react-dom') || id.includes('node_modules/react/')) return 'react-vendor';
        }
      }
    }
  }
});
