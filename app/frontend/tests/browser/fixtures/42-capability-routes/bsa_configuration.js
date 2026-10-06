export default {
  capability_id: 'bsa_configuration',
  route: '#/console?work=brief&persona_id=bsa_research&markets=za',
  fixtures: () => ({
    '/api/research/personas': {__status: 501, body: {detail: {code: 'research_personas_unimplemented', message: 'No configured research persona is served.'}}},
    '/api/research/availability': {brief_writing: {available: true, code: null, message: null}, behaviour_scan: {available: true, code: null, message: null}},
  }),
  gaps: {},
};
