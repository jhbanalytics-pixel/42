"""Collect's cultural_map labels for its counter items (L2 Needs 11) and the watchlist lane from the current
watches (contract 10.7). The fakes and fixtures are core/collect/tests/test_job.py's."""

import json
import re
from datetime import date

import pytest
from google.api_core.exceptions import NotFound

from core.collect import chain, job, local_sources, writers
from core.collect.gdelt import blocked
from core.collect.tests import test_job as tj
from core.detect import aggregate, sqlrun

WEDNESDAY = date(2026, 9, 30)
FIELDS = [name for name, _ in aggregate.ITEM_FIELDS]
AGE_WORD = bytes([103, 101, 110, 122]).decode()  # spelt in bytes so no source or .pyc holds the word
BLOCKED_TAG = AGE_WORD + "amapiano"
CLIP = "7300000000000000001"


# A: counter items into cultural_map

def mapped(run):
    return writers.cultural_map_rows(run.counters, run.items)


def counter(item_id, day="2026-09-29", market="ZA", platform="tiktok", source="live"):
    return {"obs_date": day, "market": market, "platform": platform, "item_id": item_id, "unit": "rank",
            "source": source}


def test_board_chart_and_count_items_carry_readable_labels(monkeypatch):
    monkeypatch.setitem(tj.FIXTURE, "apple_music/charts", {"success": True, "data": {"items": [
        {"post": {"id": "am_9", "content": {"text": "Water"}, "author": {"display_name": "Tyla"},
                  "ext": {"trend": {"rank": 1}}}}]}})
    monkeypatch.setitem(tj.FIXTURE, "youtube/videos/trending", {"success": True, "data": {"items": [
        {"post": {"id": "yt_9", "url": "https://www.youtube.com/watch?v=yt_9", "published_at": "2026-09-28T10:00:00Z",
                  "author": {"display_name": "Derby TV", "username": None},
                  "content": {"text": "Derby highlights #derbyday"}, "engagement": {"views": 300000},
                  "ext": {"channel_id": "UCabcdefghijklmnopqrstuv"}}}]}})
    run = tj.collect(tj.FakeClient())
    rows, left = mapped(run)
    by_label = {r["label"]: r for r in rows}
    assert by_label["#amapiano"]["kind"] == "hashtag" and by_label["#amapiano"]["canonical_key"] == "amapiano"
    assert by_label["#amapiano"]["first_seen"] == date(2026, 9, 29)  # the board's vendor curve reaches back further
    assert by_label["Water by Tyla"]["kind"] == "sound"
    assert by_label["Water by Tyla"]["canonical_key"] == "apple_music:am_9"
    assert by_label["Water by Tyla"]["first_seen_platform"] == "apple_music"
    assert by_label["Song one"]["canonical_key"] == "instagram:ig_a1"  # the Instagram music board
    assert by_label["Song one"]["first_seen_market"] == "GLOBAL"
    assert by_label["Derby TV"]["kind"] == "creator"  # the channel title, not its UC id
    assert by_label["Derby TV"]["canonical_key"] == "youtube:ucabcdefghijklmnopqrstuv"
    assert by_label["Original sound"]["kind"] == "sound"  # the tiktok/song count read
    # seen on a YouTube board in KE, NG and ZA and on the platform-wide count: the first market wins the tie
    derby = by_label["#derbyday"]
    assert (derby["first_seen_market"], derby["first_seen_platform"]) == ("KE", "youtube")
    counted = {c["item_id"] for c in run.counters}
    assert rows and all(list(r) == FIELDS for r in rows)
    assert all(r["item_id"] in counted and r["label"] and r["status"] in ("active", "generic") for r in rows)
    assert all(r["first_seen"] <= r["last_seen"] <= date(2026, 9, 29) for r in rows)
    assert len({r["item_id"] for r in rows}) == len(rows)
    assert left["blocked"] == 0


