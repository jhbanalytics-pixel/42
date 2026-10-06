"""The age scan (RULES.md rule 1) for words the understand job stores: cluster keywords and labels (cluster.py) and
the entities and sounds enrichment keeps (enrich.py). It imports nothing heavier than core.agent.checks, so
enrichment can scan without loading the clustering stack.

passes_age_scan refuses gen or generation standing as a word of its own, since the vectorizer keeps "gen" when it
drops the "z" of "Gen Z", and any word with an age word run into it (AGE_STEMS), since the vectorizer hands
"#KenyanGenZ" over as "kenyangenz" and "#kidsoftiktok" as one word, and ama before a year (AMA_YEAR) and the
two-word forms UMU_AKA, JONG_MENSE and AWON_OMO on the whole text; each word is also scanned with a "#" in front
against the hashtag patterns, and the whole text against core.agent.checks' AGE_PATTERNS and DEMOGRAPHIC. A value
that is exactly one of the proper names in NAMES ("Wizkid", "Kid Cudi") passes before any of that.
"""
import re
import unicodedata

from core.agent.checks import AGE_PATTERNS, DEMOGRAPHIC

GEN_WORDS = {"gen", "generation"}
# Age words run into a longer token, as the vectorizer hands hashtags over ("#KenyanGenZ" as "kenyangenz"):
# Gen Z or Gen Alpha anywhere; Gen X or Gen Y only at the start, so "progeny" passes; teen, youth and boomer only
# where they cannot be the tail or head of a clean word, so "canteen", "fifteen", "umpteen", "youthday",
# "youthmonth" and "boomerang" pass. A bare "youth" is left to AGE_PATTERNS, which reads the next word ("youth day");
# youth later in a token ("nigerianyouths", "ancyouthleague", "sayouthactivists") is refused unless Youth Day or
# Youth Month follows it, or a verb comes before its "you", so "thankyouthuli", "seeyouthere" and
# "soyouthinkyoucandance" pass.
# Vijana, toddler and children anywhere, but not Children's Day (passes_age_scan first joins "Children's Day",
# "Childrens Day" and "Children Day" into "childrensday"); babies run into another word, but not "furbabies".
# Zillennial, juvenile and amakhehla (isiZulu, old men) anywhere; iGen only as a whole token, so "indigenous",
# "antigen", "digen" and "sigenyi" pass.
# The joined forms of Ask's words anywhere: pikin, wazee, youngster, infant (not "infantry"), tween (not "between"),
# first time voter, young or old people, old heads. Senior and elderly stay out, so "seniorcounsel" passes.
# Kid, kids, kidz, kiddies and kiddos anywhere but after an s, so "skid" and "skids" pass; kid at the start of a token
# only before a consonant, so "kidney", "kidnap", "kidding" and the Swahili "kidogo" pass. Child anywhere but in
# "childish" (Childish Gambino), with children as above. Underage and agemates anywhere, and the words for children
# or young people: watoto, kijana and mtoto (Swahili), abantwana, umntwana and the locatives ebantwaneni and
# kubantwana (isiZulu, all by their stem bantwan), intsha (isiXhosa and isiZulu, not after the English letters of
# "paintshare" or "mintshake"), abasha (isiZulu, not "abashane" or the Ethiopian "habasha"), jongmense, tiener and
# jeug (Afrikaans, not Jeugdag or Jeugmaand, as with Youth Day), matasa (Hausa, not "matasalamat"), yara and yaran
# (Hausa, only as a whole token or with a possessive, so "yarab", "kiyara" and "sayara" pass), omode (Yoruba, at the
# start of a token or after awon, so "commode", "photomode" and the name "Omodele" pass) and umuaka (Igbo).
# Kid, kids and kidz are refused but not after wiz, so "Wizkid", "wizkidayo" and "wizkidstarboy" pass; the English
# spelling "whizkids" is still refused.
# ingane and izingane (isiZulu, child and children) by ngane at the start of a token or after i or e, which also takes
# zingane, the locatives ezinganeni and enganeni, and kwengane and nengane; ngane after any other letter passes, so
# umngane and abangane (isiZulu, friend and friends), "mangane" and "manganese" pass. Not after li or le, so the
# lingana forms alingane, kulingane and ngokulingane (equal) pass, and not before kwan or no, so inganekwane and
# izinganekwane (folktale) and nganeno (this side) pass.
# The ZA slang laaitie or laaitjie (a youngster), madala (an old man), amagogo (grannies) and toppie (an old man),
# toppie not after an s or before r or st, so "stoppie", "toppier" and "toppiest" pass.
# young at the head of a longer token, the joined form of Ask's young pattern ("youngafricans", "youngstunnafans"),
# unless it is young, younger, youngest or youngs alone, or a thing that pattern lets pass follows ("youngbrands").
# umuaka not before ra or mu, so "umuakara" (akara, bean cakes) and "umuakamu" pass. Afrikaans kinders and kindertjies
# (children), kinders not before ur, ley or pel, so "kindersurprise", "Kindersley" and "kinderspel" (child's play)
# pass, and the kinder compounds kinderhuis, kindermishandeling, kinderwelsyn and kinderoppas as stems; a bare kinder
# is left alone, so "a kinder world", "Kinder Joy" and "Kinder Bueno" pass. Afrikaans kleuter (toddler), jongspan
# (youngsters) and jongmeisie (young girl) anywhere, as none is the head or tail of a clean word. The Sesotho bacha and
# the Setswana baswa (young people) only as a whole token, so "bachata" and "Bacharach" pass; "Bacha Khan" passes as a
# name (NAMES).
AGE_STEMS = re.compile("|".join((
    r"gen(?:eration)?[_\-]?(?:z|alpha)",
    r"^gen(?:eration)?[_\-]?(?:x|y)",
    r"^teen", r"teenage", r"(?<!can)(?<!fif)(?<!six)(?<!seven)(?<!eigh)(?<!nine)(?<!thir)(?<!four)(?<!mp)teens?$",
    r"^youth(?!s?$|day|month)",
    r"(?<!thank)(?<!love)(?<!miss)(?<!see)(?<!so)(?<!bless)(?<!need)(?<!want)(?<!got)\Byouth(?!s?(?:day|month))",
    r"vijana", r"toddler", r"children(?!s?day)", r"(?<!fur)(?<!plant)(?<!sugar)(?:\Bbabies|babies\B)",
    r"pikin", r"wazee", r"youngster", r"infant(?!ry)", r"(?<!be)tween", r"firsttimevoter",
    r"young(?:er)?people", r"(?<![bfghm])old(?:er)?(?:people|heads?)",
    r"^boomer(?!ang)", r"boomers?$",
    r"mill?enn?ial", r"zoomer", r"bornfree", r"digitalnative", r"\d0sbab", r"\d0skid",
    r"(?<!s)(?<!wiz)kid(?:s|z|dies|dos?)", r"(?<!s)(?<!wiz)kid$", r"^kid(?![aeiouny]|d)", r"child(?!ren|ish)",
    r"underage", r"agemates?", r"watoto", r"kijana", r"mtoto", r"bantwan", r"umntwana",
    r"(?:^|(?<!l)[ie])ngane(?!kwan|no)",
    r"laaitj?ie", r"madala", r"amagogo", r"(?<!s)toppie(?!r|st)",
    r"^young(?!(?:er|est|s)?(?:$|brand|label|compan|business|startup|platform|channel|account|app|product|format"
    r"|genre|sound|song|track|trend|campaign|market|post|video|clip|content|tweet|thread|episode|data|record"
    r"|version|material|footage|than))",
    r"(?<![amoprt])intsha(?![cr])", r"(?<!h)abasha(?!n)",
    r"jongmens", r"tiener", r"jeug(?!dag|maand)", r"matasa(?!la)", r"^yara(?:n(?:mu|ku|su)?)?$",
    r"(?:^|awon)omode(?!le)", r"umuaka(?!ra|mu)", r"kinders(?!ur|ley|pel)", r"kindertjies",
    # bacha and baswa whole only; a run-together hashtag such as #bachaba is left to the label net.
    r"kinder(?:huis|mishandel|welsyn|oppas)", r"kleuter", r"jongspan", r"jongmeisie", r"^bacha$", r"^baswa$",
    r"zill?enn?ial", r"juvenile", r"^igen(?:eration)?s?$", r"amakhehla",
)))
# ama before a year, a decade or 2k, the ZA word for people born then ("ama2000", "Ama-2000s", "ama 2k", "ama90s"),
# read on the whole text so the vectorizer's "ama 2000" is caught too. ama must not follow a letter, so "panama2000",
# "obama2008" and "drama2024" pass, as do "amapiano", "ama2", "a2000" and "2000s nostalgia", an era and no audience.
# Between ama and the year may stand spaces, underscores, dots, hyphens, the Unicode hyphens and dashes, and minus.
SEPARATORS = r"[\s_.\-\u2010-\u2015\u2212]"
AMA_YEAR = re.compile(rf"(?<![a-z])ama{SEPARATORS}*(?:(?:19|20)\d\d|\d0s|2k)")
# Proper names that hold an age word, passed when the whole value is one of them after casefolding and dropping
# spaces, "#" and punctuation: "Wizkid", "#YoungStunna", "Kid Cudi", "Children's Day". A name is not an audience
# description (RULES.md rule 1), so it may be stored and labelled. A value that only contains a name ("young stunna
# fans are teens", "youngstunnafans") is scanned whole. Entries must be proper names of people, works or days,
# never audience words.
NAMES = {"wizkid", "youngstunna", "youngjonn", "kidcudi", "teenagedream", "childrensday", "youthday",
         "nationalyouthday", "khehlasitole", "youngjohn", "youngstacpt", "kiddominant", "kidx", "kidtini", "youngduu",
         "madalakunene", "youngfamousafrican", "youngthug", "youngmoney", "kidink",
         "yarainternational", "bachakhan"}
