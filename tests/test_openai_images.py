import base64

import pytest
from fastapi import HTTPException

from core.models.openai_images import (
    OpenAIImageRequestError,
    build_native_gpt_image_options,
    encode_image_response_item,
    gpt_image_model_id_from_size,
    image_generation_batch_sizes,
    normalize_openai_gemini_model_id,
    parse_openai_gemini_size,
    parse_requested_size,
    parse_response_format,
)
from core.models.payloads import build_image_payload_candidates, random_image_seed
from core.models.image_limits import (
    MAX_TOTAL_IMAGE_BYTES,
    ImageInputLimitError,
    add_input_image_bytes,
    validate_input_image_count,
)
from core.models.resolver import resolve_model, resolve_ratio_and_resolution
from core.adobe_client import AdobeClient, ContentPolicyError


def test_native_gpt_image_2_request_uses_requested_size():
    options = build_native_gpt_image_options(
        {
            "model": "gpt-image-2",
            "prompt": "draw a dashboard",
            "size": "1536x1024",
            "quality": "high",
        }
    )

    assert options.response_model == "gpt-image-2"
    assert options.response_format == "url"
    assert options.aspect_ratio == "3:2"
    assert options.requested_size == {"width": 1536, "height": 1024}
    assert options.upstream_model_id == "gpt-image"
    assert options.upstream_model_version == "2"

    payload = build_image_payload_candidates(
        prompt="draw a dashboard",
        aspect_ratio=options.aspect_ratio,
        output_resolution=options.output_resolution,
        upstream_model_id=options.upstream_model_id or "",
        upstream_model_version=options.upstream_model_version or "",
        quality_level="low",
        requested_size=options.requested_size,
    )[0]

    assert payload["modelId"] == "gpt-image"
    assert payload["modelVersion"] == "2"
    assert payload["size"] == {"width": 1536, "height": 1024}
    assert payload["modelSpecificPayload"]["size"] == "1536x1024"


def test_native_gpt_image_size_can_map_to_internal_model_alias():
    assert (
        gpt_image_model_id_from_size({"width": 2560, "height": 1440})
        == "gpt-image-2k-16x9"
    )


def test_native_gpt_image_accepts_16x9_ratio_size_as_2k():
    options = build_native_gpt_image_options(
        {
            "model": "gpt-image-2",
            "prompt": "draw a dashboard",
            "size": "16:9",
        }
    )

    assert options.aspect_ratio == "16:9"
    assert options.output_resolution == "2K"
    assert options.requested_size == {"width": 2560, "height": 1440}
    assert options.resolved_model_id == "gpt-image-2k-16x9"

    payload = build_image_payload_candidates(
        prompt="draw a dashboard",
        aspect_ratio=options.aspect_ratio,
        output_resolution=options.output_resolution,
        upstream_model_id=options.upstream_model_id or "",
        upstream_model_version=options.upstream_model_version or "",
        quality_level="low",
        requested_size=options.requested_size,
    )[0]

    assert payload["size"] == {"width": 2560, "height": 1440}
    assert payload["modelSpecificPayload"]["size"] == "2560x1440"


def test_custom_gpt_image_alias_can_keep_requested_model_id_and_quality():
    options = build_native_gpt_image_options(
        {
            "model": "ignored-custom-id",
            "prompt": "draw a dashboard",
            "size": "1024x1024",
        },
        model_id_override="gpt-image-2",
        response_model="gpt-image-2-high",
    )

    payload = build_image_payload_candidates(
        prompt="draw a dashboard",
        aspect_ratio=options.aspect_ratio,
        output_resolution=options.output_resolution,
        upstream_model_id=options.upstream_model_id or "",
        upstream_model_version=options.upstream_model_version or "",
        quality_level="high",
        requested_size=options.requested_size,
    )[0]

    assert options.response_model == "gpt-image-2-high"
    assert payload["modelId"] == "gpt-image"
    assert payload["modelVersion"] == "2"
    assert payload["generationSettings"]["detailLevel"] == 5


def test_gpt_image_rejects_sizes_above_upstream_limits():
    try:
        parse_requested_size("4096x4096")
    except OpenAIImageRequestError as exc:
        assert exc.param == "size"
        assert "longest edge" in str(exc)
    else:
        raise AssertionError("expected 4096x4096 to be rejected")

    try:
        parse_requested_size("3840x3840")
    except OpenAIImageRequestError as exc:
        assert exc.param == "size"
        assert "total pixels" in str(exc)
    else:
        raise AssertionError("expected 3840x3840 to be rejected")

    assert parse_requested_size("3840x2160") == {"width": 3840, "height": 2160}
    assert parse_requested_size("2880x2880") == {"width": 2880, "height": 2880}


def test_b64_json_response_item_matches_openai_images_shape():
    item = encode_image_response_item(
        b"fake-image-bytes",
        image_url="http://127.0.0.1/generated/image.png",
        response_format="b64_json",
        output_format="png",
        output_compression=None,
    )

    assert "url" not in item
    assert base64.b64decode(item["b64_json"].encode("ascii")) == b"fake-image-bytes"


def test_base64_response_format_alias_maps_to_b64_json():
    assert parse_response_format("base64", force_b64_json=False) == "b64_json"
    assert parse_response_format("b64_json", force_b64_json=False) == "b64_json"
    assert parse_response_format(None, force_b64_json=False) == "url"


