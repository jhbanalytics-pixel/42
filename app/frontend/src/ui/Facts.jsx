import {Fragment} from 'react';

/* A line of short facts joined by middots. Each fact stands whole, with its
   middot at its end, so the line breaks between facts: "7 200 views" never
   splits across rows and a new row never starts with a middot. The text reads
   exactly as parts.join(' · '). */
export function Facts({parts}){
  const list = (parts || []).filter((part) => part !== null && part !== undefined && part !== false && part !== '');
  return list.map((part, i) => (
    <Fragment key={i}>
      {i > 0 && ' '}
      <span className="fact-unit">{part}{i < list.length - 1 ? ' ·' : ''}</span>
    </Fragment>
  ));
}
