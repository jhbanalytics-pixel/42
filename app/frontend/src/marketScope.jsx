/* The header's market picker scopes the lists of saved work too (Albert,
   4 October 2026: with Nigeria picked, pages still listed South Africa).
   All markets, or no pick, keeps every row; a row with no recorded market
   stays in every market. */
import React from 'react';

const NAMES = {ZA: 'South Africa', NG: 'Nigeria', KE: 'Kenya'};

export const pickedMarket = (region) => (NAMES[region] ? region : '');

export const inMarket = (market, picked) => !picked || !NAMES[market] || market === picked;

/* One quiet line under a list: how many rows the picked market hides. */
export function MarketScopeNote({picked, hidden, what}){
  if (!picked || !hidden) return null;
  return (
    <p className="market-scope-note" data-market-scope={picked}>
      {'Showing ' + NAMES[picked] + ' only. ' + hidden + ' ' + what + ' in other markets; pick All markets at the top to see them.'}
    </p>
  );
}

/* Visual QA, 5 October 2026: with Nigeria picked at the top, the Alerts and
   Schedules forms still started on South Africa. A form's market starts on
   the header's and follows it, until the reader picks one in the form. A
   market a link named is already a pick. */
export function useFollowedMarket(follow, named = ''){
  const [market, setMarket] = React.useState(named || follow);
  const chosen = React.useRef(Boolean(named));
  React.useEffect(() => { if (!chosen.current) setMarket(follow); }, [follow]);
  const choose = (next) => { chosen.current = true; setMarket(next); };
  return [market, choose];
}
