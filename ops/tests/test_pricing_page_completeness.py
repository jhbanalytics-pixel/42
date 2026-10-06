import pytest

from ops.deploy.refresh_question_policy import Refused, _require_whole_page


def _document(trailer=b""):
    return b"<!doctype html><html><body><main>pricing</main></body></html>" + trailer


def _complete_trailer():
    return (
        b"<script>window.pageLabel='</html>';</script>"
        b"<div></div>"
        b"<footer><section><nav><ul><li><a href='/pricing'>pricing</a></li>"
        b"</ul></nav></section></footer>"
    )


def test_complete_closed_script_and_footer_trailer_is_a_whole_page():
    _require_whole_page(_document(_complete_trailer()))


def test_legacy_plain_html_document_still_passes_without_body_tags():
    _require_whole_page(b"<html>plain complete document</html>")


@pytest.mark.parametrize(
    "raw",
    [
        b"<html><body>cut off before either close",
        b"<html><body>body close is missing</html>",
        b"<html><body><script>const marker='</html>",
        b"<html><body>content</body><!-- marker </html>",
        _document(b"<script>cut off before script close"),
        _document(b"<script>complete</script><div></div><footer><section>cut footer"),
        _document(_complete_trailer() + b"<script>unexpected extra payload</script>"),
        _document(
            b"<script>complete</script><div><span>not empty</span></div>"
            b"<footer><section>content</section></footer>"
        ),
        _document(
            b"<script>complete</script><div></div><footer><section></footer></section>"
        ),
    ],
)
def test_missing_closures_false_markers_and_unexpected_trailers_refuse(raw):
    with pytest.raises(Refused, match="^unattended_source_truncated$"):
        _require_whole_page(raw)


def test_self_closing_html_tag_is_not_a_document_close():
    with pytest.raises(Refused, match="^unattended_source_truncated$"):
        _require_whole_page(b"<html/>")


def test_self_closing_body_tag_is_not_a_body_close():
    with pytest.raises(Refused, match="^unattended_source_truncated$"):
        _require_whole_page(b"<html><body/></html>")


@pytest.mark.parametrize(
    "suffix",
    [
        b"<scr",
        b"<!--",
        b"<script>",
        b"<script>script body cut",
        b"<script>script body</script>",
        b"<script>script body</script><div>",
        b"<script>script body</script><div></div>",
        b"<script>script body</script><div></div><footer>",
        b"<script>script body</script><div></div><footer><section>cut",
        b"<script>script body</script><div></div><footer><section>done</section></footer><scr",
    ],
)
def test_partial_trailer_at_each_required_boundary_refuses(suffix):
    with pytest.raises(Refused, match="^unattended_source_truncated$"):
        _require_whole_page(_document(suffix))


@pytest.mark.parametrize("suffix", [b"<?unexpected?>", b"<![CDATA[unexpected]]>"])
def test_post_document_processing_instructions_and_declarations_refuse(suffix):
    with pytest.raises(Refused, match="^unattended_source_truncated$"):
        _require_whole_page(_document(suffix))


@pytest.mark.parametrize(
    "trailer",
    [
        b"<script> \n\t</script><div></div><footer><section>pricing</section></footer>",
        b"<script>complete</script><div></div><footer><section><span></span></section></footer>",
    ],
)
def test_closed_trailer_with_empty_script_or_footer_payload_refuses(trailer):
    with pytest.raises(Refused, match="^unattended_source_truncated$"):
        _require_whole_page(_document(trailer))
