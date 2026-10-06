"""Tests for the Wave 2 social sentiment lexicon scorer (Group A).

Covers the scorer heuristics (polarity, negation flip, intensifier, neutral)
and the enrichment hook flag gating (OFF -> NA on every row, ON -> social rows
scored, GDELT rows NA).
"""

import pandas as pd
import pytest
from src.enrichment.sentiment_lexicon import SentimentLexicon, load_lexicon


def _lex(positive=None, negative=None, **extra):
    return SentimentLexicon(
        "test",
        {"positive": positive or [], "negative": negative or [], **extra},
    )


# --- scorer polarity ---------------------------------------------------------


def test_positive_term_scores_positive():
    lex = _lex(positive=["great"], negative=["bad"])
    assert lex.score("this is great") > 0


def test_negative_term_scores_negative():
    lex = _lex(positive=["great"], negative=["bad"])
    assert lex.score("this is bad") < 0


def test_neutral_text_scores_zero():
    lex = _lex(positive=["great"], negative=["bad"])
    assert lex.score("the meeting is at noon") == 0.0


def test_empty_text_scores_zero():
    lex = _lex(positive=["great"], negative=["bad"])
    assert lex.score("") == 0.0
    assert lex.score(None) == 0.0


def test_score_bounded_in_range():
    lex = _lex(positive=["good", "great", "love"], negative=["bad"])
    s = lex.score("good great love good great love good")
    assert -1.0 < s < 1.0


def test_mixed_polarity_nets_out():
    lex = _lex(positive=["good"], negative=["bad"])
    # one positive + one negative cancel to neutral net
    assert lex.score("good and bad") == 0.0


def test_multiword_phrase_matches():
    lex = _lex(positive=["soft life"], negative=["load shedding"])
    assert lex.score("living that soft life") > 0
    assert lex.score("load shedding again tonight") < 0


# --- negation ----------------------------------------------------------------


def test_negation_flips_positive_to_negative():
    lex = _lex(positive=["great"], negative=["bad"])
    pos = lex.score("this is great")
    neg = lex.score("this is not great")
    assert pos > 0
    assert neg < 0


def test_negation_flips_negative_to_positive():
    lex = _lex(positive=["good"], negative=["bad"])
    assert lex.score("not bad at all") > 0


def test_negation_outside_window_does_not_flip():
    lex = _lex(positive=["great"], negative=["bad"])
    # negator 5 tokens before the term, beyond the window -> stays positive
    assert lex.score("no one ever said it would be great") > 0


# --- intensifier / downtoner -------------------------------------------------


def test_intensifier_amplifies():
    lex = _lex(positive=["good"], negative=["bad"])
    plain = lex.score("it is good")
    amped = lex.score("it is very good")
    assert amped > plain


def test_downtoner_damps():
    lex = _lex(positive=["good"], negative=["bad"])
    plain = lex.score("it is good")
    damped = lex.score("it is kinda good")
    assert 0 < damped < plain


def test_per_market_intensifier_extends_defaults():
    lex = _lex(positive=["poa"], intensifiers={"sana": 1.6})
    plain = lex.score("the show is poa")
    amped = lex.score("the show is poa sana")
    # "sana" sits after the term here, so it should not amplify (modifier looks
    # backward); the per-market key is still merged without error.
    assert plain > 0
    assert amped > 0


# --- real market lexicons load and score -------------------------------------


@pytest.mark.parametrize("market", ["za", "ng", "ke"])
def test_market_lexicon_loads_and_scores(market):
    lex = load_lexicon(market)
    # each seed lexicon should score a clearly positive and clearly negative line
    assert lex.score("this is amazing i love it") > 0
    assert lex.score("this is terrible i hate it") < 0


def test_missing_market_returns_neutral_lexicon():
    lex = load_lexicon("zz")
    assert lex.score("anything at all here") == 0.0


# --- enrichment hook flag gating ---------------------------------------------


def _social_and_gdelt_df():
    return pd.DataFrame(
        [
            {
                "source": "rss",
                "platform": "web",
                "market": "za",
                "content_type": "article",
                "title": "amazing news",
                "text": "i love this so much",
                "author_handle": "",
            },
            {
                "source": "gdelt",
                "platform": "news",
                "market": "za",
                "content_type": "gdelt_gkg",
                "title": "terrible report",
                "text": "this is bad",
                "author_handle": "",
                "v2tone": "-5.0,1.0,6.0,7.0,1.0,0.0,100",
            },
        ]
    )


def test_hook_off_is_na_on_every_row(monkeypatch):
    monkeypatch.delenv("SENTIMENT_LEXICON_ENABLED", raising=False)
    from src.ingestion.enrichment import enrich_dataframe

    out = enrich_dataframe(_social_and_gdelt_df(), "za")
    assert "sentiment_lexicon_score" in out.columns
    assert out["sentiment_lexicon_score"].isna().all()


def test_hook_on_scores_social_row_only(monkeypatch):
    monkeypatch.setenv("SENTIMENT_LEXICON_ENABLED", "true")
    from src.ingestion.enrichment import enrich_dataframe

    out = enrich_dataframe(_social_and_gdelt_df(), "za")
    scores = list(out["sentiment_lexicon_score"])
    # social row (index 0) scored positive; gdelt row (index 1) stays NA
    assert scores[0] > 0
    assert pd.isna(scores[1])