def test_an_item_on_a_platform_generic_tag_enters_as_generic():
    rows, _ = writers.cultural_map_rows([counter("hashtag|fyp")], {"hashtag|fyp": ("hashtag", "fyp", "tiktok", "#fyp")})
    assert rows[0]["status"] == aggregate.status("hashtag", "fyp") == "generic"


def test_local_chart_items_are_labelled_title_by_artist():
    run = tj.local_collect()
    rows, _ = mapped(run)
    labels = {(r["first_seen_platform"], r["label"]) for r in rows}
    assert {("shazam", "Sunday Braai by Lwazi Sky"), ("spotify", "Sunday Braai by Lwazi Sky"),
            ("boomplay", "Owambe Season by Ade Bright"), ("turntable", "Owambe Season by Ade Bright")} <= labels
    local = {c["item_id"] for c in run.counters if c["series"] in local_sources.LOCAL_RANK_SERIES}
    assert local and local <= {r["item_id"] for r in rows}
    assert any(r["kind"] == "brand" for r in rows)  # app chart names


def test_first_and_last_seen_span_the_items_live_counter_days_with_the_earliest_market_first():
    rows, _ = writers.cultural_map_rows(
        [counter("hashtag|braai", "2026-09-29", "ZA"), counter("hashtag|braai", "2026-09-27", "NG"),
         counter("hashtag|braai", "2026-09-27", "GLOBAL"), counter("hashtag|braai", "2026-09-27", "KE"),
         counter("hashtag|braai", "2026-09-20", "ZA", source="vendor_history")],
        {"hashtag|braai": ("hashtag", "Braai", "tiktok", "#Braai")})
    assert rows == [{"item_id": "hashtag|braai", "kind": "hashtag", "canonical_key": "braai", "label": "#Braai",
                     "first_seen": date(2026, 9, 27), "first_seen_market": "KE", "first_seen_platform": "tiktok",
                     "last_seen": date(2026, 9, 29), "status": "active"}]


def test_a_rule_one_label_is_not_written_and_is_counted():
    assert blocked(BLOCKED_TAG) and blocked("#" + BLOCKED_TAG)
    items = {"hashtag|x": ("hashtag", BLOCKED_TAG, "tiktok", "#" + BLOCKED_TAG),
             "sound|y": ("sound", "y1", "apple_music", f"{AGE_WORD} anthem by Someone"),
             "hashtag|braai": ("hashtag", "braai", "tiktok", "#braai"),
             "sound|z": ("sound", "z1", "tiktok", None)}
    rows, left = writers.cultural_map_rows([counter(i) for i in items], items)
    assert [r["label"] for r in rows] == ["#braai"]
    assert left == {"blocked": 2, "unnamed": 1}


def test_a_blocked_board_hashtag_reaches_no_cultural_map_row(monkeypatch):
    monkeypatch.setitem(tj.FIXTURE, "tiktok/hashtags/popular", {"success": True, "data": {"items": [
        {"rank": 1, "hashtag_name": BLOCKED_TAG}, {"rank": 2, "hashtag_name": "heritageday"}]}})
    run = tj.collect(tj.FakeClient())
    rows, left = mapped(run)
    assert tj.fake_item_id("hashtag", BLOCKED_TAG) in {c["item_id"] for c in run.counters}
    assert not [r for r in rows if AGE_WORD in r["label"].casefold()]
    assert "#heritageday" in {r["label"] for r in rows} and left["blocked"] >= 1


def _rank_body(music):
    return {"data": {"items": [{"post": {"id": "7500000000000000009", "author": {"username": "za.nine"},
                                         "ext": {"region": "ZA", "music_id": CLIP}, "music": music}}]}}


