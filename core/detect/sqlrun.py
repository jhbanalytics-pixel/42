"""Apply the detection views (sql/views.sql), v_item_spread (sql/spread.sql), the views L4's API reads
(sql/agent_views.sql) and run parameterised queries on BigQuery."""

import datetime
import hashlib
import re
import unicodedata
from pathlib import Path

import yaml
from google.cloud import bigquery

VIEWS_SQL = Path(__file__).parent / "sql" / "views.sql"
SPREAD_SQL = Path(__file__).parent / "sql" / "spread.sql"
NEWS_SQL = Path(__file__).parent / "sql" / "news.sql"
HUBS_YAML = Path(__file__).parents[1] / "config" / "hubs.yaml"
AGENT_VIEWS_SQL = Path(__file__).parent / "sql" / "agent_views.sql"
SENSITIVE_YAML = Path(__file__).parent / "sensitive.yaml"
POLITICAL_YAML = Path(__file__).parents[1] / "brief" / "political.yaml"
CORE = "intelligence_42_core"
AGENT = "intelligence_42_agent"

# v_sensitive_items_complete (agent_views.sql, contract 12.1 rule 2): an item takes a model reason when at least
# SENSITIVE_MODEL_MIN_POSTS of its posts the model read carry it, or at least SENSITIVE_MODEL_SHARE of them do. Low on
# purpose: a false hit only keeps a topic off pages that name a person, the safe direction.
SENSITIVE_COMPLETE_VIEW = "v_sensitive_items_complete"
SENSITIVE_MODEL_MIN_POSTS = 2
SENSITIVE_MODEL_SHARE = 0.2
# The model reasons the view reads (core/understand/enrich.py SENSITIVE), each with the category it counts as.
MODEL_CATEGORIES = {"political": "political", "religion": "religion", "health": "health", "sex_life": "sex_life",
                    "race_ethnicity": "race_ethnicity", "crime": "crime"}
# Needs Albert NA-9, open on 2 October: do public holidays with religious roots (Christmas, Easter, Good Friday,
# Lent, Eid) count as religion in the sensitive set? None until he answers. True counts the model's
# religious_holiday reason as religion; False leaves it out. While it is None the complete view is never built.
# The keyword floor (sensitive.yaml) keeps its own list either way.
RELIGIOUS_HOLIDAYS_ARE_RELIGION = None

_NAME = re.compile(r"CREATE\s+OR\s+REPLACE\s+(?:TABLE\s+FUNCTION|VIEW)\s+([\w.]+)", re.IGNORECASE)


def render(sql, core=CORE, agent=AGENT):
    return sql.replace("{core}", core).replace("{agent}", agent)


def split(sql):
    """Split a script on semicolons outside quotes and comments; drop pieces that hold only comments."""
    out, buf, i, n = [], [], 0, len(sql)
    quote = None
    while i < n:
        ch = sql[i]
        if quote:
            buf.append(ch)
            if ch == "\\":
                buf.append(sql[i + 1])
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
            buf.append(ch)
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end < 0 else end
            buf.append(sql[i:end])
            i = end
            continue
        elif ch == ";":
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    out.append("".join(buf))
    return [s.strip() for s in out if _code(s)]


def _code(piece):
    return any(line.strip() and not line.strip().startswith("--") for line in piece.splitlines())


def statements(core=CORE, agent=AGENT):
    """The CREATE statements of views.sql with dataset names filled in, in file order."""
    return [_strip_leading_comments(s) for s in split(render(VIEWS_SQL.read_text(encoding="utf-8"), core, agent))]


def _strip_leading_comments(stmt):
    lines = stmt.splitlines()
    while lines and (not lines[0].strip() or lines[0].lstrip().startswith("--")):
        lines.pop(0)
    return "\n".join(lines)


def object_name(stmt):
    return _NAME.search(stmt).group(1)


def spread_statements(core=CORE, agent=AGENT):
    """The CREATE statement of spread.sql (v_item_spread) with dataset names filled in."""
    return [_strip_leading_comments(s) for s in split(render(SPREAD_SQL.read_text(encoding="utf-8"), core, agent))]


def apply_spread(client, core=CORE, agent=AGENT):
    """Create or replace v_item_spread. It reads views.sql's views, so apply_views must have run first. Returns the
    object names."""
    names = []
    for stmt in spread_statements(core, agent):
        client.query(stmt).result()
        names.append(object_name(stmt))
    return names


def news_outlet_handles(hubs=HUBS_YAML):
    """The lower-cased handles of every hub account of kind news in hubs.yaml, all markets and panels, sorted."""
    markets = (yaml.safe_load(Path(hubs).read_text(encoding="utf-8")) or {}).get("markets") or {}
    return sorted({str(e["handle"]).lower() for m in markets.values() for panel in (m or {}).values()
                   if isinstance(panel, list) for e in panel if isinstance(e, dict) and e.get("kind") == "news"})


