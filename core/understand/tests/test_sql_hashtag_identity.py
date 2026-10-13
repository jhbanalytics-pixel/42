from datetime import timedelta

from core.understand import cluster
from core.understand.tests.test_cluster import CORE, DAY, core_db, duck_execute, map_row


def test_hashtag_spellings_combine_before_the_prior_top_ten_cutoff():
    con = core_db()
    con.execute(f"INSERT INTO {CORE}.cultural_map VALUES ({','.join('?' * 17)})",
                map_row("old", [0.25] * 768, last_seen=DAY))
    tags = ["＃Ａｍａｐｉａｎｏ", "#Amapiano", " amapiano ", "AMAPIANO"] * 2
    tags += [f"filler{i}" for i in range(10) for _ in range(3)]
    con.execute(f"INSERT INTO {CORE}.clusters VALUES (?, 'prior', 'za', 'old', 'new', 'old', [], [], ?)",
                [DAY - timedelta(days=1), [0.25] * 768])
    for index, tag in enumerate(tags):
        post_id = str(index)
        con.execute(f"INSERT INTO {CORE}.posts VALUES (?, 'tiktok', NULL, 'text', ?, NULL, ?)",
                    [post_id, [tag], DAY])
        con.execute(f"INSERT INTO {CORE}.cluster_members VALUES ('prior', ?, 1)", [post_id])
    [row] = duck_execute(con)(cluster.load("cluster_items"), cluster.item_params(DAY, "za"))
    assert len(row["hashtags"]) == 10 and row["hashtags"][0] == "amapiano"