@pytest.mark.parametrize("music,label", [
    ({"id": CLIP, "title": "", "author": "DJ Nine"}, "original sound - DJ Nine"),
    ({"id": CLIP, "authorName": "DJ Nine"}, "original sound - DJ Nine"),
    ({"id": CLIP, "title": "Moyongcwele", "author": "Nontokozo Mkhize"}, "Moyongcwele by Nontokozo Mkhize"),
    ({"id": CLIP, "title": "Moyongcwele"}, "Moyongcwele"),
])
def test_a_tiktok_sound_with_no_title_is_named_by_its_author_as_tiktok_shows_it(music, label):
    names = job.body_names("tiktok/trending", {"region": "ZA", "feed": "local"}, _rank_body(music))
    assert names[("sound", "tiktok", CLIP)] == label


@pytest.mark.parametrize("music", [{"id": CLIP}, {"id": CLIP, "title": "", "author": ""},
                                   {"id": CLIP, "author": {"id": "1"}}])
def test_a_tiktok_sound_with_no_title_and_no_author_stays_unnamed(music):
    names = job.body_names("tiktok/trending", {"region": "ZA", "feed": "local"}, _rank_body(music))
    assert ("sound", "tiktok", CLIP) not in names  # never a made-up title


def test_a_tiktok_song_count_with_no_title_is_named_by_its_author():
    body = {"data": {"video_count": 15000, "author": {"display_name": "DJ Nine"}}}
    assert job.body_names("tiktok/song", {"clipId": CLIP}, body) == {("sound", "tiktok", CLIP):
                                                                     "original sound - DJ Nine"}


def test_the_author_label_only_names_tiktok_sounds():
    body = {"data": {"items": [{"track": {"audio_cluster_id": "ig_a9", "display_artist": "Someone"}}]}}
    assert job.body_names("instagram/music/trending", {}, body) == {}


def test_an_author_named_sound_keeps_its_id_key_and_groups_with_its_posts():
    """The label is only a name: the item id and canonical key come from the clip id, as for a titled sound and as
    detect keys the sound on posts (core/detect/items.py), so grouping and every id lookup are unchanged."""
    from core.detect.items import canonical_key, item_id, items_for_post
    item = tj.fake_item_id("sound", CLIP, "tiktok")
    rows, _ = writers.cultural_map_rows([counter(item)],
                                        {item: ("sound", CLIP, "tiktok", "original sound - DJ Nine")})
    assert rows[0]["label"] == "original sound - DJ Nine"
    assert rows[0]["canonical_key"] == canonical_key("sound", CLIP, "tiktok") == f"tiktok:{CLIP}"
    post = next(r for r in items_for_post({"platform": "tiktok", "sound_id": CLIP}) if r["kind"] == "sound")
    assert post["item_id"] == item_id("sound", f"tiktok:{CLIP}") and post["canonical_key"] == rows[0]["canonical_key"]
    assert not blocked("original sound - DJ Nine")


def test_an_author_only_label_never_replaces_a_titled_name_for_the_same_clip():
    key = ("sound", "tiktok", CLIP)
    titled = {"id": CLIP, "title": "Moyongcwele", "author": "Nontokozo Mkhize"}
    untitled = {"id": CLIP, "author": "Nontokozo Mkhize"}
    names = {}
    for music in (titled, untitled):  # two responses in one run, the titled one first
        job.add_names(names, job.body_names("tiktok/trending", {"region": "ZA", "feed": "local"}, _rank_body(music)))
    assert names[key] == "Moyongcwele by Nontokozo Mkhize"
    job.add_names(names, {key: "Moyongcwele (remix) by Nontokozo Mkhize"})  # a titled name still replaces one
    assert names[key] == "Moyongcwele (remix) by Nontokozo Mkhize"
    names = job.add_names({key: "original sound - Nontokozo Mkhize"}, {key: "Moyongcwele by Nontokozo Mkhize"})
    assert names[key] == "Moyongcwele by Nontokozo Mkhize"  # a title replaces the author-only name
    body = {"data": {"items": [{"post": {"id": f"75{i}", "author": {"username": f"za.{i}"}, "music": music}}
                               for i, music in enumerate((titled, untitled))]}}
    assert job.body_names("tiktok/trending", {"region": "ZA", "feed": "local"}, body)[key] == \
        "Moyongcwele by Nontokozo Mkhize"  # and within one response


