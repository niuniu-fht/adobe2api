import base64

import pytest

from core.models.openai_images import (
    MIN_GPT_IMAGE_PIXELS,
    build_native_gpt_image_options,
    encode_image_response_item,
    gpt_image_model_id_from_size,
    image_generation_batch_sizes,
    normalize_openai_gemini_model_id,
    parse_openai_gemini_size,
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
from core.adobe_client import AdobeClient, AdobeRequestError, ContentPolicyError


def test_input_image_format_is_converted_to_png():
    import app
    from PIL import Image
    from io import BytesIO

    source = Image.new("RGBA", (2, 2), (255, 0, 0, 128))
    encoded = BytesIO()
    source.save(encoded, format="PNG")

    converted, mime_type = app._normalize_input_image(encoded.getvalue(), "image/avif")

    assert mime_type == "image/png"
    with Image.open(BytesIO(converted)) as result:
        assert result.format == "PNG"


@pytest.mark.parametrize("source_mode", ["CMYK", "L", "P"])
def test_input_image_unsupported_mode_is_converted_to_rgb_png(source_mode):
    import app
    from PIL import Image
    from io import BytesIO

    source = Image.new(source_mode, (2, 2))
    encoded = BytesIO()
    source_format = "JPEG" if source_mode == "CMYK" else "PNG"
    source.save(encoded, format=source_format)

    converted, mime_type = app._normalize_input_image(
        encoded.getvalue(),
        "image/jpeg" if source_format == "JPEG" else "image/png",
    )

    assert mime_type == "image/png"
    with Image.open(BytesIO(converted)) as result:
        assert result.format == "PNG"
        assert result.mode == "RGB"


def test_input_image_format_rejects_invalid_bytes():
    import app

    with pytest.raises(app.HTTPException, match="unsupported or invalid image format"):
        app._normalize_input_image(b"not an image", "image/jpeg")


def test_native_gpt_image_2_request_converts_requested_size():
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
    assert options.output_resolution == "1K"
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


def test_native_gpt_image_15_uses_version_15_and_supported_1k_size():
    options = build_native_gpt_image_options(
        {
            "model": "gpt-image-1.5",
            "prompt": "draw a dashboard",
            "size": "1536x1024",
        }
    )

    assert options.response_model == "gpt-image-1.5"
    assert options.aspect_ratio == "3:2"
    assert options.output_resolution == "1K"
    assert options.requested_size == {"width": 1536, "height": 1024}
    assert options.upstream_model_id == "gpt-image"
    assert options.upstream_model_version == "1.5"

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
    assert payload["modelVersion"] == "1.5"
    assert payload["size"] == {"width": 1536, "height": 1024}


@pytest.mark.parametrize(
    ("requested", "adapted", "ratio"),
    [
        (None, {"width": 1024, "height": 1024}, "1:1"),
        ("auto", {"width": 1024, "height": 1024}, "1:1"),
        ("1024x1024", {"width": 1024, "height": 1024}, "1:1"),
        ("1024x1536", {"width": 1024, "height": 1536}, "2:3"),
        ("9:16", {"width": 1024, "height": 1536}, "2:3"),
        ("1264x848", {"width": 1536, "height": 1024}, "3:2"),
        ("16:9", {"width": 1536, "height": 1024}, "3:2"),
    ],
)
def test_native_gpt_image_15_maps_unsupported_sizes_before_upstream(
    requested, adapted, ratio
):
    data = {"model": "gpt-image-1.5", "prompt": "draw"}
    if requested is not None:
        data["size"] = requested

    options = build_native_gpt_image_options(data)

    assert options.upstream_model_version == "1.5"
    assert options.output_resolution == "1K"
    assert options.aspect_ratio == ratio
    assert options.requested_size == adapted


@pytest.mark.parametrize(
    ("requested", "adapted"),
    [
        ("256x256", {"width": 816, "height": 816}),
        ("320x160", {"width": 1152, "height": 576}),
    ],
)
def test_gpt_image_upscales_below_upstream_pixel_minimum(requested, adapted):
    options = build_native_gpt_image_options(
        {"model": "gpt-image-2", "prompt": "draw", "size": requested}
    )

    assert options.requested_size == adapted
    assert adapted["width"] % 16 == 0
    assert adapted["height"] % 16 == 0
    assert adapted["width"] * adapted["height"] >= MIN_GPT_IMAGE_PIXELS

    payload = build_image_payload_candidates(
        prompt="draw",
        aspect_ratio=options.aspect_ratio,
        output_resolution=options.output_resolution,
        upstream_model_id="gpt-image",
        upstream_model_version="2",
        requested_size=options.requested_size,
    )[0]
    assert payload["size"] == adapted
    assert payload["modelSpecificPayload"]["size"] == (
        f'{adapted["width"]}x{adapted["height"]}'
    )


@pytest.mark.parametrize("raw_size", [None, "", "auto", "not-a-size"])
def test_native_gpt_image_forwards_auto_without_default_dimensions(raw_size):
    data = {"model": "gpt-image-2", "prompt": "draw freely"}
    if raw_size is not None:
        data["size"] = raw_size

    options = build_native_gpt_image_options(data)
    payload = build_image_payload_candidates(
        prompt="draw freely",
        aspect_ratio=options.aspect_ratio,
        output_resolution=options.output_resolution,
        upstream_model_id=options.upstream_model_id or "",
        upstream_model_version=options.upstream_model_version or "",
        requested_size=options.requested_size,
    )[0]

    assert options.aspect_ratio == "auto"
    assert options.output_resolution == "auto"
    assert options.requested_size is None
    assert options.resolved_model_id is None
    assert payload["modelSpecificPayload"]["size"] == "auto"
    assert "size" not in payload
    assert "outputResolution" not in payload


def test_custom_gpt_image_alias_forwards_auto_size():
    options = build_native_gpt_image_options(
        {"model": "gpt-image-2-high", "prompt": "draw freely", "size": "auto"},
        model_id_override="gpt-image-2",
        response_model="gpt-image-2-high",
    )
    payload = build_image_payload_candidates(
        prompt="draw freely",
        aspect_ratio=options.aspect_ratio,
        output_resolution=options.output_resolution,
        upstream_model_id=options.upstream_model_id or "",
        upstream_model_version=options.upstream_model_version or "",
        requested_size=options.requested_size,
    )[0]

    assert options.response_model == "gpt-image-2-high"
    assert payload["modelSpecificPayload"]["size"] == "auto"
    assert "size" not in payload
    assert "outputResolution" not in payload


def test_native_gpt_image_size_can_map_to_internal_model_alias():
    assert (
        gpt_image_model_id_from_size({"width": 2560, "height": 1440})
        == "firefly-gpt-image-2k-16x9"
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
    assert options.resolved_model_id == "firefly-gpt-image-2k-16x9"

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


def test_builtin_gpt_image_quality_aliases_default_to_high():
    client = AdobeClient()
    client.apply_config(
        {
            "gpt_image_quality": "low",
            "gpt_image_model_qualities": {},
        }
    )

    for model_id in ("gpt-image-2-high", "gpt-image-2-higher"):
        assert client.is_gpt_image_model_alias(model_id)
        assert client.get_gpt_image_quality(model_id) == "high"

        options = build_native_gpt_image_options(
            {"model": model_id, "prompt": "draw a dashboard", "size": "auto"},
            model_id_override="gpt-image-2",
            response_model=model_id,
        )
        payload = build_image_payload_candidates(
            prompt="draw a dashboard",
            aspect_ratio=options.aspect_ratio,
            output_resolution=options.output_resolution,
            upstream_model_id=options.upstream_model_id or "",
            upstream_model_version=options.upstream_model_version or "",
            quality_level=client.get_gpt_image_quality(model_id),
            requested_size=options.requested_size,
        )[0]

        assert options.response_model == model_id
        assert payload["generationSettings"]["detailLevel"] == 5


def test_clarity_alias_enables_transparent_png_postprocess():
    client = AdobeClient()
    client.apply_config({"gpt_image_quality": "low", "gpt_image_model_qualities": {}})

    assert client.is_gpt_image_model_alias("gpt-image-2-clarity")
    assert client.is_gpt_image_model_alias("gpt-image-2-**clarity")
    assert client.is_gpt_image_model_alias("gpt-image-2-clarity-free")
    assert client.get_gpt_image_quality("gpt-image-2-clarity") == "low"

    options = build_native_gpt_image_options(
        {
            "model": "gpt-image-2-clarity",
            "prompt": "draw a sticker",
            "size": "auto",
            "output_format": "jpeg",
        },
        model_id_override="gpt-image-2",
        response_model="gpt-image-2-clarity",
    )

    assert options.response_model == "gpt-image-2-clarity"
    assert options.output_format == "png"
    assert options.transparent_background is True
    assert options.direct_transparent_mask is False

    free_options = build_native_gpt_image_options(
        {
            "model": "gpt-image-2-clarity-free",
            "prompt": "remove background",
            "size": "1024x1024",
            "output_format": "jpeg",
        },
        model_id_override="gpt-image-2",
        response_model="gpt-image-2-clarity-free",
    )

    assert free_options.response_model == "gpt-image-2-clarity-free"
    assert free_options.output_format == "png"
    assert free_options.transparent_background is True
    assert free_options.direct_transparent_mask is True


def test_apply_mask_alpha_creates_transparent_png():
    from io import BytesIO
    from PIL import Image

    image_io = BytesIO()
    Image.new("RGB", (2, 1), (200, 40, 20)).save(image_io, format="PNG")
    mask_io = BytesIO()
    mask = Image.new("L", (2, 1))
    mask.putdata([255, 0])
    mask.save(mask_io, format="PNG")

    output = AdobeClient.apply_mask_alpha(image_io.getvalue(), mask_io.getvalue())

    with Image.open(BytesIO(output)) as result:
        assert result.mode == "RGBA"
        assert result.getpixel((0, 0))[3] == 255
        assert result.getpixel((1, 0))[3] == 0


def test_higher_alias_quality_can_be_configured_independently():
    client = AdobeClient()
    client.apply_config(
        {
            "gpt_image_quality": "low",
            "gpt_image_model_qualities": {
                "gpt-image-2-high": "medium",
                "gpt-image-2-higher": "high",
            },
        }
    )

    assert client.get_gpt_image_quality("gpt-image-2") == "low"
    assert client.get_gpt_image_quality("gpt-image-2-high") == "medium"
    assert client.get_gpt_image_quality("gpt-image-2-higher") == "high"


def test_gpt_image_converts_large_size_to_ratio_and_resolution():
    options = build_native_gpt_image_options(
        {
            "model": "gpt-image-2",
            "prompt": "draw a poster",
            "size": "3072x4096",
        }
    )

    assert options.aspect_ratio == "3:4"
    assert options.output_resolution == "4K"
    assert options.requested_size == {"width": 2496, "height": 3312}
    assert options.resolved_model_id == "firefly-gpt-image-4k-3x4"

    payload = build_image_payload_candidates(
        prompt="draw a poster",
        aspect_ratio=options.aspect_ratio,
        output_resolution=options.output_resolution,
        upstream_model_id=options.upstream_model_id or "",
        upstream_model_version=options.upstream_model_version or "",
        requested_size=options.requested_size,
    )[0]

    assert payload["outputResolution"] == "4K"
    assert payload["size"] == {"width": 2496, "height": 3312}
    assert payload["modelSpecificPayload"]["size"] == "2496x3312"


def test_gpt_image_caps_square_at_upstream_pixel_limit():
    options = build_native_gpt_image_options(
        {
            "model": "gpt-image-2-high",
            "prompt": "draw a square",
            "size": "10000x10000",
        },
        model_id_override="gpt-image-2",
        response_model="gpt-image-2-high",
    )

    assert options.aspect_ratio == "1:1"
    assert options.output_resolution == "4K"
    assert options.requested_size == {"width": 2880, "height": 2880}
    assert options.response_model == "gpt-image-2-high"


def test_gpt_image_normalizes_non_multiple_size_for_adobe():
    options = build_native_gpt_image_options(
        {
            "model": "gpt-image-2",
            "prompt": "draw a portrait",
            "size": "1024x1448",
        }
    )

    assert options.aspect_ratio == "3:4"
    assert options.output_resolution == "1K"
    assert options.requested_size == {"width": 1024, "height": 1440}
    assert options.requested_size["width"] % 16 == 0
    assert options.requested_size["height"] % 16 == 0


def test_gpt_image_scales_oversized_dimensions_while_preserving_ratio():
    options = build_native_gpt_image_options(
        {
            "model": "gpt-image-2",
            "prompt": "draw a large poster",
            "size": "6000x8000",
        }
    )

    assert options.aspect_ratio == "3:4"
    assert options.output_resolution == "4K"
    assert options.requested_size == {"width": 2496, "height": 3312}


def test_gpt_image_caps_longest_edge_at_3840():
    options = build_native_gpt_image_options(
        {
            "model": "gpt-image-2",
            "prompt": "draw a wide banner",
            "size": "5000x1000",
        }
    )

    assert options.requested_size == {"width": 3840, "height": 768}
    assert max(options.requested_size.values()) == 3840


def test_gpt_image_adapts_3840_square_to_upstream_pixel_limit():
    options = build_native_gpt_image_options(
        {
            "model": "gpt-image-2",
            "prompt": "draw a square",
            "size": "3840x3840",
        }
    )

    assert options.aspect_ratio == "1:1"
    assert options.output_resolution == "4K"
    assert options.requested_size == {"width": 2880, "height": 2880}
    assert (
        options.requested_size["width"] * options.requested_size["height"]
        <= 8_294_400
    )


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


def test_gpt_image_references_use_storage_blobs_only():
    payloads = build_image_payload_candidates(
        prompt="edit the reference image",
        aspect_ratio="1:1",
        output_resolution="1K",
        upstream_model_id="gpt-image",
        upstream_model_version="2",
        source_image_ids=["blob-1", "blob-2"],
    )

    assert len(payloads) == 1
    assert payloads[0]["generationMetadata"]["module"] == "text2image"
    assert payloads[0]["referenceBlobs"] == [
        {"id": "blob-1", "usage": "subject"},
        {"id": "blob-2", "usage": "subject"},
    ]
    assert payloads[0]["modelSpecificPayload"] == {}
    assert all("referenceImages" not in payload for payload in payloads)
    assert all("referenceVideos" not in payload for payload in payloads)


def test_gpt_image_unsafe_stops_without_seed_retry(monkeypatch):
    client = AdobeClient()
    attempted_seeds = []

    def fake_generate_once(**kwargs):
        attempted_seeds.append(kwargs["seed"])
        raise ContentPolicyError(
            "unsafe",
            upstream_code="image_unsafe",
        )

    monkeypatch.setattr(client, "_generate_once", fake_generate_once)
    monkeypatch.setattr(
        "core.adobe_client.random_image_seed",
        lambda: 101,
    )

    with pytest.raises(ContentPolicyError, match="图片不安全"):
        client.generate(
            token="TOKEN",
            prompt="a blue crystal cube",
            upstream_model_id="gpt-image",
            upstream_model_version="2",
        )

    assert attempted_seeds == [101]


def test_gpt_image_candidate_fallback_preserves_primary_error(monkeypatch):
    client = AdobeClient()

    class FakeResponse:
        def __init__(self, message):
            self.status_code = 400
            self.text = message
            self.headers = {}

        def json(self):
            return {"error_code": "bad_request", "message": self.text}

    primary_responses = iter(
        [
            FakeResponse("primary general-reference failure"),
            FakeResponse("primary subject-reference failure"),
        ]
    )
    fallback_responses = iter(
        [
            FakeResponse("requests general-reference failure"),
            FakeResponse("requests subject-reference failure"),
        ]
    )
    monkeypatch.setattr(
        client,
        "_build_payload_candidates",
        lambda **kwargs: [{"candidate": "general"}, {"candidate": "subject"}],
    )
    monkeypatch.setattr(
        client, "_post_json", lambda *args, **kwargs: next(primary_responses)
    )
    monkeypatch.setattr(
        client,
        "_post_json_requests_once",
        lambda *args, **kwargs: next(fallback_responses),
    )

    with pytest.raises(
        AdobeRequestError,
        match="requests general-reference failure",
    ):
        client._generate_once(token="TOKEN", prompt="edit the image")


def test_content_policy_error_reaches_images_route_unchanged(monkeypatch):
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

    with pytest.raises(ContentPolicyError) as error_info:
        app._run_with_token_retries(
            request=RequestStub(),
            operation_name="images.generations",
            run_once=raise_content_policy,
            set_request_error_detail=lambda *args, **kwargs: "ERR-CODE",
        )

    assert error_info.value.status_code == 400
    assert error_info.value.user_message == "图片不安全"


@pytest.mark.parametrize("error_kind", ["auth_error", "pool_http_401"])
def test_invalid_token_immediately_switches_to_next_account(monkeypatch, error_kind):
    import app

    class TokenManagerStub:
        def __init__(self):
            self.active = ["TOKEN-A", "TOKEN-B"]
            self.invalid = []
            self.success = []

        def get_available(self, strategy=None):
            return self.active[0] if self.active else None

        def get_meta_by_value(self, token):
            suffix = token.rsplit("-", 1)[-1]
            return {
                "token_id": f"token-{suffix.lower()}",
                "token_account_id": f"account-{suffix.lower()}",
                "token_account_name": f"Account {suffix}",
            }

        def report_invalid(self, token):
            self.invalid.append(token)
            self.active = [value for value in self.active if value != token]

        def report_success(self, token):
            self.success.append(token)

        def handle_auth_failure(self, _token):
            raise AssertionError("invalid token must not retry the current account")

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

    token_manager = TokenManagerStub()
    attempts = []
    invalid_callbacks = []
    monkeypatch.setattr(app, "token_manager", token_manager)
    monkeypatch.setattr(app, "client", ClientStub())
    monkeypatch.setattr(app, "_append_attempt_log", lambda **kwargs: None)
    sleep_calls = []
    monkeypatch.setattr(app.time, "sleep", lambda delay: sleep_calls.append(delay))

    def run_once(token):
        attempts.append(token)
        if token == "TOKEN-A":
            if error_kind == "auth_error":
                raise app.AuthError("Token invalid or expired")
            raise app.HTTPException(
                status_code=401,
                detail="All available tokens are invalid or expired",
            )
        return "ok"

    result = app._run_with_token_retries(
        request=RequestStub(),
        operation_name="images.generations",
        run_once=run_once,
        set_request_error_detail=lambda *args, **kwargs: "ERR-CODE",
        on_token_invalid=invalid_callbacks.append,
    )

    assert result == "ok"
    assert attempts == ["TOKEN-A", "TOKEN-B"]
    assert token_manager.invalid == ["TOKEN-A"]
    assert token_manager.success == ["TOKEN-B"]
    assert invalid_callbacks == ["TOKEN-A"]


def test_unrelated_http_401_remains_terminal(monkeypatch):
    import app

    class TokenManagerStub:
        def get_available(self, strategy=None):
            return "TOKEN-A"

        def get_meta_by_value(self, token):
            return {"token_id": "token-a"}

        def report_invalid(self, _token):
            pytest.fail("unrelated 401 must not invalidate an Adobe token")

    class ClientStub:
        retry_enabled = True
        retry_max_attempts = 3
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

    with pytest.raises(app.HTTPException, match="Invalid API key"):
        app._run_with_token_retries(
            request=RequestStub(),
            operation_name="images.generations",
            run_once=lambda _token: (_ for _ in ()).throw(
                app.HTTPException(status_code=401, detail="Invalid API key")
            ),
            set_request_error_detail=lambda *args, **kwargs: "ERR-CODE",
        )


def test_submit_rate_limit_waits_five_seconds_then_switches_account(monkeypatch):
    import app

    class TokenManagerStub:
        def __init__(self):
            self.active = ["TOKEN-A", "TOKEN-B"]
            self.success = []

        def list_active_ids(self):
            return list(self.active)

        def list_active_account_tokens(self):
            return [
                {
                    "token": token,
                    "account_id": f"account-{token.rsplit('-', 1)[-1].lower()}",
                }
                for token in self.active
            ]

        def get_available(self, strategy=None):
            return self.active[0] if self.active else None

        def get_meta_by_value(self, token):
            suffix = token.rsplit("-", 1)[-1]
            return {
                "token_id": f"token-{suffix.lower()}",
                "token_account_id": f"account-{suffix.lower()}",
            }

        def report_success(self, token):
            self.success.append(token)

        def report_invalid(self, _token):
            pytest.fail("submit 429 must not invalidate the token")

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

    token_manager = TokenManagerStub()
    attempts = []
    unavailable_callbacks = []
    monkeypatch.setattr(app, "token_manager", token_manager)
    monkeypatch.setattr(app, "client", ClientStub())
    monkeypatch.setattr(app, "_append_attempt_log", lambda **kwargs: None)
    sleep_calls = []
    monkeypatch.setattr(app.time, "sleep", lambda delay: sleep_calls.append(delay))

    def run_once(token):
        attempts.append(token)
        if token == "TOKEN-A":
            raise app.SubmitRateLimitedError()
        return "ok"

    result = app._run_with_token_retries(
        request=RequestStub(),
        operation_name="images.generations",
        run_once=run_once,
        set_request_error_detail=lambda *args, **kwargs: "ERR-CODE",
        on_token_unavailable=unavailable_callbacks.append,
    )

    assert result == "ok"
    assert attempts == ["TOKEN-A", "TOKEN-B"]
    assert sleep_calls == [3.0]
    assert unavailable_callbacks == ["TOKEN-A"]
    assert token_manager.success == ["TOKEN-B"]


def test_submit_rate_limit_switches_until_tokens_exhausted_with_delay(monkeypatch):
    import app

    tokens = [f"TOKEN-{index}" for index in range(10)]

    class TokenManagerStub:
        def list_active_ids(self):
            return list(tokens)

        def list_active_account_tokens(self):
            return [
                {"token": token, "account_id": f"account-{index}"}
                for index, token in enumerate(tokens)
            ]

        def get_available(self, strategy=None):
            return tokens[0]

        def get_meta_by_value(self, token):
            index = tokens.index(token)
            return {
                "token_id": f"token-{index}",
                "token_account_id": f"account-{index}",
            }

        def report_success(self, _token):
            pytest.fail("every submit should be rate limited")

        def report_invalid(self, _token):
            pytest.fail("submit 429 must not invalidate the token")

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

    attempts = []
    unavailable_callbacks = []
    monkeypatch.setattr(app, "token_manager", TokenManagerStub())
    monkeypatch.setattr(app, "client", ClientStub())
    monkeypatch.setattr(app, "_append_attempt_log", lambda **kwargs: None)
    sleep_calls = []
    monkeypatch.setattr(app.time, "sleep", lambda delay: sleep_calls.append(delay))

    def run_once(token):
        attempts.append(token)
        raise app.SubmitRateLimitedError()

    with pytest.raises(app.HTTPException) as error_info:
        app._run_with_token_retries(
            request=RequestStub(),
            operation_name="images.generations",
            run_once=run_once,
            set_request_error_detail=lambda *args, **kwargs: "ERR-CODE",
            on_token_unavailable=unavailable_callbacks.append,
        )

    assert error_info.value.status_code == 400
    assert error_info.value.detail == "Too many requests. Please try again later."
    assert attempts == tokens
    assert sleep_calls == [3.0, 6.0] + [9.0] * (len(tokens) - 2)
    assert unavailable_callbacks == tokens


def test_poll_nanobanana_timeout_switches_account_once(monkeypatch):
    import app

    tokens = ["TOKEN-A", "TOKEN-B", "TOKEN-C"]

    class TokenManagerStub:
        def list_active_ids(self):
            return list(tokens)

        def list_active_account_tokens(self):
            return [
                {
                    "token": token,
                    "account_id": f"account-{token.rsplit('-', 1)[-1].lower()}",
                }
                for token in tokens
            ]

        def get_available(self, strategy=None):
            return tokens[0]

        def get_meta_by_value(self, token):
            suffix = token.rsplit("-", 1)[-1].lower()
            return {
                "token_id": f"token-{suffix}",
                "token_account_id": f"account-{suffix}",
            }

        def report_success(self, token):
            self.success = token

        def report_invalid(self, _token):
            pytest.fail("poll nanobanana timeout must not invalidate token")

    class ClientStub:
        retry_enabled = False
        retry_max_attempts = 1
        token_rotation_strategy = "round_robin"

    class RequestState:
        log_id = "LOG_ID"

    class RequestStub:
        method = "POST"
        url = type("Url", (), {"path": "/v1/images/edits"})()
        state = RequestState()

    attempts = []
    unavailable_callbacks = []
    sleep_calls = []
    monkeypatch.setattr(app, "token_manager", TokenManagerStub())
    monkeypatch.setattr(app, "client", ClientStub())
    monkeypatch.setattr(app, "_append_attempt_log", lambda **kwargs: None)
    monkeypatch.setattr(app.time, "sleep", lambda delay: sleep_calls.append(delay))

    def run_once(token):
        attempts.append(token)
        if token == "TOKEN-A":
            raise app.PollNanobananaTimeoutError(
                "poll failed: 408 Gateway timeout from fal-nanobanana"
            )
        return "ok"

    result = app._run_with_token_retries(
        request=RequestStub(),
        operation_name="images.edits",
        run_once=run_once,
        set_request_error_detail=lambda *args, **kwargs: "ERR-CODE",
        on_token_unavailable=unavailable_callbacks.append,
    )

    assert result == "ok"
    assert attempts == ["TOKEN-A", "TOKEN-B"]
    assert sleep_calls == []
    assert unavailable_callbacks == ["TOKEN-A"]


def test_poll_nanobanana_timeout_second_failure_returns_408(monkeypatch):
    import app

    tokens = ["TOKEN-A", "TOKEN-B", "TOKEN-C"]

    class TokenManagerStub:
        def list_active_ids(self):
            return list(tokens)

        def list_active_account_tokens(self):
            return [
                {
                    "token": token,
                    "account_id": f"account-{token.rsplit('-', 1)[-1].lower()}",
                }
                for token in tokens
            ]

        def get_available(self, strategy=None):
            return tokens[0]

        def get_meta_by_value(self, token):
            suffix = token.rsplit("-", 1)[-1].lower()
            return {
                "token_id": f"token-{suffix}",
                "token_account_id": f"account-{suffix}",
            }

        def report_success(self, _token):
            pytest.fail("second fal timeout should return error")

        def report_invalid(self, _token):
            pytest.fail("poll nanobanana timeout must not invalidate token")

    class ClientStub:
        retry_enabled = False
        retry_max_attempts = 1
        token_rotation_strategy = "round_robin"

    class RequestState:
        log_id = "LOG_ID"

    class RequestStub:
        method = "POST"
        url = type("Url", (), {"path": "/v1/images/edits"})()
        state = RequestState()

    attempts = []
    unavailable_callbacks = []
    monkeypatch.setattr(app, "token_manager", TokenManagerStub())
    monkeypatch.setattr(app, "client", ClientStub())
    monkeypatch.setattr(app, "_append_attempt_log", lambda **kwargs: None)

    def run_once(token):
        attempts.append(token)
        raise app.PollNanobananaTimeoutError(
            "poll failed: 408 Gateway timeout from fal-nanobanana"
        )

    with pytest.raises(app.HTTPException) as error_info:
        app._run_with_token_retries(
            request=RequestStub(),
            operation_name="images.edits",
            run_once=run_once,
            set_request_error_detail=lambda *args, **kwargs: "ERR-CODE",
            on_token_unavailable=unavailable_callbacks.append,
        )

    assert error_info.value.status_code == 408
    assert "fal-nanobanana" in str(error_info.value.detail)
    assert attempts == ["TOKEN-A", "TOKEN-B"]
    assert unavailable_callbacks == ["TOKEN-A"]


def test_openai_prefixed_gemini_model_is_normalized():
    assert normalize_openai_gemini_model_id(
        "gpt-image-gemini-3.1-flash-image"
    ) == "gemini-3.1-flash-image"
    assert normalize_openai_gemini_model_id(
        "gpt-image-gemini-3-pro-image"
    ) == "gemini-3-pro-image"
    assert normalize_openai_gemini_model_id("gpt-image-2") is None


def test_openai_sizes_map_to_gemini_ratio_and_resolution():
    assert parse_openai_gemini_size("auto") == ("auto", "2K")
    assert parse_openai_gemini_size("not-a-size") == ("auto", "2K")
    assert parse_openai_gemini_size("1024x1024") == ("1:1", "1K")
    assert parse_openai_gemini_size("1536x1024") == ("3:2", "2K")
    assert parse_openai_gemini_size("1024x1536") == ("2:3", "2K")
    assert parse_openai_gemini_size("1792x1024") == ("16:9", "2K")
    assert parse_openai_gemini_size("1024x1792") == ("9:16", "2K")
    assert parse_openai_gemini_size("4096x4096") == ("1:1", "4K")


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("8:1", "8:1"),
        ("4:1", "4:1"),
        ("21:9", "21:9"),
        ("16:9", "16:9"),
        ("5:4", "5:4"),
        ("4:3", "4:3"),
        ("3:2", "3:2"),
        ("1:1", "1:1"),
        ("4:5", "4:5"),
        ("3:4", "3:4"),
        ("2:3", "2:3"),
        ("9:16", "9:16"),
        ("1:4", "1:4"),
        ("1:8", "1:8"),
        ("7:5", "4:3"),
        ("5:7", "3:4"),
    ],
)
def test_openai_gemini_ratios_use_nearest_upstream_ratio(requested, expected):
    assert parse_openai_gemini_size(requested) == (expected, "2K")


