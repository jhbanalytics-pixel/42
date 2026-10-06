/* PULSE ui · MarketChip: market code with a full-name tooltip. No flag glyph:
   on Windows without flag emoji fonts, the regional-indicator fallback paints
   the code twice ("ZA ZA"). Same fix as ConsoleWorkbench's RegionSwitch
   (adad6b2). Consolidates the REGION_NAME lookup that lives in model.js. */
import {REGION_NAME} from '../model.js';

export function MarketChip({market}){
  const code = String(market || '').toUpperCase();
  const name = REGION_NAME[code] || code;
  return (
    <span className="ui-market-chip" title={name}>
      <span className="ui-market-code">{code}</span>
    </span>
  );
}