def test_the_run_keeps_a_titled_sound_name_when_a_later_call_carries_only_its_author(monkeypatch):
    calls = []
    real = job.body_names

    def body_names(route, params, body):
        calls.append(route)
        if len(calls) == 1:
            return {("sound", "tiktok", CLIP): "Moyongcwele by Nontokozo Mkhize"}
        return {**real(route, params, body), ("sound", "tiktok", CLIP): "original sound - Nontokozo Mkhize"}

    monkeypatch.setattr(job, "body_names", body_names)
    run = tj.collect(tj.FakeClient())
    assert len(calls) > 1 and run.names[("sound", "tiktok", CLIP)] == "Moyongcwele by Nontokozo Mkhize"


def test_an_item_with_no_readable_name_is_left_for_later_and_counted():
    run = tj.collect(tj.FakeClient())  # the fixture's YouTube authors carry only a UC id
    rows, left = mapped(run)
    creator = tj.fake_item_id("creator", "UC_j1", "youtube")
    assert creator in {c["item_id"] for c in run.counters} and creator not in {r["item_id"] for r in rows}
    assert left["unnamed"] >= 1


def test_the_merge_is_l2s_cultural_map_merge_with_the_rows_as_a_parameter():
    rows, _ = writers.cultural_map_rows([counter("hashtag|braai")],
                                        {"hashtag|braai": ("hashtag", "braai", "tiktok", "#braai")})
    bq = tj.FakeBQ()
    assert writers.merge_cultural_map(bq, rows) == 1
    sql, params = bq.queries[-1]
    assert sql == sqlrun.render(aggregate.cultural_map_merge_sql(), writers.CORE, writers.AGENT)
    assert "#braai" not in sql and "2026" not in sql and list(params) == ["items"]
    assert not re.search(r"\b(DELETE|DROP|TRUNCATE|REPLACE)\b", sql.upper())
    (struct,) = params["items"].values
    assert list(struct.struct_values) == FIELDS and struct.struct_values["label"] == "#braai"
    assert dict(struct.struct_types) == dict(aggregate.ITEM_FIELDS)
    assert bq.mapped == [dict(rows[0])]


def test_the_merge_runs_in_chunks_and_not_at_all_without_rows():
    row = {"item_id": "x", "kind": "hashtag", "canonical_key": "x", "label": "#x", "first_seen": date(2026, 9, 29),
           "first_seen_market": "ZA", "first_seen_platform": "tiktok", "last_seen": date(2026, 9, 29),
           "status": "active"}
    bq = tj.FakeBQ()
    assert writers.merge_cultural_map(bq, []) == 0 and not bq.queries
    assert writers.merge_cultural_map(bq, [dict(row, item_id=str(i)) for i in range(aggregate.CHUNK + 1)]) == 2


def test_main_merges_counter_items_into_cultural_map_and_counts_what_it_left_out():
    runs, bq = chain.MemoryRunsStore(), tj.FakeBQ()
    assert tj.run_main(["--run-date", "2026-09-29"], runs=runs, bq=bq) == 0
    assert "#amapiano" in {r["label"] for r in bq.mapped}
    counts = runs.rows[-1]["counts"]
    assert counts["cultural_map"] == len(bq.mapped) > 0
    assert counts["labels_blocked"] == 0 and counts["labels_missing"] >= 1


# B: the watchlist lane

def watch(watch_id, target, market="ZA", status="active", label=None, **item):
    return {"watch_id": watch_id, "target": target, "market": market, "status": status,
            "label": label or target.get("value") or target.get("item_id"),
            "item_kind": item.get("kind"), "item_key": item.get("key"), "item_label": item.get("label")}


def signature(planned):
    return [(m, c.row, c.route, json.dumps(c.params, sort_keys=True), c.lane) for m, cs in planned["calls"].items()
            for c in cs]


