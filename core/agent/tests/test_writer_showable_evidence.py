import copy
from datetime import date, datetime, timezone

from core.agent import checks, writer
from core.agent.context import RunContext
from core.agent.tools.warehouse import _store


BAD_ONE = "obs1_00000000000000000000000000000001"
BAD_TWO = "obs1_00000000000000000000000000000002"
GOOD = "obs1_00000000000000000000000000000003"
WINDOW = (date(2026, 9, 25), date(2026, 10, 1))


class FixedModel:
    def __init__(self, output):
        self.output = copy.deepcopy(output)
        self.calls = []

    def complete_json(self, *, system, user, schema, model, max_tokens):
        self.calls.append({"system": system, "user": user, "schema": schema, "model": model,
                           "max_tokens": max_tokens})
        return copy.deepcopy(self.output), {"input_tokens": 100, "output_tokens": 20, "usd": 0.001}


def _row(post_id, *, creator_id, handle, text):
    return {
        "post_id": post_id,
        "platform": "facebook",
        "url": f"https://www.facebook.com/reel/{post_id[-2:]}/",
        "creator_id": creator_id,
        "handle": handle,
        "published_at": datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc),
        "post_date": date(2026, 10, 1),
        "geo_market": "ZA",
        "geo_source": "place_mention",
        "text": text,
        "views": 10,
        "likes": 2,
        "comments": 1,
        "shares": 0,
        "engagement": 3,
        "home_market": None,
        "source_sightings": [],
    }


def _context(*, include_good):
    ctx = RunContext(
        run_id="r_20261001_test",
        tier="T1",
        as_of=datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc),
        market="ZA",
        window_start=WINDOW[0],
        window_end=WINDOW[1],
    )
    ctx.evidence = {
        BAD_ONE: _store(ctx, _row(BAD_ONE, creator_id=None, handle=None, text="Synthetic transit post.")),
        BAD_TWO: _store(ctx, _row(BAD_TWO, creator_id=None, handle=None, text="Synthetic food post.")),
    }
    if include_good:
        ctx.evidence[GOOD] = _store(
            ctx, _row(GOOD, creator_id="page_three", handle="page.three", text="Synthetic food post."))
    ctx.record_query("SELECT post_id FROM intelligence_42_core.posts", {},
                     [{"post_id": BAD_ONE}, {"post_id": BAD_TWO}], "retained located posts")
    return ctx


def _claim(claim_id, text, evidence_id):
    return {"id": claim_id, "text": text, "label": "observed", "kind": "observation",
            "evidence_ids": [evidence_id], "quotes": [], "numbers": []}


def _output(claims):
    return {"short_answer": "The retained posts discuss food.", "claims": claims, "so_what": [],
            "watch_next": [], "gaps": [], "context": ""}


def _write(model, ctx):
    return writer.write_answer(
        model,
        question="What is trending in South Africa?",
        as_of=ctx.as_of,
        market="ZA",
        window="2026-09-25 to 2026-10-01",
        ctx=ctx,
    )


def test_writer_excludes_k1_unshowable_posts_but_keeps_context_and_k1_rejects_citation():
    ctx = _context(include_good=True)
    retained_rows = copy.deepcopy(ctx.queries["q_1"]["rows"])
    model = FixedModel(_output([
        _claim("c1", "A stored post mentions food.", GOOD),
        _claim("c2", "A stored post mentions transit.", BAD_ONE),
        _claim("c3", "A stored post mentions food.", BAD_TWO),
    ]))

    draft, _ = _write(model, ctx)

    user = model.calls[0]["user"]
    post_blocks = [line for line in user.splitlines() if line.startswith("post ")]
    assert len(post_blocks) == 1 and GOOD in post_blocks[0]
    assert all(bad_id not in "\n".join(post_blocks) for bad_id in (BAD_ONE, BAD_TWO))
    assert BAD_ONE in user and BAD_TWO in user
    assert "do not cite" in user.casefold()
    assert ctx.queries["q_1"]["rows"] == retained_rows
    assert set(ctx.evidence) == {BAD_ONE, BAD_TWO, GOOD}
    assert ctx.writer_evidence_exclusions == {
        "count": 2,
        "excluded_ids": [BAD_ONE, BAD_TWO],
        "records": [
            {"evidence_id": BAD_ONE, "reason": "missing_required_fields", "fields": ["handle"]},
            {"evidence_id": BAD_TWO, "reason": "missing_required_fields", "fields": ["handle"]},
        ],
    }

    checked, verdicts = checks.check_answer(
        draft, ctx, warehouse=None, window=WINDOW, markets=["ZA"])
    k1 = {row["claim_id"]: row for row in verdicts if row["rule"] == "K1"}
    assert k1["c1"]["verdict"] == "pass"
    assert k1["c2"]["verdict"] == "cut"
    assert "has no handle" in k1["c2"]["reason"]
    assert k1["c3"]["verdict"] == "cut"
    assert "has no handle" in k1["c3"]["reason"]
    assert checked["status"] == "insufficient_evidence"


def test_writer_with_no_showable_posts_returns_truthful_insufficiency():
    ctx = _context(include_good=False)
    model = FixedModel(_output([]))

    draft, _ = _write(model, ctx)

    user = model.calls[0]["user"]
    assert not [line for line in user.splitlines() if line.startswith("post ")]
    assert ctx.writer_evidence_exclusions["count"] == 2
    checked, _ = checks.check_answer(
        draft, ctx, warehouse=None, window=WINDOW, markets=["ZA"])
    assert checked["status"] == "insufficient_evidence"
    assert checked["short_answer"] == checks.INSUFFICIENT
    assert {gap["what"] for gap in checked["gaps"]} >= {"Some posts found could not be quoted"}
