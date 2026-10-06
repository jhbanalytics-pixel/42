import {ENDPOINT, QUESTION_STATES, ROUTE} from './_question.js';

export default {
  capability_id: 'questions_followups',
  route: ROUTE,
  fixtures: (state) => ({[ENDPOINT]: QUESTION_STATES[state]()}),
  gaps: {
    stale: 'general_question_detail_v1 carries as_of and a closed window but no freshness state or age, so the route cannot declare a stored answer stale.',
  },
};
