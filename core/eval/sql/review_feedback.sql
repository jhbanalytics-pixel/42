-- Feedback rows that may be weekly review labels (core/eval/review.py). The LIKE only narrows the scan; the table
-- also holds app taps and free text, so review.review_labels keeps just the rows whose what is a JSON object with
-- source 42_review_v1, and quality_score keeps the four ISO weeks ending with the scored one.
SELECT fb.who, fb.what, fb.reason
FROM `ogilvy-trends-v2.intelligence_42_agent.feedback` AS fb
WHERE fb.what LIKE '%42_review_v1%'
