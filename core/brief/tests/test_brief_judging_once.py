"""W8-DEC-05b: the brief judges each market once, on its own frozen evidence context, and never carries one market's
verdict to another. Nothing reuses a judgement across markets today; this pins that."""

from core.brief.tests.test_brief_job import FakeModel, brief, held_items, payload, world


def test_each_market_is_judged_once_on_its_own_prompt_and_no_verdict_crosses_markets():
    # Judging is once per market on a frozen evidence context. A critic that holds NG's card must not hold ZA's or
    # KE's, and each writer call names only its own market.
    import re

    class NgIsHeld(FakeModel):
        def complete_json(self, *, system, user, schema, model, max_tokens):
            out, usage = super().complete_json(system=system, user=user, schema=schema, model=model,
                                               max_tokens=max_tokens)
            if "ruled_out" in schema["properties"] and "Market: NG." in user:
                out = dict(out, ruled_out=False)
            return out, usage

    model = NgIsHeld()
    r = brief(world(n=1), model=model, workers=1)
    writer_markets = [re.search(r"\bMarket: (ZA|NG|KE)\.", c["user"]).group(1) for c in model.calls
                      if not c["support"] and not c.get("critic")]
    assert sorted(writer_markets) == ["KE", "NG", "ZA"]
    assert [c["item_id"] for c in payload(r, "ZA")["cards"]] == ["za1"]
    assert [c["item_id"] for c in payload(r, "KE")["cards"]] == ["ke1"]
    assert payload(r, "NG")["cards"] == [] and set(held_items(r, "NG")) == {"ng1"}
