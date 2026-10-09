"""The brief row's rule_version names every rule change that alters what a brief row says for the same inputs, one
token per decision, appended in tree order (ruling 17). This branch lands the B7 decade rule, so it appends its own
token. Fixed string, not read from the code."""


def test_the_briefs_rule_version_names_the_b7_decade_rule():
    from core.brief import job
    assert job.RULE_VERSION == "warmup-1+k6-b7-decade"
