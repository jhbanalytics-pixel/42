import {DESK_STATES} from './_desk.js';

export default {
  capability_id: 'today_briefing',
  route: '#/pulse',
  fixtures: (state) => ({'/api/desk': DESK_STATES[state]()}),
  gaps: {},
};
