/* The topic globe's canvas on the Communities page. The words around it
   (the picker and the chosen community's panel) live in people42.jsx and
   work without it; this file only draws. The 3D code loads on first use,
   so a page with no community, or a browser with no WebGL, never fetches it. */
import React, {useEffect, useRef, useState} from 'react';

const UNAVAILABLE = 'This browser cannot draw the globe. Every community and its topics are listed here and below.';
const LABELLED = 6;

function webgl(){
  try {
    const canvas = document.createElement('canvas');
    return Boolean(canvas.getContext && (canvas.getContext('webgl2') || canvas.getContext('webgl')));
  } catch {
    return false;
  }
}

const reduced = () => {
  try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch { return false; }
};

const shortLabel = (label) => String(label || '').split(', ')[0];

export function TopicGlobe({model, market, selected, onPick, picked}){
  const hostRef = useRef(null);
  const labelsRef = useRef(null);
  const sceneRef = useRef(null);
  const pickRef = useRef(onPick);
  pickRef.current = onPick;
  const [state, setState] = useState(() => (webgl() ? 'loading' : 'unavailable'));

  useEffect(() => {
    if (state === 'unavailable') return undefined;
    let gone = false;
    import('./topicGlobeScene.js')
      .then(({createGlobeScene}) => {
        if (gone || !hostRef.current) return;
        sceneRef.current = createGlobeScene(hostRef.current, model, {
          market, reducedMotion: reduced(), labels: labelsRef.current, onPick: (id) => pickRef.current && pickRef.current(id),
        });
        sceneRef.current.select(selected);
        setState('ready');
      })
      .catch(() => { if (!gone) setState('unavailable'); });
    return () => {
      gone = true;
      if (sceneRef.current) sceneRef.current.dispose();
      sceneRef.current = null;
    };
  }, [model, market]);

  useEffect(() => {
    if (sceneRef.current) sceneRef.current.select(selected, {turnTo: picked});
  }, [selected, picked]);

  if (state === 'unavailable') {
    return <p className="cg42-unavailable" data-globe-state="unavailable">{UNAVAILABLE}</p>;
  }
  const mine = new Set(model.links.filter((l) => l.community === selected).map((l) => l.topic));
  const named = model.communities.filter((c, i) => i < LABELLED || c.id === selected)
    .sort((a, b) => (b.id === selected) - (a.id === selected));
  return (
    <div className="cg42-view" data-state={state}>
      <div className="cg42-stage" ref={hostRef} />
      <div className="cg42-labels" ref={labelsRef} aria-hidden="true">
        {named.map((c) => (
          <span key={c.id} className={'cg42-label cg42-label-community' + (c.id === selected ? ' cg42-label-on' : '')}
            data-pos={c.pos.join(',')}>{shortLabel(c.label)}</span>
        ))}
        {model.topics.filter((t) => mine.has(t.id)).map((t) => (
          <span key={t.id} className="cg42-label cg42-label-topic" data-pos={t.pos.join(',')}>{t.title}</span>
        ))}
      </div>
      {state === 'loading' && <p className="cg42-loading" role="status">Drawing the globe</p>}
    </div>
  );
}
