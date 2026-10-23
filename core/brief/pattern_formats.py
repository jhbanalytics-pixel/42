"""The format names that never count as an adopted format: one list, read by every count of adoption.

The format vocabulary is enrichment's (core/understand/enrich.py FORMATS, written to post_enrichment.formats) and the
video step's, the same list (core/understand/video.py FORMATS, the format key of post_enrichment.video_notes):
talking_head, skit, dance, duet, stitch, tutorial, slideshow, meme_image, news_clip and other.

Its readers: the candidate statement's pattern bucket (core/brief/sql/brief.sql pattern_format, through the
SQL_PLACEHOLDER that render fills in when core/brief/job.py and ops/runners/funnel_scoreboard.py read the file), the
shared-pattern line the writer and the critic read and the code's own count behind an inferred why-now
(core/brief/explain.py _shared_patterns, _pattern_line, adopted_pattern), and the writer's and critic's prompts, which
name the list from here.

This file imports nothing, so a runner started from ops/runners can read it as it reads brief.sql.
"""

# No practice at all: other is no format, news_clip points to news and talking_head is someone speaking to camera
# (18 Oct 2026).
GENERIC_FORMATS = ("talking_head", "other", "news_clip")
# Genres: each names a kind of video, not one practice local creators copy from each other, so 3 creators posting one
# on 2 days under a tag is what the tag's genre looks like, not adoption. skit is the comedy and drama sketch, tutorial
# the how-to, slideshow the photo carousel, meme_image the image meme, and duet and stitch the reaction to another
# creator's video (23 Oct 2026: NG "funny, joke" and a serialized drama ranked as adopted on these and the critic cut
# both). dance stays counted: a dance is a set of moves creators learn from each other.
GENRE_FORMATS = ("skit", "tutorial", "slideshow", "meme_image", "duet", "stitch")
PATTERN_SKIP_FORMATS = GENERIC_FORMATS + GENRE_FORMATS

# The text brief.sql holds where its pattern_format count leaves these formats out.
SQL_PLACEHOLDER = "{pattern_skip_formats}"


def sql_list():
    """The list as SQL string literals, for NOT IN ( ... )."""
    return ", ".join(f"'{name}'" for name in PATTERN_SKIP_FORMATS)


def render(sql):
    """sql with the placeholder replaced by the list."""
    return sql.replace(SQL_PLACEHOLDER, sql_list())


def words(last="and"):
    """The list in a sentence: "talking_head, other, ... and stitch", or with "or" before the last name."""
    *head, tail = PATTERN_SKIP_FORMATS
    return f"{', '.join(head)} {last} {tail}"
