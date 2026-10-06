"""Cross-connector dedup between youtube (API) and youtube_scrape (yt-dlp).

Both connectors run the same query terms per market, so the same video can
land twice in one market frame set. The dedup keeps the API row (richer
stats) and drops the youtube_scrape duplicate. Audit finding E-1.
"""

import pandas as pd
import scripts.run_rss_now as cron


def _api_frame(video_ids):
    return pd.DataFrame(
        [
            {
                "source": "Some Channel",
                "platform": "youtube",
                "content_type": "video",
                "query_group": "youtube_search",
                "url": f"https://www.youtube.com/watch?v={vid}",
                "text": f"api video {vid}",
            }
            for vid in video_ids
        ]
    )


def _scrape_frame(urls):
    return pd.DataFrame(
        [
            {
                "source": "Some Channel",
                "platform": "youtube",
                "content_type": "video/scrape",
                "query_group": "youtube_scrape",
                "url": url,
                "text": f"scrape row {i}",
            }
            for i, url in enumerate(urls)
        ]
    )


class TestExtractYoutubeVideoId:
    def test_watch_url(self):
        assert cron._youtube_video_id("https://www.youtube.com/watch?v=abcDEF12345") == (
            "abcDEF12345"
        )

    def test_youtu_be_url(self):
        assert cron._youtube_video_id("https://youtu.be/abcDEF12345") == "abcDEF12345"

    def test_shorts_url(self):
        assert cron._youtube_video_id("https://www.youtube.com/shorts/abcDEF12345") == (
            "abcDEF12345"
        )

    def test_malformed_url_returns_none(self):
        assert cron._youtube_video_id("not a url") is None
        assert cron._youtube_video_id("") is None
        assert cron._youtube_video_id(None) is None


class TestDedupYoutubeScrapeFrames:
    def test_same_video_in_both_sources_drops_scrape_row(self):
        api = _api_frame(["abcDEF12345"])
        scrape = _scrape_frame(["https://www.youtube.com/watch?v=abcDEF12345"])
        out = cron.dedup_youtube_scrape_frames([api, scrape])
        merged = pd.concat(out, ignore_index=True)
        assert len(merged) == 1
        assert merged.iloc[0]["query_group"] == "youtube_search"

    def test_scrape_only_video_kept(self):
        api = _api_frame(["abcDEF12345"])
        scrape = _scrape_frame(["https://www.youtube.com/watch?v=zzzZZZ99999"])
        out = cron.dedup_youtube_scrape_frames([api, scrape])
        merged = pd.concat(out, ignore_index=True)
        assert len(merged) == 2
        assert (merged["query_group"] == "youtube_scrape").sum() == 1

    def test_malformed_scrape_url_kept(self):
        api = _api_frame(["abcDEF12345"])
        scrape = _scrape_frame(["garbage-url-no-id"])
        out = cron.dedup_youtube_scrape_frames([api, scrape])
        merged = pd.concat(out, ignore_index=True)
        assert len(merged) == 2

    def test_no_api_rows_leaves_scrape_untouched(self):
        scrape = _scrape_frame(["https://www.youtube.com/watch?v=abcDEF12345"])
        out = cron.dedup_youtube_scrape_frames([scrape])
        merged = pd.concat(out, ignore_index=True)
        assert len(merged) == 1

    def test_mixed_frame_drops_only_duplicates(self):
        api = _api_frame(["abcDEF12345", "qqqQQQ11111"])
        scrape = _scrape_frame(
            [
                "https://www.youtube.com/watch?v=abcDEF12345",
                "https://youtu.be/freshID0001",
                "broken",
            ]
        )
        out = cron.dedup_youtube_scrape_frames([api, scrape])
        merged = pd.concat(out, ignore_index=True)
        assert len(merged) == 4
        scrape_urls = set(merged.loc[merged["query_group"] == "youtube_scrape", "url"])
        assert scrape_urls == {"https://youtu.be/freshID0001", "broken"}

    def test_non_youtube_frames_pass_through(self):
        other = pd.DataFrame([{"platform": "tiktok", "text": "x"}])
        out = cron.dedup_youtube_scrape_frames([other])
        assert len(out) == 1
        assert out[0].equals(other)

    def test_empty_frame_list(self):
        assert cron.dedup_youtube_scrape_frames([]) == []
