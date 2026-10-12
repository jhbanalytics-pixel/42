"""The ranked question rule, as the ruling states it, in its 34 examples.

A question is ranked when it asks which stories, conversations or topics lead (a rank cue and a story cue) about a market,
place, platform, day, span, feed or demographic group, and names no one subject. It is not ranked when it has a yes or no
answer, asks for an own list (creators, sounds, hashtags, or brands ranked), asks why, forecasts, or asks for a brief as
the deliverable. A preposition (about, on, around, for, with, re, re:, regarding, concerning) followed by a thing names a
subject; followed by a market, place, platform, day, span, the week, the timeline, social or a demographic group it does
not, and a bare the never decides on its own. The writer's rule and the reader's gate ask this one function."""
import pytest

from core.agent import asks, writer

RANKED = [
    "What are people in South Africa posting about most this week?",
    "What are the top stories in South Africa this week?",
    "What are the biggest conversations in Kenya this week?",
    "What dominated conversation in Mzansi this week?",
    "What are the top stories for the week in South Africa?",
    "What is trending on TikTok in South Africa this week?",
    "What are the top stories on 10 October?",
    "What are the top stories in Cape Town this week?",
    "What are the biggest stories on the timeline this week?",
    "What is trending with Gen Z in Nigeria?",
    "What are the biggest conversations with young people in South Africa this week?",
    "What are the most concerning stories in South Africa this week?",
    "What are the top 5 stories in South Africa this week? Keep it brief.",
    ("What's South African social actually talking about this week, outside the election and sport? Give me the three "
     "conversations with the most momentum right now and show me the posts that prove it."),
]
NOT_RANKED = [
    "What are the biggest conversations on the petrol price this week?",
    "What are the top stories around the budget speech?",
    "What are the top conversations for the fuel levy this week?",
    "What are the biggest stories with the taxi strike in Soweto?",
    "What are the top stories on the national shutdown this week?",
    "What are the biggest stories this week in Mzansi re the petrol price?",
    "What are the biggest stories this week in Mzansi re: the petrol price?",
    "What are people posting most about Checkers?",
    "What is the top story about Bafana Bafana?",
    "Is amapiano trending in South Africa?",
    "Is #gqomchallenge trending in Soweto this week?",
    "Which creators are biggest in Kenya this week?",
    "What are the top hashtags in Nigeria this week?",
    "What are people saying about the top 3 banks?",
    "Why are the top stories in South Africa trending?",
    "Which of the top stories will keep growing?",
    "Which topics are most likely to blow up in South Africa this week?",
    "A launch brief for a student data bundle in Nigeria: lean into the top trending conversations on campus.",
    "How should Castle Lager play the trending Springbok conversation?",
    "How is South Africa doing on social this week?",
    "Compare football banter in South Africa, Nigeria and Kenya.",
]


@pytest.mark.parametrize("question", RANKED)
def test_a_question_that_asks_which_stories_lead_is_ranked(question):
    assert asks.asks_ranked(question) is True
    assert writer.asks_ranked(question) is True  # the writer's rule asks the same function


@pytest.mark.parametrize("question", NOT_RANKED)
def test_a_question_that_names_a_subject_or_asks_something_else_is_not_ranked(question):
    assert asks.asks_ranked(question) is False
    assert writer.asks_ranked(question) is False


def test_the_examples_are_the_ruling_s_34_and_the_re_with_a_colon():
    assert len(RANKED) + len(NOT_RANKED) == 35


# One question for each word the not-a-subject list allows after a preposition, and each place the rule names, so a word
# dropped from the list or from the capitals is a failing example.
ALSO_RANKED = [
    "What are the biggest stories on the feed this week?",
    "What are the top stories for the weekend in South Africa?",
    "What are the top stories with women in South Africa this week?",
    "What are the top stories with men in Nigeria this week?",
    "What are the top stories with youth in Kenya this week?",
    "What are the top stories with students in Nigeria this week?",
    "What are the biggest conversations with people in South Africa this week?",
    "What are the top stories in Ibadan this week?",
    "What are the top stories in Port Harcourt this week?",
]
ALSO_NOT_RANKED = [
    "What are the biggest stories this week in Mzansi regarding the petrol price?",
    "What are the biggest stories this week in Mzansi re. the petrol price?",
    "What are the biggest stories on women's football this week?",
    "What are the top stories on students' fees this week?",
    "What are the top stories on the men's national team this week?",
    "What are the top stories on the week's petrol price hike?",
    "Brief on the top stories in South Africa this week, please.",
    "Give me a quick social media brief of the top stories in South Africa this week.",
]


@pytest.mark.parametrize("question", ALSO_RANKED)
def test_each_word_the_rule_lets_follow_a_preposition_keeps_the_question_ranked(question):
    assert asks.asks_ranked(question) is True


@pytest.mark.parametrize("question", ALSO_NOT_RANKED)
def test_a_possessive_or_a_brief_or_a_regarding_names_a_subject_or_asks_for_a_brief(question):
    assert asks.asks_ranked(question) is False