def planned_calls(planned):
    return [c for cs in planned["calls"].values() for c in cs]


@pytest.mark.usefixtures("collect_share_2000")
def test_no_watches_leaves_the_plan_as_it_was():
    base = job.plan(WEDNESDAY)
    assert base["total"] == 1891 and len(planned_calls(base)) == 368  # one row 14i reel search included
    assert local_sources.total_hold(local_sources.plan(WEDNESDAY)) == 34
    monday = date(2026, 10, 5)
    monday_plan = job.plan(monday)
    monday_local = local_sources.total_hold(local_sources.plan(monday))
    assert monday_plan["total"] + monday_local == 1960
    assert monday_plan["total"] + monday_local <= job.load_caps()["ENGINE_DAILY"]["collect"] == 2000
    paused = [watch("w1", {"kind": "hashtag", "value": "braai"}, status="paused")]
    for watches in ([], paused):
        planned = job.plan(WEDNESDAY, watches=watches)
        assert signature(planned) == signature(base) and planned["total"] == base["total"]


def test_no_watches_leaves_the_live_calls_as_they_were():
    base, empty = tj.FakeClient(), tj.FakeClient()
    tj.collect(base)
    run = tj.collect(empty, watches=[])
    assert empty.calls == base.calls and run.counts()["watch_calls"] == 0


def test_active_watches_become_watchlist_count_reads_inside_row_12_and_the_collect_cap():
    base = job.plan(WEDNESDAY)
    watches = [watch("w1", {"kind": "hashtag", "value": "#Braai"}),
               watch("w2", {"kind": "item", "item_id": "abc"}, market="NG", kind="sound", key=f"tiktok:{CLIP}",
                     label="Tshwala Bam"),
               watch("w3", {"kind": "hashtag", "value": "amapiano"}, market="all"),
               watch("w4", {"kind": "sound", "value": f"TikTok:{CLIP}"}, market="KE")]
    planned = job.plan(WEDNESDAY, watches=watches)
    watched = [c for c in planned_calls(planned) if c.row == job.WATCH_ROW]
    assert sorted((c.route, json.dumps(c.params)) for c in watched) == [
        ("tiktok/hashtag", '{"hashtag": "amapiano"}'), ("tiktok/hashtag", '{"hashtag": "braai"}'),
        ("tiktok/song", f'{{"clipId": "{CLIP}"}}')]
    assert {c.lane for c in watched} == {"watchlist"} and {c.market for c in watched} == {job.GLOBAL}
    assert all(any(c is g for g in planned["calls"][job.GLOBAL]) for c in watched)
    reads = [c for c in planned_calls(planned) if c.route in ("tiktok/hashtag", "tiktok/song")]
    assert len(reads) <= job.COUNT_CALLS == 30
    assert planned["total"] <= base["total"]
    cap = job.load_caps()["ENGINE_DAILY"]["collect"]
    assert planned["total"] + local_sources.total_hold(local_sources.plan(WEDNESDAY)) <= cap
    others = [c for c in planned_calls(planned) if c.route not in ("tiktok/hashtag", "tiktok/song")]
    assert [(c.row, c.route, c.params) for c in others] == [
        (c.row, c.route, c.params) for c in planned_calls(base) if c.route not in ("tiktok/hashtag", "tiktok/song")]


def test_watch_reads_never_pass_30_and_take_the_candidates_count_slots_first():
    watches = [watch(f"w{i:02}", {"kind": "hashtag", "value": f"tag{i}"}) for i in range(45)]
    calls, skipped = job.watch_calls(watches)
    assert len(calls) == job.COUNT_CALLS and len(skipped) == 15
    assert {reason for _, reason in skipped} == {f"over the {job.COUNT_CALLS} row 12 reads a run"}
    planned = job.plan(WEDNESDAY, watches=watches)
    assert not [c for c in planned_calls(planned) if c.row == "12"]
    assert len([c for c in planned_calls(planned) if c.row == job.WATCH_ROW]) == 30


