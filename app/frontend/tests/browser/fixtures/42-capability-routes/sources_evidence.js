import {ENDPOINT, QUESTION_STATES, ROUTE} from './_question.js';

export default {
  capability_id: 'sources_evidence',
  route: ROUTE,
  fixtures: (state) => ({[ENDPOINT]: QUESTION_STATES[state]()}),
  gaps: {},
};
