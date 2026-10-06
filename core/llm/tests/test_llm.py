from types import SimpleNamespace as NS

import pytest

from core.llm import provider
from core.llm.gemini import GeminiModel, prepare_schema, usage_of


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("MODEL_PROVIDER", "GEMINI_MODEL", "GEMINI_FAST_MODEL", "GEMINI_PRICE_INPUT_PER_M",
              "GEMINI_PRICE_OUTPUT_PER_M", "GEMINI_THINKING_LEVEL"):
        monkeypatch.delenv(k, raising=False)


def priced(monkeypatch, i="0.5", o="3"):
    monkeypatch.setenv("GEMINI_PRICE_INPUT_PER_M", i)
    monkeypatch.setenv("GEMINI_PRICE_OUTPUT_PER_M", o)


def test_gemini_is_the_only_provider_and_the_default():
    assert provider.provider() == "gemini" and provider.PROVIDERS == ("gemini",)
    assert provider.default_model("smart") == provider.default_model("fast") == "gemini-3.8-flash"


def test_gemini_ids_come_from_one_setting_and_fast_can_differ(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    assert provider.default_model("smart") == provider.default_model("fast") == "gemini-3.8-flash"
    monkeypatch.setenv("GEMINI_MODEL", "gemini-x")
    monkeypatch.setenv("GEMINI_FAST_MODEL", "gemini-y")
    assert (provider.default_model("smart"), provider.default_model("fast")) == ("gemini-x", "gemini-y")


def test_a_bad_provider_is_refused(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    with pytest.raises(ValueError):
        provider.provider()


def test_gemini_3_8_flash_is_priced_at_list_and_other_ids_fail_closed_until_set(monkeypatch):
    assert provider.price_for("gemini-3.8-flash") == {"input": 1.5, "output": 7.5, "cached": 0.15}
    with pytest.raises(provider.PriceUnset):
        provider.price_for("gemini-other")
    priced(monkeypatch)
    assert provider.price_for("gemini-other")["output"] == 3.0
    assert provider.price_for("gemini-3.8-flash")["input"] == 0.5  # the environment overrides the list price
    with pytest.raises(provider.PriceUnset):
        provider.price_for("mystery")


def test_prepare_schema_moves_pattern_into_description_without_touching_the_original():
    schema = {"type": "object", "properties": {"id": {"type": "string", "pattern": "^c[0-9]+$"}, "n": {"type": "integer"}}}
    out = prepare_schema(schema)
    assert "pattern" not in out["properties"]["id"] and "^c[0-9]+$" in out["properties"]["id"]["description"]
    assert schema["properties"]["id"]["pattern"] == "^c[0-9]+$"


class FakeClient:
    def __init__(self, response=None, error=None):
        self.calls = []
        self.models = NS(generate_content=self.generate)
        self.response, self.error = response, error

    def generate(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return self.response


def response(text='{"ok": true}', finish="STOP", pin=100, pout=20, think=30):
    return NS(text=text, candidates=[NS(finish_reason=NS(name=finish))],
              usage_metadata=NS(prompt_token_count=pin, response_token_count=pout, thoughts_token_count=think,
                                tool_use_prompt_token_count=0, candidates_token_count=None))


def test_complete_json_returns_parsed_json_and_bills_thinking_as_output(monkeypatch):
    priced(monkeypatch, "1", "10")
    client = FakeClient(response())
    out, usage = GeminiModel(client=client).complete_json(system="s", user="u", schema={"type": "object"},
                                                          model="gemini-3.8-flash", max_tokens=400)
    assert out == {"ok": True}
    assert usage["input_tokens"] == 100 and usage["output_tokens"] == 50
    assert usage["usd"] == pytest.approx((100 * 1 + 50 * 10) / 1e6)
    sent = client.calls[0]
    assert sent["model"] == "gemini-3.8-flash" and sent["contents"] == "u"
    assert sent["config"].system_instruction == "s" and sent["config"].max_output_tokens == 400 + 2000  # thinking headroom
    assert sent["config"].response_mime_type == "application/json"
    assert sent["config"].http_options is None


def test_complete_json_can_bound_one_request_without_changing_model_defaults(monkeypatch):
    priced(monkeypatch)
    client = FakeClient(response())
    model = GeminiModel(client=client, retries=5)
    kwargs = {"system": "s", "user": "u", "schema": {"type": "object"}, "model": "gemini-3.8-flash",
              "max_tokens": 400}

    model.complete_json(**kwargs, request_timeout_s=1.2345)
    options = client.calls[0]["config"].http_options
    assert options.timeout == 1234
    assert options.retry_options.attempts == 1
    assert model.retries == 5

    model.complete_json(**kwargs)
    assert client.calls[1]["config"].http_options is None


def test_truncated_output_raises_carrying_the_spend(monkeypatch):
    priced(monkeypatch)
    with pytest.raises(RuntimeError, match="truncated") as e:
        GeminiModel(client=FakeClient(response(finish="MAX_TOKENS"))).complete_json(
            system="s", user="u", schema={}, model="gemini-3.8-flash", max_tokens=10)
    assert e.value.usage["output_tokens"] == 50 and e.value.usd == e.value.usage["usd"] > 0


def test_empty_or_bad_json_raises_carrying_the_spend(monkeypatch):
    priced(monkeypatch)
    for text in ("", "not json"):
        with pytest.raises(Exception) as e:
            GeminiModel(client=FakeClient(response(text=text))).complete_json(
                system="s", user="u", schema={}, model="gemini-3.8-flash", max_tokens=10)
        assert e.value.usd > 0


def test_unpriced_model_fails_before_any_call():
    client = FakeClient(response())
    with pytest.raises(provider.PriceUnset):
        GeminiModel(client=client).complete_json(system="s", user="u", schema={}, model="gemini-other", max_tokens=10)
    assert client.calls == []


def test_make_model_gives_gemini_with_the_project(monkeypatch):
    assert isinstance(provider.make_model(), GeminiModel)
    model = provider.make_model(project="p")
    assert isinstance(model, GeminiModel) and model.project == "p"
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    assert isinstance(provider.make_model(), GeminiModel)
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    with pytest.raises(ValueError):
        provider.make_model()


def test_make_model_forwards_a_timeout(monkeypatch):
    from google import genai

    built = []
    monkeypatch.setattr(genai, "Client", lambda **kw: built.append(kw) or NS(models=None))

    model = provider.make_model(project="p", timeout_s=90)
    assert isinstance(model, GeminiModel) and model.timeout_s == 90 and model.project == "p"
    model.client
    assert built[0]["http_options"].timeout == 90_000
    assert provider.make_model().timeout_s is None


def test_a_rate_limit_from_gemini_trips_the_brief_breaker():
    pytest.importorskip("core.brief.job")
    from core.brief.job import _rate_limited
    err = Exception("429 RESOURCE_EXHAUSTED. Quota exceeded")
    assert _rate_limited(err)




def test_cached_tokens_bill_at_the_cached_rate():
    meta = NS(prompt_token_count=1000, cached_content_token_count=400, response_token_count=100,
              thoughts_token_count=0, tool_use_prompt_token_count=0, candidates_token_count=None)
    usage = usage_of(NS(usage_metadata=meta), "gemini-3.8-flash")
    assert usage["usd"] == pytest.approx((600 * 1.5 + 400 * 0.15 + 100 * 7.5) / 1e6)


def test_output_that_breaks_type_or_enum_fails_but_the_limits_the_callers_enforce_do_not():
    schema = {"type": "object", "required": ["v"], "properties": {"v": {"type": "string", "enum": ["a", "b"]},
                                                                    "id": {"type": "string", "pattern": "^c[0-9]$"},
                                                                    "xs": {"type": "array", "maxItems": 1}}}
    client = FakeClient(response(text='{"v": "a", "id": "zz", "xs": [1, 2]}'))
    out, _ = GeminiModel(client=client).complete_json(system="s", user="u", schema=schema, model="gemini-3.8-flash",
                                                       max_tokens=10)
    assert out["id"] == "zz"
    with pytest.raises(Exception) as e:
        GeminiModel(client=FakeClient(response(text='{"v": "z"}'))).complete_json(
            system="s", user="u", schema=schema, model="gemini-3.8-flash", max_tokens=10)
    assert e.value.usd > 0


def test_a_prohibited_reply_raises():
    with pytest.raises(RuntimeError, match="blocked"):
        GeminiModel(client=FakeClient(response(text="", finish="PROHIBITED_CONTENT"))).complete_json(
            system="s", user="u", schema={}, model="gemini-3.8-flash", max_tokens=10)


def test_minimal_thinking_is_refused(monkeypatch):
    monkeypatch.setenv("GEMINI_THINKING_LEVEL", "minimal")
    with pytest.raises(ValueError):
        GeminiModel(client=FakeClient(response())).complete_json(system="s", user="u", schema={}, model="gemini-3.8-flash",
                                                                 max_tokens=10)


# Media parts (video reading, BUILD.md 2.6)


def test_a_text_only_call_sends_the_same_contents_and_config_with_or_without_the_media_argument(monkeypatch):
    priced(monkeypatch)
    plain, empty = FakeClient(response()), FakeClient(response())
    kwargs = {"system": "s", "user": "u", "schema": {"type": "object"}, "model": "gemini-3.8-flash", "max_tokens": 400}
    GeminiModel(client=plain).complete_json(**kwargs)
    GeminiModel(client=empty).complete_json(**kwargs, media=None)
    assert plain.calls[0]["contents"] == empty.calls[0]["contents"] == "u"
    assert plain.calls[0]["config"] == empty.calls[0]["config"]
    GeminiModel(client=empty).complete_json(**kwargs, media=[])
    assert empty.calls[1]["contents"] == "u"


def test_media_parts_go_before_the_text_as_uri_and_inline_bytes(monkeypatch):
    priced(monkeypatch)
    client = FakeClient(response())
    out, usage = GeminiModel(client=client).complete_json(
        system="s", user="what is shown", schema={"type": "object"}, model="gemini-3.8-flash", max_tokens=400,
        media=[{"uri": "https://www.youtube.com/watch?v=abc", "mime_type": "video/mp4"},
               {"bytes": b"\xff\xd8jpeg", "mime_type": "image/jpeg"}])
    assert out == {"ok": True} and usage["input_tokens"] == 100
    video, image, text = client.calls[0]["contents"]
    assert (video.file_data.file_uri, video.file_data.mime_type) == ("https://www.youtube.com/watch?v=abc", "video/mp4")
    assert (image.inline_data.data, image.inline_data.mime_type) == (b"\xff\xd8jpeg", "image/jpeg")
    assert text == "what is shown"


@pytest.mark.parametrize("part", [{"uri": "https://x/v.mp4"}, {"mime_type": "video/mp4"},
                                  {"uri": "ftp://x/v.mp4", "mime_type": "video/mp4"},
                                  {"bytes": "not bytes", "mime_type": "image/jpeg"}, "a string"])
def test_a_malformed_media_part_fails_before_any_call(monkeypatch, part):
    priced(monkeypatch)
    client = FakeClient(response())
    with pytest.raises(ValueError):
        GeminiModel(client=client).complete_json(system="s", user="u", schema={"type": "object"},
                                                 model="gemini-3.8-flash", max_tokens=400, media=[part])
    assert client.calls == []


def test_the_video_role_uses_the_fast_model(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    assert provider.default_model("video") == provider.default_model("fast") == "gemini-3.8-flash"
    monkeypatch.setenv("GEMINI_FAST_MODEL", "gemini-y")
    assert provider.default_model("video") == "gemini-y"


def test_a_linked_clip_can_be_read_up_to_an_end_offset(monkeypatch):
    priced(monkeypatch)
    client = FakeClient(response())
    GeminiModel(client=client).complete_json(
        system="s", user="u", schema={"type": "object"}, model="gemini-3.8-flash", max_tokens=400,
        media=[{"uri": "https://www.youtube.com/watch?v=abc", "mime_type": "video/mp4", "end_s": 90}])
    video = client.calls[0]["contents"][0]
    assert video.file_data.file_uri == "https://www.youtube.com/watch?v=abc"
    assert (video.video_metadata.start_offset, video.video_metadata.end_offset) == ("0s", "90s")
    with pytest.raises(ValueError):
        GeminiModel(client=client).complete_json(
            system="s", user="u", schema={"type": "object"}, model="gemini-3.8-flash", max_tokens=400,
            media=[{"uri": "https://www.youtube.com/watch?v=abc", "mime_type": "video/mp4", "end_s": 0}])
