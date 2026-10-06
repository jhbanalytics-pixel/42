import {spawnSync} from 'node:child_process';

const mutations = [
  ['route-effect', 'direct-link', 'direct route hydration produced'],
  ['timer', 'direct-link', 'direct route hydration produced'],
  ['artifact-callback', 'artifact-loading', 'artifact loading produced'],
  ['persona-resolver', 'direct-link', 'direct route hydration produced'],
  ['market-callback', 'route-changes', 'route controls produced'],
  ['automatic-retry', 'capacity-retry', 'capacity state changed the original request count'],
];

if (process.env.RESEARCH_MUTATION_RUNNER_SELF_TEST === 'infrastructure-failure') {
  mutations.splice(0, mutations.length, [
    'route-effect',
    'missing-infrastructure-case',
    'direct route hydration produced',
  ]);
}

let failed = 0;
for (const [mutation, testCase, expectedFailure] of mutations) {
  const result = spawnSync(process.execPath, ['scripts/test_explicit_research_generation.mjs'], {
    cwd: new URL('..', import.meta.url),
    encoding: 'utf8',
    env: {
      ...process.env,
      RESEARCH_GENERATION_MUTATION: mutation,
      RESEARCH_GENERATION_CASE: testCase,
    },
  });
  const output = (result.stdout + result.stderr).trim();
  if (result.error) {
    console.error('FAIL mutation infrastructure: ' + mutation + ': ' + result.error.message);
    failed++;
  } else if (output.includes('MUTATION_NOT_APPLIED')) {
    console.error('FAIL mutation was not applied: ' + mutation);
    failed++;
  } else if (result.status === 0) {
    console.error('FAIL mutation survived: ' + mutation);
    failed++;
  } else if (!output.includes('FAIL ' + testCase + ': ' + expectedFailure)) {
    console.error('FAIL mutation infrastructure: ' + mutation);
    if (output) console.error(output);
    failed++;
  } else {
    console.log('PASS mutation killed: ' + mutation);
  }
}

if (failed) {
  console.error('FAIL: ' + failed + ' research generation mutation(s) survived');
  process.exit(1);
}
console.log('PASS: ' + mutations.length + ' research generation mutations killed');
