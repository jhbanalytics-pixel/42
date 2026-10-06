-- How many clips video reading has read on @day (BUILD.md 2.6, video.py): the understand_spend rows booked on it
-- under what video_clip, one per clip whose call was billed. Parameter: @day DATE, the SAST day the job started.
-- run_video reads no more than VIDEO_DAILY's clips less this count, so a rerun on the same day adds none past it.
SELECT COUNT(*) AS video_clip_count, COUNT(*) AS n
FROM `ogilvy-trends-v2.intelligence_42_agent.runs` AS r
WHERE r.stage = 'understand_spend'
  AND r.run_date = @day
  AND JSON_VALUE(r.counts, '$.what') = 'video_clip'