def news_statements(core=CORE, agent=AGENT):
    """The CREATE statements of news.sql (v_news_bridge, v_item_first_sighting, v_news_followthrough,
    v_item_origin) with dataset names and the news outlet handles filled in."""
    outlets = "CAST([" + ", ".join(_literal(h) for h in news_outlet_handles()) + "] AS ARRAY<STRING>)"
    sql = render(NEWS_SQL.read_text(encoding="utf-8"), core, agent).replace("{news_outlets}", outlets)
    return [_strip_leading_comments(s) for s in split(sql)]


def apply_news(client, core=CORE, agent=AGENT):
    """Create or replace the news.sql views in file order. They read views.sql's views, so apply_views must have
    run first. Returns the object names."""
    names = []
    for stmt in news_statements(core, agent):
        client.query(stmt).result()
        names.append(object_name(stmt))
    return names


def apply_views(client, core=CORE, agent=AGENT):
    """Create or replace every view and table function, in dependency order. Returns the object names."""
    names = []
    for stmt in statements(core, agent):
        client.query(stmt).result()
        names.append(object_name(stmt))
    return names


LOCALITY_VIEWS_SQL = Path(__file__).parent / "sql" / "locality_views.sql"


def locality_view_statements(core=CORE, agent=AGENT):
    """The CREATE statements of locality_views.sql (v_item_locality_checked, v_item_locality_current) with dataset
    names filled in, in file order."""
    return [_strip_leading_comments(s) for s in split(render(LOCALITY_VIEWS_SQL.read_text(encoding="utf-8"), core, agent))]


def apply_locality_views(client, core=CORE, agent=AGENT):
    """Create or replace the locality_v2 views. They read views.sql's v_good_runs, so apply_views must have run first.
    Returns the object names."""
    names = []
    for stmt in locality_view_statements(core, agent):
        client.query(stmt).result()
        names.append(object_name(stmt))
    return names


KEY_RULES = ("label", "keys", "words", "starts", "g4b", "edges", "inside")  # how far a term reaches
_RULE_LISTS = {"labels_only": "label", "keys_only": "keys", "whole_words_in_keys": "words",
               "start_of_keys": "starts", "anywhere_in_keys": "inside"}


def sensitive_terms(political=POLITICAL_YAML, sensitive=SENSITIVE_YAML):
    """(reason, term, key rule) triples: every market's political.yaml list as 'political' under the G4b key
    rules, then sensitive.yaml by reason: 'label' for its labels_only terms, 'keys' for keys_only, 'words'
    for whole_words_in_keys, 'starts' for start_of_keys, 'inside' for anywhere_in_keys and 'edges' for the
    rest. A term listed twice
    keeps its widest rule."""
    listed = yaml.safe_load(Path(sensitive).read_text(encoding="utf-8")) or {}
    special = {w: rule for key, rule in _RULE_LISTS.items() for w in listed.pop(key, None) or []}
    listed.pop("never_sensitive", None)
    listed.pop("gives_way_to", None)
    rules = {}
    for words in (yaml.safe_load(Path(political).read_text(encoding="utf-8")) or {}).values():
        for w in words or []:
            rules.setdefault(("political", w), "g4b")
    for reason, words in listed.items():
        for w in words or []:
            rule = special.get(w, "edges")
            rules[(reason, w)] = max(rules.get((reason, w), rule), rule, key=KEY_RULES.index)
    return [(r, t, rule) for (r, t), rule in rules.items()]


def never_sensitive(sensitive=SENSITIVE_YAML):
    """Canonical keys, letters only, that are never in the sensitive set (sensitive.yaml never_sensitive)."""
    listed = yaml.safe_load(Path(sensitive).read_text(encoding="utf-8")) or {}
    return list(dict.fromkeys(_squash(w) for w in listed.get("never_sensitive") or []))


def gives_way(sensitive=SENSITIVE_YAML):
    """{term: longer terms that take its reason away when they match the same item} (sensitive.yaml
    gives_way_to)."""
    listed = yaml.safe_load(Path(sensitive).read_text(encoding="utf-8")) or {}
    return {t: list(longer) for t, longer in (listed.get("gives_way_to") or {}).items()}


def _literal(text):
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _pattern(term):
    """An RE2 whole-word pattern for a term; a term in capitals matches only in capitals (as gatectx does), and
    so does one with a capital inside a word (PrEP), which political.yaml never has. A term with a digit
    needs a boundary that is neither letter nor digit (#419, not #dance1419)."""
    words = r"\s+".join(re.sub(r"([^\w\s])", r"\\\1", w) for w in unicodedata.normalize("NFKC", term).split())
    exact = term.isupper() or any(not (w.islower() or w.isupper() or w.istitle()) for w in term.split())
    edge = r"[^\p{L}\p{N}]" if any(ch.isdigit() for ch in term) else r"\P{L}"
    return ("" if exact else "(?i)") + f"(?:^|{edge})" + words + f"(?:{edge}|$)"


def _squash(term):
    return "".join(ch for ch in unicodedata.normalize("NFKC", term).casefold() if ch.isalpha())


