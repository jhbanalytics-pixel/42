/* One name for the count of different accounts, said the same way on Today
   and in Discover: "3 accounts posting, last 3 days". The API words the same
   measure "creators in 3 days". Only the words of a figure change; no value,
   query id or run id does, and nothing measured over one window is ever said
   over another.

   A stored count line is a sentence written by another stage and can carry
   figures this page did not measure. It is never reworded and never joined to
   a measured figure: when a measured accounts figure sits on the card and the
   line carries any number, the line gives way to the measured figures. */
const isFigure = (value) => Boolean(value) && typeof value === 'object' && value.value !== undefined && value.value !== null;
const has = (card, key) => Object.prototype.hasOwnProperty.call(card, key);
const digitsOf = (text) => Number(String(text).replace(/[^\d]/g, ''));

const WINDOW_UNIT = /^creators in (\d+) days?$/i;
const ACCOUNTS_3_DAYS = /^accounts? posting, last 3 days$/;

export function accountsFigure(figure){
  if (!isFigure(figure)) return figure;
  const found = WINDOW_UNIT.exec(String(figure.unit || '').trim());
  if (!found) return figure;
  return {...figure, unit: (Number(figure.value) === 1 ? 'account posting' : 'accounts posting') + ', last ' + found[1] + ' days'};
}

function accountsLine(line, measured){
  if (typeof line !== 'string' || !isFigure(measured)) return line;
  return /\d/.test(line) ? null : line;
}

/* The figure that counts the accounts posting in the last 3 days, after the
   words have been settled: the reach figure or one of the numbers. */
function measuredAccounts(card){
  const figures = [card.reach, ...(Array.isArray(card.numbers) ? card.numbers : [])].map(accountsFigure);
  return figures.find((figure) => isFigure(figure) && ACCOUNTS_3_DAYS.test(String(figure.unit || '').trim()));
}

export function accountsCard(card){
  if (!card || typeof card !== 'object') return card;
  const next = {...card};
  if (has(card, 'reach')) next.reach = accountsFigure(card.reach);
  if (Array.isArray(card.numbers)) next.numbers = card.numbers.map(accountsFigure);
  if (isFigure(card.reach7)) next.reach7 = accountsFigure(card.reach7);
  if (has(card, 'count_line')) next.count_line = accountsLine(card.count_line, measuredAccounts(card));
  return next;
}

/* The accounts the card counts in its 3 day window, or null when the payload
   carries no such figure or its value is not a number. A card is only held
   back for a count that was sent, is a number and is zero. */
export function countedCreators(card){
  if (!card || typeof card !== 'object') return null;
  const found = measuredAccounts(card);
  return found && typeof found.value === 'number' ? found.value : null;
}

/* The same words in a stored sentence: "posted by 31 creators in 3 days" reads
   "posted by 31 accounts in 3 days". The stored text is unchanged. */
const SAID_CREATORS = /(\d[\d   ]*)\s+creators?\s+in\s+(\d+ days?)\b/gi;
export function accountsWords(text){
  if (typeof text !== 'string') return text;
  return text.replace(SAID_CREATORS, (whole, count, days) => count + ' ' + (digitsOf(count) === 1 ? 'account' : 'accounts') + ' in ' + days);
}
