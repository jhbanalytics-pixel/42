/* The one question each menu page answers (UX pass, 3 October 2026), shown
   on All pages. Each page's own line under its title opens with the same
   question, written in that page's file; keep the two in step. */
export const PAGE_QUESTIONS = Object.freeze({
  '#/pulse': 'What is moving in this market today, and why?',
  '#/ask': 'What do you want to know about a trend or a market?',
  '#/explore': 'What else is 42 following, beyond today’s picks?',
  '#/alerts': 'What are you watching, and has any of it moved?',
  '#/investigations': 'What is 42 researching for you in depth?',
  '#/dossiers': 'Which answers have you checked and can share?',
  '#/history': 'What did we see before, and what did we ask?',
  '#/compare': 'How do trends stack up against each other?',
  '#/lexicon': 'Which words and hashtags are people using?',
  '#/communities': 'Which creators move together?',
  '#/seedpath': 'Where did 42 first record a word, and which words appear with it?',
  '#/seeds': 'What will 42 search for next, and what did its last searches find?',
  '#/coverage': 'What did 42 collect, and what is missing?',
  '#/fieldwork': 'Which sources delivered on the collection day?',
  '#/method': 'How does 42 decide what counts?',
  '#/schedules': 'Which questions does 42 ask again for you?',
  '#/skins': 'What does a client’s own view of 42 show?',
  '#/people/hidden': 'Who is kept off creator pages, and why?',
  '#/map': 'Where is everything in 42?',
});

export function pageQuestion(href){
  return PAGE_QUESTIONS[href] || '';
}
