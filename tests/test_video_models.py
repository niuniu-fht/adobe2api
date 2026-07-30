import threading

import pytest

from types import SimpleNamespace

from api.routes.generation import (
    build_unified_video_task_response,
    handle_video_auth_failure,
    normalize_video_generation_error,
    parse_unified_video_request,
    resolve_video_request_parameters,
)
from core.adobe_client import AdobeClient, AdobeRequestError
from core.models.catalog import MODEL_CATALOG, VIDEO_MODEL_CATALOG
from core.token_mgr import TokenManager


def test_public_model_ids_do_not_use_firefly_prefix():
    public_ids = list(MODEL_CATALOG)
    public_ids.extend(
        model_id
        for model_id, conf in VIDEO_MODEL_CATALOG.items()
        if not conf.get("hidden", False)
    )

    assert public_ids
    assert all(not model_id.startswith("firefly-") for model_id in public_ids)


def test_public_video_models_expose_one_id_per_model_family():
    public_ids = {
        model_id
        for model_id, conf in VIDEO_MODEL_CATALOG.items()
        if not conf.get("hidden", False)
    }

    assert public_ids == {
        "seedance2",
        "seedance2-fast",
        "sora2",
        "sora2-pro",
        "veo31",
        "veo31-ref",
        "veo31-fast",
        "kling-o3",
        "kling3",
    }
    assert all("s-" not in model_id for model_id in public_ids)


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            {
                "status": "refreshed",
                "message": "token refreshed via cookie",
                "profile_id": "PROFILE_1",
            },
            ("refreshed", "token refreshed via cookie", "FRESH_TOKEN"),
        ),
        (
            {"status": "retry", "message": "auto refresh failed: expired cookie"},
            ("retry", "auto refresh failed: expired cookie", None),
        ),
        ({}, ("invalid", "Token invalid or expired", None)),
    ],
)
def test_video_auth_failure_uses_bound_token_refresh(result, expected):
    auth_calls = []
    profile_calls = []

    class TokenManagerStub:
        def handle_auth_failure(self, token):
            auth_calls.append(token)
            return result

        def get_available_for_refresh_profile(self, profile_id):
            profile_calls.append(profile_id)
            return "FRESH_TOKEN"

    assert handle_video_auth_failure(TokenManagerStub(), "STALE_TOKEN") == expected
    assert auth_calls == ["STALE_TOKEN"]
    assert profile_calls == (
        ["PROFILE_1"] if result.get("status") == "refreshed" else []
    )


def test_refresh_profile_lookup_returns_new_active_token():
    manager = TokenManager.__new__(TokenManager)
    manager._lock = threading.Lock()
    manager.tokens = [
        {
            "status": "active",
            "refresh_profile_id": "OTHER_PROFILE",
            "value": "OTHER_TOKEN",
        },
        {
            "status": "invalid",
            "refresh_profile_id": "PROFILE_1",
            "value": "STALE_TOKEN",
        },
        {
            "status": "active",
            "refresh_profile_id": "PROFILE_1",
            "value": "FRESH_TOKEN",
        },
    ]

    assert manager.get_available_for_refresh_profile("PROFILE_1") == "FRESH_TOKEN"


def test_video_model_permission_error_preserves_upstream_status():
    error = AdobeRequestError(
        "Adobe account is not authorized for the requested video model",
        status_code=403,
        error_type="permission_denied",
        upstream_code="model_not_authorized",
        error_code="ModelNotAuthorized",
    )

    assert normalize_video_generation_error(error) == (
        "ModelNotAuthorized",
        "Adobe account is not authorized for the requested video model",
        403,
    )


@pytest.mark.parametrize(
    ("model_id", "provider", "engine", "duration", "ratio", "resolution"),
    [
        ("sora2-pro-12s-9x16", "openai", "sora2", 12, "9:16", "720p"),
        (
            "veo31-fast-6s-16x9-1080p",
            "google",
            "veo31-fast",
            6,
            "16:9",
            "1080p",
        ),
        ("kling3-10s-9x16", "kling", "kling3", 10, "9:16", "720p"),
        (
            "sd2-fast-4s-16x9-480p",
            "bytedance",
            "seedance2-fast",
            4,
            "16:9",
            "480p",
        ),
    ],
)
def test_video_model_id_selects_vendor_and_dimensions(
    model_id, provider, engine, duration, ratio, resolution
):
    conf = VIDEO_MODEL_CATALOG[model_id]

    assert conf["provider"] == provider
    assert conf["engine"] == engine
    assert conf["fixed_parameters"] is True
    assert resolve_video_request_parameters({}, conf) == (
        duration,
        ratio,
        resolution,
        None,
    )


@pytest.mark.parametrize(
    ("model_id", "field", "value"),
    [
        ("sora2-4s-16x9", "duration", 4),
        ("sora2-pro-8s-9x16", "seconds", 8),
        ("veo31-6s-16x9-720p", "ratio", "16:9"),
        ("veo31-fast-8s-9x16-1080p", "aspect_ratio", "9:16"),
        ("kling-o3-15s-16x9", "aspectRatio", "16:9"),
    ],
)
def test_fixed_video_models_reject_duration_and_ratio_fields(
    model_id, field, value
):
    with pytest.raises(ValueError, match="encoded in model"):
        resolve_video_request_parameters(
            {field: value},
            VIDEO_MODEL_CATALOG[model_id],
        )


