/* LoopRail: the Discover, Seed path, Seeds line. Shared by the loop pages so
   moving between them reads as one investigation. Shell consistency, 2 October
   2026: it was three boxed steps joined by arrows, a wizard drawn around three
   links. It is now one plain line of three links in their order: the page the
   reader is on is set at 600 and carries aria-current="step", and the step
   words stay for a screen reader, which hears "Step 2 of 3, Seed path". */
import './styles/loop.css';

const STAGES = [
  {key: 'discover', href: '#/explore', step: 'Step 1 of 3', name: 'Discover'},
  {key: 'explorer', href: '#/seedpath', step: 'Step 2 of 3', name: 'Seed path'},
  {key: 'seeds', href: '#/seeds', step: 'Step 3 of 3', name: 'Seeds'},
];

export function LoopRail({active}){
  return (
    <nav className="loop-rail" aria-label="From a trend to a seed">
      {STAGES.map((s) => (
        <a
          key={s.key}
          className={'loop-stage' + (active === s.key ? ' is-active' : '')}
          href={s.href}
          aria-current={active === s.key ? 'step' : undefined}
        >
          <span className="loop-stage-eyebrow">{s.step}</span>{' '}
          <span className="loop-stage-name">{s.name}</span>
        </a>
      ))}
    </nav>
  );
}

export default LoopRail;
