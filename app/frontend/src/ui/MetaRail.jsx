/* PULSE ui · MetaRail: the dossier hero's right rail, label/value pairs in
   --data-mono. Purely presentational: a dl (dt/dd) so screen readers get real
   pairs rather than a div soup. tone applies color but never alone; a glyph
   (triangle) carries the same signal so the tone reads without color vision.
   Absorbs the topic hero's freed width; reused by any hero that needs a
   compact stat column. Hides a null/undefined/empty item and hides the whole
   rail when nothing resolves (DATA_SPEC). */

const TONE_COLOR = {
  up: 'var(--up)',
  down: 'var(--down)',
  accent: 'var(--accent)',
  neutral: 'var(--ink)',
};
const TONE_GLYPH = {
  up: '▲',
  down: '▼',
};

export function MetaRail({items}){
  const list = Array.isArray(items)
    ? items.filter((it) => it && it.value !== null && it.value !== undefined && it.value !== '')
    : [];
  if (list.length === 0) return null;

  return (
    <dl className="ui-meta-rail">
      {list.map((it, i) => {
        const mono = it.mono !== false;
        const color = it.tone ? (TONE_COLOR[it.tone] || TONE_COLOR.neutral) : undefined;
        const glyph = it.tone ? TONE_GLYPH[it.tone] : null;
        return (
          <div className="ui-meta-rail-item" key={it.label || i}>
            <dt className="ui-meta-rail-label">{it.label}</dt>
            <dd
              className={'ui-meta-rail-value' + (mono ? ' ui-meta-rail-value--mono tnum' : '')}
              data-tone={it.tone || undefined}
              style={color ? {color} : undefined}
            >
              {glyph && <span aria-hidden="true">{glyph} </span>}
              {it.value}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}
