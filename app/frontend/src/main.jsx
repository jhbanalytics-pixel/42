import {createRoot} from 'react-dom/client';
import 'ogilvy-intelligence-design-system/style.critical.css';
import './styles/boot.css';
import App from './App.jsx';
import archivoUrl from './assets/fonts/archivo-variable.woff2?url';
import martianMonoUrl from './assets/fonts/martian-mono-variable.woff2?url';
import {scheduleDeferredStyles} from './deferredStyles.js';

function StyleLoadError(){
  return (
    <main className="gate-v4 gate-v4--style-error oi-product">
      <section className="gate-v4-style-error" role="alert">
        <p>42 · Ogilvy Intelligence</p>
        <h1>42 could not finish opening.</h1>
        <p>Reload the page. If this repeats, the interface assets are unavailable.</p>
      </section>
    </main>
  );
}

const CRITICAL_FONT_FACES = [
  '400 16px "Newsreader"',
  '400 16px "Recursive Sans"',
  '400 16px "Recursive Mono"',
];

/* Night Desk face (styles/nightdesk.css). It is registered before the first
   render so the interface opens in its own face instead of swapping to it
   when the deferred sheets land. */
function bootstrapNightDeskFace(){
  if (typeof FontFace !== 'function' || !document.fonts || typeof document.fonts.add !== 'function') return Promise.resolve();
  const face = new FontFace('Archivo 42', 'url("' + archivoUrl + '") format("woff2")', {weight: '100 900', stretch: '62% 125%', display: 'swap'});
  /* Control room's label face (styles/controlroom.css), registered the same way. */
  const mono = new FontFace('Martian Mono 42', 'url("' + martianMonoUrl + '") format("woff2")', {weight: '300 600', stretch: '75% 112.5%', display: 'swap'});
  document.fonts.add(face);
  document.fonts.add(mono);
  return Promise.all([face.load(), mono.load()]).catch(() => undefined);
}

function bootstrapCriticalFonts(){
  if (!document.fonts || typeof document.fonts.load !== 'function') return Promise.resolve();
  return Promise.all([...CRITICAL_FONT_FACES.map((face) => document.fonts.load(face)), bootstrapNightDeskFace()]).catch(() => undefined);
}

const root = createRoot(document.getElementById('root'));
document.documentElement.setAttribute('data-product-styles', 'loading');
bootstrapCriticalFonts().then(() => {
  root.render(<App/>);
  scheduleDeferredStyles({
    frame: typeof requestAnimationFrame === 'function' ? requestAnimationFrame : undefined,
    criticalStylesReady: () => getComputedStyle(document.documentElement)
      .getPropertyValue('--font-serif')
      .trim().length > 0,
    load: () => Promise.all([
      import('ogilvy-intelligence-design-system/style.deferred.css'),
      import('./app.css'),
      import('./styles/empty-states.css'),
      import('./styles/dossier.css'),
      import('./styles/controls42.css'),
      import('./styles/nightdesk.css'),
      import('./styles/controlroom.css'),
    ]),
    onReady: () => document.documentElement.setAttribute('data-product-styles', 'ready'),
    onFailure: () => {
      document.documentElement.setAttribute('data-product-styles', 'failed');
      root.render(<StyleLoadError />);
    },
  });
});