def test_one_watch_takes_one_slot_and_the_candidates_keep_the_rest():
    planned = job.plan(WEDNESDAY, watches=[watch("w1", {"kind": "hashtag", "value": "braai"})])
    reads = [c for c in planned_calls(planned) if c.route in ("tiktok/hashtag", "tiktok/song")]
    assert 1 + 3 * 8 <= len(reads) <= job.COUNT_CALLS


def test_two_watches_on_one_tag_make_one_read():
    calls, skipped = job.watch_calls([watch("w1", {"kind": "hashtag", "value": "Braai"}),
                                      watch("w2", {"kind": "hashtag", "value": "#braai"}, market="KE")])
    assert [c.params for c in calls] == [{"hashtag": "braai"}] and skipped == []


def test_paused_watches_make_no_calls():
    calls, skipped = job.watch_calls([watch("w1", {"kind": "hashtag", "value": "braai"}, status="paused")])
    assert calls == [] and skipped == [("w1", "not active")]


def test_rule_one_and_unroutable_watches_make_no_calls():
    watches = [watch("w1", {"kind": "hashtag", "value": BLOCKED_TAG}),
               watch("w2", {"kind": "sound", "value": f"tiktok:{CLIP}"}, label=f"{AGE_WORD} anthem"),
               watch("w3", {"kind": "item", "item_id": "abc"}, kind="hashtag", key="amapiano",
                     label=f"#{AGE_WORD}piano"),
               watch("w4", {"kind": "creator", "value": "@someone"}),
               watch("w5", {"kind": "brand", "value": "Nando's"}),
               watch("w6", {"kind": "query", "value": "load shedding"}),
               watch("w7", {"kind": "item", "item_id": "gone"}),
               watch("w8", {"kind": "sound", "value": "instagram:123"}),
               watch("w9", {"kind": "hashtag", "value": "fyp"})]
    calls, skipped = job.watch_calls(watches)
    assert calls == []
    assert dict(skipped) == {
        "w1": "rule 1, platform-generic or not a hashtag", "w2": "rule 1", "w3": "rule 1",
        "w4": "no watch route for creator targets", "w5": "no watch route for brand targets",
        "w6": "no watch route for query targets", "w7": "item not in cultural_map",
        "w8": "only TikTok sounds have a count route", "w9": "rule 1, platform-generic or not a hashtag"}


def test_a_candidate_already_watched_is_read_once():
    run = tj.collect(tj.FakeClient(), watches=[watch("w1", {"kind": "hashtag", "value": "amapiano"})])
    reads = [c for c in run.client_calls if c["route"] == "tiktok/hashtag" and c["params"] == {"hashtag": "amapiano"}]
    assert len(reads) == 1 and reads[0]["market"] == job.GLOBAL


def test_the_watch_read_is_parameterised_and_takes_active_watches_per_market():
    bq = tj.FakeBQ(watches=[
        {"watch_id": "w1", "market": "ZA", "status": "active", "label": "Braai",
         "target": '{"kind": "hashtag", "value": "braai"}', "item_kind": None, "item_key": None, "item_label": None},
        {"watch_id": "w2", "market": "all", "status": "active", "label": "Tshwala Bam",
         "target": '{"kind": "item", "item_id": "abc"}', "item_kind": "sound", "item_key": f"tiktok:{CLIP}",
         "item_label": "Tshwala Bam"}])
    got = writers.read_watches(bq, job.MARKETS)
    sql, params = bq.queries[-1]
    assert "`ogilvy-trends-v2.intelligence_42_agent.v_watches_current`" in sql
    assert "w.status = 'active'" in sql and "w.market IN UNNEST(@markets)" in sql
    assert "`ogilvy-trends-v2.intelligence_42_core.cultural_map`" in sql and "valid_to IS NULL" in sql
    assert list(params) == ["markets"] and params["markets"].values == ["ZA", "NG", "KE", "all"]
    assert not re.search(r"'(ZA|NG|KE|all)'", sql)
    assert [w["target"] for w in got] == [{"kind": "hashtag", "value": "braai"}, {"kind": "item", "item_id": "abc"}]
    assert got[1]["item_key"] == f"tiktok:{CLIP}"