@pytest.mark.parametrize(
    "model_id",
    [
        "gpt-image-gemini-3.1-flash-image",
        "gpt-image-gemini-3-pro-image",
    ],
)
def test_openai_prefixed_gemini_auto_reaches_upstream_payload(model_id):
    ratio, resolution, response_model = resolve_ratio_and_resolution(
        {"size": "auto"},
        model_id,
    )
    model_conf = resolve_model(response_model)

    assert (ratio, resolution, response_model) == ("auto", "2K", model_id)

    payload = build_image_payload_candidates(
        prompt="draw freely",
        aspect_ratio=ratio,
        output_resolution=resolution,
        upstream_model_id=model_conf["upstream_model_id"],
        upstream_model_version=model_conf["upstream_model_version"],
    )[0]

    assert "aspectRatio" not in payload["modelSpecificPayload"]
    assert payload["modelSpecificPayload"]["imageSize"] == "2K"
    assert payload["size"] == {"width": 4096, "height": 4096}

    auto_4k = resolve_ratio_and_resolution(
        {"size": "auto", "image_size": "4K"},
        model_id,
    )
    payload_4k = build_image_payload_candidates(
        prompt="draw freely",
        aspect_ratio=auto_4k[0],
        output_resolution=auto_4k[1],
        upstream_model_id=model_conf["upstream_model_id"],
        upstream_model_version=model_conf["upstream_model_version"],
    )[0]
    assert auto_4k == ("auto", "4K", model_id)
    assert "aspectRatio" not in payload_4k["modelSpecificPayload"]
    assert payload_4k["size"] == {"width": 4096, "height": 4096}


@pytest.mark.parametrize(
    "model_id",
    [
        "gpt-image-gemini-3.1-flash-image",
        "gpt-image-gemini-3-pro-image",
    ],
)
def test_openai_prefixed_gemini_explicit_ratio_uses_nearest_upstream(model_id):
    assert resolve_ratio_and_resolution(
        {"aspect_ratio": "7:5", "image_size": "4K"},
        model_id,
    ) == ("4:3", "4K", model_id)


@pytest.mark.parametrize(
    "model_id",
    [
        "gpt-image-gemini-3.1-flash-image",
        "gpt-image-gemini-3-pro-image",
    ],
)
def test_openai_prefixed_gemini_unknown_ratio_uses_upstream_auto(model_id):
    assert resolve_ratio_and_resolution(
        {"aspect_ratio": "not-a-ratio", "image_size": "1K"},
        model_id,
    ) == ("auto", "1K", model_id)


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

