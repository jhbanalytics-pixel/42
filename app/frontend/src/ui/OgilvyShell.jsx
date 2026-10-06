import React from 'react';

import {PRIMARY_NAV, UTILITY_NAV} from '../redesignContract.js';

const MARKETS = ['ZA', 'NG', 'KE', 'ALL'];

function routeView(path){
  return path.slice(1).split('?')[0];
}

export function OgilvyMasthead({route, setRoute, region, setRegion, freshness}){
  return (
    <header className="oi-shell__masthead">
      <div className="oi-shell__row">
        <button
          type="button"
          className="oi-shell__action oi-shell__brand"
          onClick={() => setRoute('/pulse')}
          aria-label="42 Ogilvy Intelligence, open Briefing"
        >
          <span className="oi-shell__mark">42</span>
          <span className="oi-shell__identity">Ogilvy Intelligence</span>
        </button>

        <nav className="oi-shell__jobs" aria-label="Primary jobs">
          {PRIMARY_NAV.map(({label, path}) => (
            <button
              type="button"
              className="oi-shell__action oi-shell__job"
              key={path}
              aria-current={route === routeView(path) ? 'page' : undefined}
              onClick={() => setRoute(path)}
            >
              {label}
            </button>
          ))}
        </nav>

        <div className="oi-shell__scope" role="group" aria-label="Market scope">
          {freshness && <span className="oi-shell__freshness">{freshness}</span>}
          {MARKETS.map((market) => (
            <button
              type="button"
              className="oi-shell__action oi-shell__market"
              key={market}
              aria-current={region === market ? 'page' : undefined}
              onClick={() => setRegion(market)}
            >
              {market}
            </button>
          ))}
        </div>
      </div>
    </header>
  );
}

export function OgilvyShell({route, setRoute, region, setRegion, freshness, children}){
  return (
    <div className="oi-product oi-shell">
      <OgilvyMasthead
        route={route}
        setRoute={setRoute}
        region={region}
        setRegion={setRegion}
        freshness={freshness}
      />
      <nav className="oi-shell__utility" aria-label="Utility navigation">
        {UTILITY_NAV.map(({label, path}) => <a key={path} href={'#' + path}>{label}</a>)}
      </nav>
      <div className="oi-shell__content">{children}</div>
    </div>
  );
}