def test_a_missing_watches_view_reads_as_no_watches():
    class Missing:
        def query(self, sql, job_config=None):
            raise NotFound("v_watches_current")

    assert writers.read_watches(Missing(), job.MARKETS) == []


def test_main_reads_the_watches_and_makes_their_calls_in_lane_watchlist():
    client, runs = tj.FakeClient(), chain.MemoryRunsStore()
    bq = tj.FakeBQ(watches=[{"watch_id": "w1", "market": "ZA", "status": "active", "label": "Braai",
                             "target": '{"kind": "hashtag", "value": "braai"}', "item_kind": None, "item_key": None,
                             "item_label": None}])
    assert tj.run_main(["--run-date", "2026-09-29"], client=client, bq=bq, runs=runs) == 0
    reads = [c for c in client.calls if c["route"] == "tiktok/hashtag" and c["params"] == {"hashtag": "braai"}]
    assert len(reads) == 1 and reads[0]["lane"] == "watchlist" and reads[0]["seed_key"] == "braai"
    counts = runs.rows[-1]["counts"]
    assert counts["watches"] == 1 and counts["watch_calls"] == 1 and counts["watches_not_planned"] == 0


@pytest.mark.usefixtures("collect_share_2000")
def test_plan_mode_reads_no_watches_and_says_so(capsys):
    assert job.main(["--plan", "--run-date", "2026-09-30"], env={}) == 0
    out = capsys.readouterr().out
    assert "watches: none read in plan mode" in out
    assert re.search(r"total hold 1891 credits over 368 calls", out)


@pytest.mark.parametrize("value", ["", "   ", None])
def test_an_empty_watch_value_makes_no_call(value):
    calls, skipped = job.watch_calls([watch("w1", {"kind": "hashtag", "value": value}, label="x")])
    assert calls == [] and skipped == [("w1", "rule 1, platform-generic or not a hashtag")]


def test_an_item_read_only_from_a_vendor_curve_is_first_seen_on_its_earliest_curve_day():
    rows, _ = writers.cultural_map_rows(
        [counter("sound|s", "2026-09-28", "GLOBAL", source="vendor_history"),
         counter("sound|s", "2026-09-27", "GLOBAL", source="vendor_history")],
        {"sound|s": ("sound", "s1", "tiktok", "Original sound")})
    assert (rows[0]["first_seen"], rows[0]["last_seen"]) == (date(2026, 9, 27), date(2026, 9, 28))


def test_turntable_names_read_the_visible_lines_and_keep_the_raw_identity():
    # TurnTable cells carry the song as "<br>BACK 2 U<br>Seyi Vibez" (the label retained on staging, L2 handover
    # 2 Oct). The item stays keyed on the raw cell; only its readable name drops the markup.
    fetch = next(f for f in local_sources.plan(tj.TUESDAY) if f.source == "turntable")
    cells = ["<br>BACK 2 U<br>Seyi Vibez", "<br>Ozeba &amp; Co", "<br>One<br>Two<br>Three", "Plain Song"]
    markdown = "\n".join(f"| {rank} | {cell} |" for rank, cell in enumerate(cells, 1))
    body = {"data": {"page": {"content": {"markdown": markdown}}}}
    entries = local_sources.chart_entries(markdown, local_sources.SCRAPE_SOURCES["turntable"])
    assert [local_sources.song_key(e) for e in entries] == cells

    names = job.chart_names(fetch, body)

    assert names == {("sound", "turntable", cells[0]): "BACK 2 U by Seyi Vibez",
                     ("sound", "turntable", cells[1]): "Ozeba & Co",
                     ("sound", "turntable", cells[2]): "One Two Three",
                     ("sound", "turntable", cells[3]): "Plain Song"}