def test_image_generation_batch_sizes_limit_each_worker_to_two_images():
    assert image_generation_batch_sizes(1) == [1]
    assert image_generation_batch_sizes(2) == [2]
    assert image_generation_batch_sizes(3) == [1, 2]
    assert image_generation_batch_sizes(4) == [2, 2]
    assert image_generation_batch_sizes(5) == [1, 2, 2]


def test_reference_image_limits_allow_sixteen_and_200mb():
    validate_input_image_count(16)
    assert add_input_image_bytes(MAX_TOTAL_IMAGE_BYTES - 1, 1) == (
        MAX_TOTAL_IMAGE_BYTES
    )

    with pytest.raises(ImageInputLimitError, match="at most 16"):
        validate_input_image_count(17)
    with pytest.raises(ImageInputLimitError, match="max 200MB"):
        add_input_image_bytes(MAX_TOTAL_IMAGE_BYTES, 1)


def test_gpt_image_seed_is_randomized():
    generated_seeds = {random_image_seed() for _ in range(20)}
    assert all(0 <= value <= 999999 for value in generated_seeds)
    assert len(generated_seeds) > 1


def test_gpt_image_unsafe_retries_with_new_seeds(monkeypatch):
    client = AdobeClient()
    attempted_seeds = []

    def fake_generate_once(**kwargs):
        attempted_seeds.append(kwargs["seed"])
        if len(attempted_seeds) < 3:
            raise ContentPolicyError(
                "unsafe",
                upstream_code="image_unsafe",
            )
        return b"image", {"status": "succeeded"}

    seed_values = iter([101, 202, 303])
    monkeypatch.setattr(client, "_generate_once", fake_generate_once)
    monkeypatch.setattr(
        "core.adobe_client.random_image_seed",
        lambda: next(seed_values),
    )

    image_bytes, meta = client.generate(
        token="TOKEN",
        prompt="a blue crystal cube",
        upstream_model_id="gpt-image",
        upstream_model_version="2",
    )

    assert image_bytes == b"image"
    assert meta["status"] == "succeeded"
    assert attempted_seeds == [101, 202, 303]


def test_content_policy_error_keeps_plain_http_detail(monkeypatch):
    import app

    class TokenManagerStub:
        def get_available(self, strategy=None):
            return "TOKEN"

        def get_meta_by_value(self, token):
            return {}

    class ClientStub:
        retry_enabled = False
        retry_max_attempts = 1
        token_rotation_strategy = "round_robin"

    class RequestState:
        log_id = "LOG_ID"

    class RequestStub:
        method = "POST"
        url = type("Url", (), {"path": "/v1/images/generations"})()
        state = RequestState()

    monkeypatch.setattr(app, "token_manager", TokenManagerStub())
    monkeypatch.setattr(app, "client", ClientStub())
    monkeypatch.setattr(app, "_append_attempt_log", lambda **kwargs: None)

    def raise_content_policy(_token):
        raise ContentPolicyError(
            "生成的图片可能不安全，请修改提示词或更换随机种子后重试。",
            upstream_code="image_unsafe",
        )

    with pytest.raises(HTTPException) as error_info:
        app._run_with_token_retries(
            request=RequestStub(),
            operation_name="images.generations",
            run_once=raise_content_policy,
            set_request_error_detail=lambda *args, **kwargs: "ERR-CODE",
        )

    assert error_info.value.status_code == 400
    assert error_info.value.detail == (
        "生成的图片可能不安全，请修改提示词或更换随机种子后重试。"
    )
    assert isinstance(error_info.value.detail, str)


def test_openai_prefixed_gemini_model_is_normalized():
    assert normalize_openai_gemini_model_id(
        "gpt-image-gemini-3.1-flash-image"
    ) == "gemini-3.1-flash-image"
    assert normalize_openai_gemini_model_id(
        "gpt-image-gemini-3-pro-image"
    ) == "gemini-3-pro-image"
    assert normalize_openai_gemini_model_id("gpt-image-2") is None


def test_openai_sizes_map_to_gemini_ratio_and_resolution():
    assert parse_openai_gemini_size("1024x1024") == ("1:1", "1K")
    assert parse_openai_gemini_size("1536x1024") == ("3:2", "2K")
    assert parse_openai_gemini_size("1024x1536") == ("2:3", "2K")
    assert parse_openai_gemini_size("1792x1024") == ("16:9", "2K")
    assert parse_openai_gemini_size("1024x1792") == ("9:16", "2K")
    assert parse_openai_gemini_size("4096x4096") == ("1:1", "4K")


def test_openai_prefixed_gemini_size_reaches_gemini_payload():
    model_id = "gpt-image-gemini-3.1-flash-image"
    ratio, resolution, response_model = resolve_ratio_and_resolution(
        {"size": "1536x1024"},
        model_id,
    )
    model_conf = resolve_model(response_model)

    assert (ratio, resolution, response_model) == ("3:2", "2K", model_id)
    assert model_conf["upstream_model_id"] == "gemini-flash"
    assert model_conf["upstream_model_version"] == "nano-banana-3"

    payload = build_image_payload_candidates(
        prompt="draw a dashboard",
        aspect_ratio=ratio,
        output_resolution=resolution,
        upstream_model_id=model_conf["upstream_model_id"],
        upstream_model_version=model_conf["upstream_model_version"],
    )[0]

    assert payload["modelId"] == "gemini-flash"
    assert payload["modelVersion"] == "nano-banana-3"
    assert payload["modelSpecificPayload"]["aspectRatio"] == "3:2"
    assert payload["modelSpecificPayload"]["imageSize"] == "2K"
    assert payload["size"] == {"width": 2496, "height": 1664}