def _needle(term):
    """The term's letters, lowercase; a term with no letters (419) keeps its digits instead."""
    return _squash(term) or "".join(ch for ch in unicodedata.normalize("NFKC", term) if ch.isdigit())


def _gives_way_sql(longer):
    return "".join(f"|{_squash(w)}" for w in longer) + ("|" if longer else "")


def terms_sql(terms, gives=None):
    """The terms as a BigQuery ARRAY<STRUCT<reason, term, key_rule, pattern, squashed, gives_way>> literal.
    gives_way holds the longer terms, letters only, that take the term's reason away, as '|a|b|'."""
    gives = gives_way() if gives is None else gives
    rows = [f"STRUCT({_literal(r)} AS reason, {_literal(t)} AS term, {_literal(rule)} AS key_rule, "
            f"{_literal(_pattern(t))} AS pattern, {_literal(_needle(t))} AS squashed, "
            f"{_literal(_gives_way_sql(gives.get(t, [])))} AS gives_way)" for r, t, rule in terms]
    return "[" + ",\n  ".join(rows) + "]"


def never_sql(never):
    return "ARRAY<STRING>[" + ", ".join(_literal(w) for w in never) + "]"


def list_version(terms=None, never=None):
    """sha256 of the term list and the never list as rendered into v_sensitive_items."""
    terms = sensitive_terms() if terms is None else terms
    never = never_sensitive() if never is None else never
    rendered = terms_sql(terms) + "\n" + never_sql(never)
    return "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def model_categories_sql(religious_holidays):
    """The model reasons v_sensitive_items_complete reads, each with its category, as an ARRAY literal."""
    pairs = dict(MODEL_CATEGORIES)
    if religious_holidays:
        pairs["religious_holiday"] = "religion"
    return "[" + ", ".join(f"STRUCT({_literal(r)} AS reason, {_literal(c)} AS category)"
                           for r, c in pairs.items()) + "]"


def agent_statements(core=CORE, agent=AGENT, terms=None, never=None, sensitive_complete=False,
                     religious_holidays=None):
    """The CREATE statements of agent_views.sql with the dataset names, the sensitive term list, the never list
    and their list_version filled in. v_sensitive_items_complete is left out unless sensitive_complete is true,
    since the API switches named item lists on as soon as it exists; it then needs religious_holidays (or
    RELIGIOUS_HOLIDAYS_ARE_RELIGION) to be True or False, and raises ValueError while that is still None."""
    terms = sensitive_terms() if terms is None else terms
    never = never_sensitive() if never is None else never
    holidays = RELIGIOUS_HOLIDAYS_ARE_RELIGION if religious_holidays is None else religious_holidays
    if sensitive_complete and holidays is None:
        raise ValueError(f"{SENSITIVE_COMPLETE_VIEW} waits for Albert's answer on whether religious-root holidays "
                         "count as religion (RELIGIOUS_HOLIDAYS_ARE_RELIGION is None)")
    text = AGENT_VIEWS_SQL.read_text(encoding="utf-8")
    text = (text.replace("{sensitive_terms}", terms_sql(terms)).replace("{never_sensitive}", never_sql(never))
            .replace("{list_version}", _literal(list_version(terms, never)))
            .replace("{model_categories}", model_categories_sql(bool(holidays)))
            .replace("{model_min_posts}", str(int(SENSITIVE_MODEL_MIN_POSTS)))
            .replace("{model_share}", repr(float(SENSITIVE_MODEL_SHARE))))
    out = [_strip_leading_comments(s) for s in split(render(text, core, agent))]
    if not sensitive_complete:
        out = [s for s in out if object_name(s).split(".")[-1] != SENSITIVE_COMPLETE_VIEW]
    return out


def apply_agent_views(client, core=CORE, agent=AGENT, sensitive_complete=False):
    """Create or replace the views and table function of agent_views.sql, in file order. They read views.sql's
    views, so apply_views must have run first. v_sensitive_items_complete only when sensitive_complete is true
    (agent_statements). Returns the object names."""
    names = []
    for stmt in agent_statements(core, agent, sensitive_complete=sensitive_complete):
        client.query(stmt).result()
        names.append(object_name(stmt))
    return names


def _param(name, value):
    if isinstance(value, bool):
        kind = "BOOL"
    elif isinstance(value, int):
        kind = "INT64"
    elif isinstance(value, float):
        kind = "FLOAT64"
    elif isinstance(value, datetime.datetime):
        kind = "TIMESTAMP"
    elif isinstance(value, datetime.date):
        kind = "DATE"
    else:
        kind = "STRING"
    return bigquery.ScalarQueryParameter(name, kind, value)


def query(client, sql, params=None, core=CORE, agent=AGENT):
    """Run one query with named @params; {core} and {agent} are filled in. Returns rows as dicts."""
    config = bigquery.QueryJobConfig(query_parameters=[_param(k, v) for k, v in (params or {}).items()])
    return [dict(row.items()) for row in client.query(render(sql, core, agent), job_config=config).result()]
