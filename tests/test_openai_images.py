import base64

from core.models.openai_images import (
    build_native_gpt_image_options,
    encode_image_response_item,
    gpt_image_model_id_from_size,
    image_generation_batch_sizes,
)
from core.models.payloads import build_image_payload_candidates


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
    assert options.response_format == "b64_json"
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
        == "firefly-gpt-image-2k-16x9"
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


def test_image_generation_batch_sizes_limit_each_worker_to_two_images():
    assert image_generation_batch_sizes(1) == [1]
    assert image_generation_batch_sizes(2) == [2]
    assert image_generation_batch_sizes(3) == [1, 2]
    assert image_generation_batch_sizes(4) == [2, 2]
    assert image_generation_batch_sizes(5) == [1, 2, 2]