# umu aka (Igbo, children) and its possessive umu akam, and the Afrikaans jong mense and jong meisies (young people,
# young girls), written as two words apart by any of AMA_YEAR's separators and read on the whole text as AMA_YEAR is;
# umuaka, jongmense and jongmeisie are in AGE_STEMS. "umu akara" passes. The Yoruba awon omo (the children) is read
# the same way, apart or run together, and only as a whole word, so "awon omoluabi" passes.
UMU_AKA = re.compile(rf"(?<![a-z])umu{SEPARATORS}+akam?(?![a-z])")
JONG_MENSE = re.compile(rf"(?<![a-z])jong{SEPARATORS}+(?:mense|meisie)")
AWON_OMO = re.compile(rf"(?<![a-z])awon{SEPARATORS}*omo(?![a-z])")
CHILDRENS_DAY = re.compile(r"\bchildren['’]?s?\s+day\b", re.I)
# The age patterns written for hashtags. The rest judge a word by its neighbours ("young brand", "youth day"), so a
# word standing alone would fail them where its keyword or label passes.
HASHTAG_PATTERNS = [p for p in AGE_PATTERNS if "#" in p.pattern]


def fold(text):
    """text in NFKD with its combining marks dropped, so the tone-marked Yoruba and Igbo "ọmọdé" and
    "ụmụaka" are scanned as omode and umuaka."""
    return "".join(c for c in unicodedata.normalize("NFKD", str(text or "")) if unicodedata.category(c) != "Mn")


def passes_age_scan(text):
    text = fold(text)
    if re.sub(r"[\W_]+", "", text.casefold()) in NAMES:
        return True
    text = CHILDRENS_DAY.sub("childrensday", text)
    lower = text.lower()
    if GEN_WORDS & set(re.split(r"[\W_]+", lower)) or any(
            p.search(lower) for p in (AMA_YEAR, UMU_AKA, JONG_MENSE, AWON_OMO)):
        return False
    # The vectorizer lowercases and drops the "#", so "#GenZProtests" reaches here as "genzprotests".
    for token in re.findall(r"[\w\-]+", lower):
        if AGE_STEMS.search(token) or any(p.search(f"#{token}") for p in HASHTAG_PATTERNS):
            return False
    return not any(p.search(text) for p in AGE_PATTERNS) and not DEMOGRAPHIC.search(text)
