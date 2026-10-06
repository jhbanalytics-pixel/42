import {useState, useEffect, useRef} from 'react';
import './styles/gate.css';

export function PasscodeScreen({onSubmit, failed}){
  const [v, setV] = useState('');
  const inputRef = useRef(null);
  const [accessFirst, setAccessFirst] = useState(false);
  useEffect(() => { if (inputRef.current) inputRef.current.focus(); }, []);
  useEffect(() => {
    const query = inputRef.current?.ownerDocument?.defaultView?.matchMedia('(max-width: 760px)');
    if (!query) return undefined;
    const updateOrder = () => setAccessFirst(query.matches);
    updateOrder();
    query.addEventListener('change', updateOrder);
    return () => query.removeEventListener('change', updateOrder);
  }, []);
  /* Clear the field on a failed attempt so the user can retype without first
     selecting the obscured value. */
  useEffect(() => {
    if (!failed) return;
    setV('');
  }, [failed]);
  const progress = Math.min(v.length / 12, 1);
  const principles = (
    <section key="principles" className="gate-v4-field" aria-label="42 intelligence principles">
      <div className="gate-v4-hero">
        <p className="gate-v4-number" aria-hidden="true">42</p>
        <p className="gate-v4-kicker">Ogilvy Intelligence · Cultural operating picture</p>
        {/* Kept as the page h1 for assistive technology, styled as a calm sans
            deck so the access heading carries the visual weight (NN/g, visual
            hierarchy: weight follows task priority). */}
        <h1 id="gate-title">Open cultural intelligence.</h1>
        <p className="gate-v4-deck">Read what changed, why it matters, and the proof behind the decision across South Africa, Nigeria, and Kenya.</p>
      </div>

      {/* The principles read as a plain definition list with the title as the
          term. Numbered 01, 02, 03 folios are the landing page feature list
          idiom and implied a sequence the principles do not have (NN/g,
          visual hierarchy). */}
      <dl className="gate-v4-system">
        <div><dt>Open discovery</dt><dd>Signals form from observed behaviour, not fixed categories.</dd></div>
        <div><dt>Evidence first</dt><dd>Every recommendation keeps its receipts and limitations.</dd></div>
        <div><dt>Audience neutral</dt><dd>No age lens, ever. Audiences are described by language, place, interest and community.</dd></div>
      </dl>
    </section>
  );
  const access = (
    <aside key="access" className="gate-v4-access" aria-labelledby="gate-access-title">
      <div>
        <p className="gate-v4-folio">Private working environment · 42</p>
        <h2 id="gate-access-title">Enter the desk.</h2>
        <p className="gate-v4-access-copy">Use your Ogilvy team access key. Nothing is submitted until you choose Enter 42.</p>
      </div>

      <form
        className="gate-v4-form"
        style={{'--gate-entry-progress': progress}}
        onSubmit={(e) => { e.preventDefault(); const c = v.trim(); if (c) onSubmit(c); }}
      >
        <label htmlFor="gate-passcode">Access key</label>
        <input
          id="gate-passcode"
          ref={inputRef}
          type="password"
          value={v}
          onChange={(e) => setV(e.target.value)}
          aria-describedby="gate-status"
          autoComplete="current-password"
        />
        <span className="gate-v4-progress" aria-hidden="true" />
        <button type="submit" className="gate-v4-submit">Enter 42 <span aria-hidden="true">→</span></button>
      </form>

      <p
        id="gate-status"
        className={'gate-v4-status' + (failed ? ' is-error' : '')}
        role={failed ? 'alert' : 'status'}
        aria-live={failed ? 'assertive' : 'polite'}
      >
        {failed ? 'Access not recognised. Check the key and try again.' : 'Protected Ogilvy team access.'}
      </p>
    </aside>
  );
  return (
    <main className="gate-v4 oi-product" data-screen-label="Passcode gate" aria-labelledby="gate-title">
      {/* The mast keeps only the lockup. Market codes, an Internal tag and a
          footer list of page names looked like controls but did nothing on
          the gate, a false affordance (Norman, The Design of Everyday Things). */}
      <header className="gate-v4-mast">
        <p><strong>42</strong><span>Ogilvy Intelligence</span></p>
      </header>

      <div className="gate-v4-layout">
        {accessFirst ? [access, principles] : [principles, access]}
      </div>
    </main>
  );
}
