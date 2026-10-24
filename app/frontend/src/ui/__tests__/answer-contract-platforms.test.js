// The app's answer contract takes exactly the platforms the answer schema does: collect stores Bluesky and LinkedIn
// posts (morning row 16w), and an answer citing one must render, not fail the contract. Pinterest is not in the
// schema (pinned to Release A), so its row 16w search stays off and the contract refuses it.
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import completeRecord from './fixtures/ask42_complete.json';

const {validateAnswer} = await import('../../answerContract.js');
const schema = JSON.parse(readFileSync(new URL('../../../../../core/eval/answer.schema.json', import.meta.url), 'utf8'));
const platforms = schema.$defs.evidence.properties.platform.enum;

function citing(platform){
  const answer = structuredClone(completeRecord.answer);
  answer.evidence[0].platform = platform;
  return validateAnswer(answer).ok;
}

test('an answer citing a Bluesky or LinkedIn post passes the app contract', () => {
  expect(citing('bluesky')).toBe(true);
  expect(citing('linkedin')).toBe(true);
});

test('every platform in the answer schema passes the app contract, and no other', () => {
  for (const platform of platforms) expect(citing(platform)).toBe(true);
  expect(platforms).not.toContain('pinterest');
  expect(citing('pinterest')).toBe(false);
  expect(citing('myspace')).toBe(false);
});
