import base64

import pytest

from core.models.gemini import (
    GeminiRequestError,
    build_gemini_generate_response,
    gemini_model_resource,
    gemini_model_resources,
    normalize_gemini_model_id,
    parse_gemini_generate_request,
)
from core.models.resolver import resolve_model, resolve_ratio_and_resolution


def test_gemini_model_aliases_resolve_to_public_model_ids():
    assert normalize_gemini_model_id("models/gemini-3.1-flash-image") == (
        "gemini-3.1-flash-image"
    )
    assert normalize_gemini_model_id("nano-banana-2") == "gemini-3.1-flash-image"
    assert normalize_gemini_model_id("nanobanna2") == "gemini-3.1-flash-image"
    assert normalize_gemini_model_id("nano-banana-pro") == "gemini-3-pro-image"
    assert normalize_gemini_model_id("nanobananapro") == "gemini-3-pro-image"


def test_gemini_models_have_generate_content_metadata():
    resources = gemini_model_resources()

    assert [item["name"] for item in resources] == [
        "models/gemini-3.1-flash-image",
        "models/gemini-3-pro-image",
    ]
    assert all(
        item["supportedGenerationMethods"] == ["generateContent"]
        for item in resources
    )
    assert gemini_model_resource("nano-banana-2")["displayName"] == "Nano Banana 2"


def test_parse_gemini_request_accepts_text_inline_image_and_image_config():
    image_bytes = b"fake-png"
    options = parse_gemini_generate_request(
        {
            "systemInstruction": {"parts": [{"text": "Follow the art direction."}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": "Draw a banana astronaut."},
                        {
                            "inlineData": {
                                "mimeType": "image/png",
                                "data": base64.b64encode(image_bytes).decode("ascii"),
                            }
                        },
                    ],
                }
            ],
            "generationConfig": {
                "candidateCount": 2,
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {"aspectRatio": "16:9", "imageSize": "4K"},
            },
        },
        "models/gemini-3.1-flash-image",
    )

    assert options.canonical_model_id == "gemini-3.1-flash-image"
    assert options.prompt == "Follow the art direction.\nDraw a banana astronaut."
    assert options.input_images == [(image_bytes, "image/png")]
    assert options.aspect_ratio == "16:9"
    assert options.image_size == "4K"
    assert options.candidate_count == 2


def test_parse_gemini_request_rejects_missing_text():
    with pytest.raises(GeminiRequestError, match="text part"):
        parse_gemini_generate_request(
            {"contents": [{"parts": []}]},
            "gemini-3-pro-image",
        )


def test_gemini_response_uses_inline_data_candidates():
    response = build_gemini_generate_response(
        model_id="gemini-3-pro-image",
        images_base64=["IMAGE_A", "IMAGE_B"],
        response_id="RESPONSE_ID",
    )

    assert response["modelVersion"] == "gemini-3-pro-image"
    assert response["responseId"] == "RESPONSE_ID"
    assert [
        candidate["content"]["parts"][0]["inlineData"]["data"]
        for candidate in response["candidates"]
    ] == ["IMAGE_A", "IMAGE_B"]
    assert all(
        candidate["finishReason"] == "STOP" for candidate in response["candidates"]
    )


def test_openai_compatible_alias_resolves_matching_family_and_options():
    ratio, resolution, response_model = resolve_ratio_and_resolution(
        {"aspect_ratio": "16:9", "quality": "4k"},
        "nano-banana-2",
    )
    config = resolve_model(response_model)

    assert (ratio, resolution, response_model) == (
        "16:9",
        "4K",
        "nano-banana-2",
    )
    assert config["upstream_model_version"] == "nano-banana-3"

    _, _, pro_response_model = resolve_ratio_and_resolution(
        {"aspect_ratio": "1:1", "quality": "2k"},
        "nano-banana-pro",
    )
    assert resolve_model(pro_response_model)["upstream_model_version"] == (
        "nano-banana-2"
    )
