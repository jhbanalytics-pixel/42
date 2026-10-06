/* PULSE ui · StatTile: a single labelled metric. The value uses the --data-mono
   instrument face (added to tokens.css) so numbers read as an instrument
   against the editorial serif. tone tints the value: up / down / accent /
   neutral (default). */
import {CountUp} from '../parts.jsx';

const TONE_COLOR = {
  up: 'var(--up)',
  down: 'var(--down)',
  accent: 'var(--accent)',
  neutral: 'var(--ink)',
};

export function StatTile({value, label, tone, countUp}){
  const color = TONE_COLOR[tone] || TONE_COLOR.neutral;
  return (
    <div className="ui-stat-tile">
      <div className="ui-stat-value" style={{color}}>
        {countUp && typeof value === 'number' ? <CountUp to={value} comma /> : value}
      </div>
      {label && <div className="ui-stat-label">{label}</div>}
    </div>
  );
}
