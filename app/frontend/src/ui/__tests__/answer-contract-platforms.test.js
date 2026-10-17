// The app's answer contract takes every platform the answer schema does: collect stores Pinterest and Bluesky posts
// (morning row 16w), and an answer citing one must render, not fail the contract.
import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import completeRecord from './fixtures/ask42_complete.json';

const {validateAnswer} = await import('../../answerContract.js');
const schema = JSON.parse(readFileSync(new URL('../../../../../core/eval/answer.schema.json', import.meta.url), 'utf8'));
const platforms = schema.$defs.evidence.properties.platform.enum;

test('an answer citing a Pinterest or Bluesky post passes the app contract', () => {
  for (const platform of ['pinterest', 'bluesky']) {
    const answer = structuredClone(completeRecord.answer);
    answer.evidence[0].platform = platform;
    expect(validateAnswer(answer).ok).toBe(true);
  }
});

test('every platform in the answer schema passes the app contract, and no other', () => {
  expect(platforms).toContain('pinterest');
  for (const platform of platforms) {
    const answer = structuredClone(completeRecord.answer);
    answer.evidence[0].platform = platform;
    expect(validateAnswer(answer).ok).toBe(true);
  }
  const answer = structuredClone(completeRecord.answer);
  answer.evidence[0].platform = 'myspace';
  expect(validateAnswer(answer).ok).toBe(false);
});
