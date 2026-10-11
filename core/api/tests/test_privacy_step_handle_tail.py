"""An @handle in a step is masked to its end (release C review 3, minor 7). TikTok and Instagram handles take dots and
dashes, so a mask that stops at the first one leaves the tail of the name in the text: "@thabo.m_old" would read
"@***.m_old". Pinned on the stored record and on the stream, which share the rule."""
from core.api import privacy
from core.api.tests import test_privacy_ask_routes as routes
from core.api.tests.test_privacy_projection import ask_record

TEXT = "Looking at @thabo.m_old and @lindi-ree and @plain, then @tail."
EXPECTED = "Looking at @*** and @*** and @***, then @***."


def test_a_dotted_or_dashed_handle_in_a_step_is_masked_whole_on_the_record_and_the_stream():
    store = routes.RouteStore(hide={"c_ren"})
    record = ask_record()
    record["steps"] = [{"seq": 1, "text": TEXT}]
    assert [s["text"] for s in privacy.project_record(record, store)["steps"]] == [EXPECTED]
    hidden = privacy.read_hidden(store)
    event = privacy.project_event("step", {"seq": 1, "text": TEXT}, hidden, store, set(), {})
    assert event["text"] == EXPECTED