@pytest.mark.parametrize(
    ("model_id", "model_field", "model", "version"),
    [
        (
            "sora2-pro-4s-16x9",
            "model",
            "openai:firefly:colligo:sora2-pro",
            "sora-2",
        ),
        ("veo31-fast-4s-16x9-720p", "modelId", "veo", "3.1-fast-generate"),
        ("kling3-5s-16x9", "modelId", "kling", "kling_v3_standard_i2v"),
    ],
)
def test_video_models_build_their_vendor_payload(
    model_id, model_field, model, version
):
    conf = VIDEO_MODEL_CATALOG[model_id]
    payload = AdobeClient()._build_video_payload(
        video_conf=conf,
        prompt="A paper boat",
        aspect_ratio=conf["aspect_ratio"],
        duration=conf["duration"],
        resolution=conf["resolution"],
    )

    assert payload[model_field] == model
    assert payload["modelVersion"] == version


@pytest.mark.parametrize(
    ("model_id", "duration", "ratio", "resolution"),
    [
        ("sora2", 12, "9:16", "720p"),
        ("veo31-fast", 6, "16:9", "1080p"),
        ("kling3", 15, "9:16", "720p"),
        ("seedance2", 15, "21:9", "1080p"),
    ],
)
def test_generic_video_models_take_dimensions_from_request(
    model_id, duration, ratio, resolution
):
    assert resolve_video_request_parameters(
        {
            "duration": duration,
            "ratio": ratio,
            "resolution": resolution,
        },
        VIDEO_MODEL_CATALOG[model_id],
    ) == (duration, ratio, resolution, None)


@pytest.mark.parametrize(
    ("model_id", "data", "message"),
    [
        ("sora2", {"duration": 6}, "duration"),
        ("veo31", {"ratio": "1:1"}, "ratio"),
        ("kling3", {"resolution": "1080p"}, "resolution"),
        ("seedance2-fast", {"resolution": "1080p"}, "resolution"),
    ],
)
def test_generic_video_models_reject_unsupported_dimensions(model_id, data, message):
    with pytest.raises(ValueError, match=message):
        resolve_video_request_parameters(data, VIDEO_MODEL_CATALOG[model_id])


def test_unified_video_request_maps_material_arrays_and_parameters():
    parsed = parse_unified_video_request(
        {
            "model": "seedance2",
            "prompt": "@图片1 是主角，参考 @视频1 的镜头和 @音频1 的节奏",
            "images": ["https://HOST/character.jpg"],
            "videos": ["https://HOST/camera.mp4"],
            "audios": ["https://HOST/music.mp3"],
            "ratio": "16:9",
            "duration": 15,
            "resolution": "720p",
        },
        VIDEO_MODEL_CATALOG,
    )

    assert parsed["model"] == "seedance2"
    assert parsed["response_model"] == "seedance2"
    assert parsed["duration"] == 15
    assert parsed["ratio"] == "16:9"
    assert parsed["resolution"] == "720p"
    assert parsed["image_refs"] == [
        {"url": "https://HOST/character.jpg", "role": "reference_image"}
    ]
    assert parsed["video_refs"] == [
        {"url": "https://HOST/camera.mp4", "role": "reference_video"}
    ]
    assert parsed["audio_refs"] == [
        {"url": "https://HOST/music.mp3", "role": "reference_audio"}
    ]


def test_unified_video_request_maps_two_images_to_first_and_last_frames():
    parsed = parse_unified_video_request(
        {
            "model": "veo31-fast",
            "prompt": "Smooth transition",
            "images": ["https://HOST/first.jpg", "https://HOST/last.jpg"],
            "duration": 8,
            "ratio": "9:16",
            "resolution": "1080p",
        },
        VIDEO_MODEL_CATALOG,
    )

    assert [item["role"] for item in parsed["image_refs"]] == [
        "first_frame",
        "last_frame",
    ]


def test_unified_seedance_image_is_a_numbered_reference_not_a_frame():
    parsed = parse_unified_video_request(
        {
            "model": "seedance2",
            "prompt": "@图片1 是主角，保持人物一致性",
            "images": ["https://HOST/character.jpg"],
            "ratio": "16:9",
            "duration": 15,
            "resolution": "720p",
        },
        VIDEO_MODEL_CATALOG,
    )

    assert parsed["image_refs"] == [
        {"url": "https://HOST/character.jpg", "role": "reference_image"}
    ]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"prompt": "test"}, "model is required"),
        ({"model": "sora2"}, "prompt is required"),
        (
            {
                "model": "sora2",
                "prompt": "test",
                "videos": ["https://HOST/reference.mp4"],
            },
            "videos supports at most 0",
        ),
        (
            {
                "model": "seedance2",
                "prompt": "test",
                "images": ["file:///tmp/image.jpg"],
            },
            "http or https URL",
        ),
        (
            {"model": "seedance2", "prompt": "test", "watermark": True},
            "watermark",
        ),
    ],
)
def test_unified_video_request_rejects_invalid_protocol_fields(data, message):
    with pytest.raises(ValueError, match=message):
        parse_unified_video_request(data, VIDEO_MODEL_CATALOG)


def test_unified_video_task_response_exposes_query_and_content_contract():
    task = SimpleNamespace(
        id="TASK_ID",
        model="seedance2",
        status="succeeded",
        progress=100,
        ratio="16:9",
        duration=15,
        resolution="720p",
        video_url="http://HOST/generated/TASK_ID.mp4",
        error=None,
        created_at=100,
        updated_at=200,
    )

    response = build_unified_video_task_response(task)
    assert response["task_id"] == "TASK_ID"
    assert response["status"] == "completed"
    assert response["content_url"] == "/v1/videos/TASK_ID/content"
