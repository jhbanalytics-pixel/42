"""The shared corpus for the rule 1 parity test: phrases the age and demographic rule (RULES.md rule 1, TRUST.md K6)
must flag, and look-alikes it must let through. One phrase, one label, no detector consulted to choose it.

A phrase is in here only when the rule's own words settle it. Generation labels, life stages named as an audience,
age figures and inferred demographics are flagged. Calendar moments (Youth Day, Children's Day), numbers that are
not ages (dates, durations, counts) and words that only contain an age word (kidney, boomerang) are let through.
"""

FLAG = {
    "generation label": [
        "Gen Z", "gen z", "GenZ", "Generation Z", "Gen Alpha", "Generation Alpha", "Gen X", "Gen Y", "zoomers",
        "a zoomer", "millennials", "millennial", "boomers", "baby boomers", "born-frees", "digital natives", "iGen",
        "#genz", "#genzrevolution", "GenZProtests",
    ],
    "life stage named as an audience": [
        "teens", "teenagers", "a teenager", "teenage fans", "the youth", "youths", "young people", "young men",
        "younger women", "youngsters", "kids", "school kids", "children", "a child", "toddlers", "babies",
        "infants", "pensioners", "elderly", "seniors", "senior citizens", "old people", "middle-aged",
        "adolescents", "matriculants", "school-going learners", "school leavers", "first-time voters",
        "grandmothers", "young adults", "retirees", "minors",
    ],
    "named by work item N15 as passed by the age scan": ["baby", "old man", "old woman", "elders"],
    "age figure": [
        "18-24", "aged 25", "ages 18 to 24", "25-34 year olds", "35-year-old", "in their twenties",
        "early twenties", "twenty-somethings", "thirty-somethings", "under 30s", "over 40s", "the 25-34s",
        "users aged 30+", "born in 1998", "grew up in the 90s", "90s kids",
    ],
    "inferred demographic": [
        "gender split", "income brackets", "middle class", "working-class", "demographics", "life stage",
        "skews female", "skews young", "mostly women", "predominantly male", "likely female", "LSM 5",
        "high income", "low-income households",
    ],
}

PASS = {
    "calendar moment or name": ["Youth Day", "Youth Month", "Children's Day", "Wizkid", "Boomerang", "boomerang"],
    "a word that only contains an age word": ["kidney", "kidnap", "kidding", "canteen", "fifteen", "Gen Za"],
    "an old or young thing, not a person": ["a young brand", "a young startup", "the old town", "an old song",
                                            "older version", "senior manager", "learners licence"],
    "a number that is not an age": ["over 20 plates", "under 5 million", "10-15s clips", "2024-2026",
                                    "18 October 2026", "40 000 views", "50+ videos", "June 18-24",
                                    "25-30 minutes"],
}


def phrases():
    """[(phrase, must_flag, group)] with each phrase once."""
    out, seen = [], set()
    for must_flag, table in ((True, FLAG), (False, PASS)):
        for group, items in table.items():
            for phrase in items:
                assert phrase not in seen, phrase
                seen.add(phrase)
                out.append((phrase, must_flag, group))
    return out
