import pytest
import base64
from types import SimpleNamespace

from api.routes.generation import (
    build_grok_video_task_response,
    load_seedance_media_reference,
    parse_grok_video_request,
    parse_seedance_official_request,
    resolve_video_request_parameters,
)
from core.adobe_client import AdobeClient
from core.models.catalog import VIDEO_MODEL_CATALOG
from core.stores import RequestLogStore


SEEDANCE_CONF = VIDEO_MODEL_CATALOG["firefly-seedance2"]


def test_seedance_catalog_uses_adobe_standard_model_ids():
    assert SEEDANCE_CONF["engine"] == "seedance2"
    assert SEEDANCE_CONF["upstream_model_id"] == "seedance"
    assert SEEDANCE_CONF["upstream_model_version"] == "seedance_2.0"
    assert SEEDANCE_CONF["supported_resolutions"] == (
        "480p",
        "720p",
        "1080p",
    )


def test_seedance_standard_preset_model_ids_are_frontend_friendly():
    preset_ids = sorted(
        model_id
        for model_id in VIDEO_MODEL_CATALOG
        if model_id.startswith("sd2-") and not model_id.startswith("sd2-fast-")
    )
    assert len(preset_ids) == 12
    assert preset_ids[0] == "sd2-4s-16x9-1080p"
    assert "sd2-6s-9x16-720p" in preset_ids
    assert all("4s" in model_id or "6s" in model_id or "8s" in model_id for model_id in preset_ids)


def test_seedance_fast_preset_model_ids_only_expose_adobe_resolutions():
    preset_ids = sorted(
        model_id
        for model_id in VIDEO_MODEL_CATALOG
        if model_id.startswith("sd2-fast-")
    )
    assert len(preset_ids) == 12
    assert "sd2-fast-4s-16x9-480p" in preset_ids
    assert "sd2-fast-8s-9x16-720p" in preset_ids
    assert not any(model_id.endswith("-1080p") for model_id in preset_ids)


def test_grok_video_request_maps_to_seedance_fast_async_task():
    parsed = parse_grok_video_request(
        {
            "model": "grok-imagine-video-1.5",
            "prompt": "A paper boat in the rain",
            "duration": "8",
            "aspect_ratio": "16:9",
            "resolution": "480p",
        },
        VIDEO_MODEL_CATALOG,
    )

    assert parsed["response_model"] == "grok-imagine-video-1.5"
    assert parsed["model"] == "firefly-seedance2-fast"
    assert parsed["duration"] == 8
    assert parsed["ratio"] == "16:9"
    assert parsed["resolution"] == "480p"


def test_grok_video_endpoint_shape_accepts_fixed_sd2_model_name():
    parsed = parse_grok_video_request(
        {
            "model": "sd2-fast-4s-16x9-480p",
            "prompt": "A paper boat in the rain",
        },
        VIDEO_MODEL_CATALOG,
    )

    assert parsed["response_model"] == "sd2-fast-4s-16x9-480p"
    assert parsed["model"] == "sd2-fast-4s-16x9-480p"
    assert parsed["duration"] == 4
    assert parsed["ratio"] == "16:9"
    assert parsed["resolution"] == "480p"


@pytest.mark.parametrize("ratio", ["933", "930", "9:33", "9:30"])
def test_grok_video_request_rejects_undefined_seedance_ratios(ratio):
    with pytest.raises(ValueError, match="aspect_ratio"):
        parse_grok_video_request(
            {
                "model": "sd2-fast-4s-16x9-480p",
                "prompt": "A paper boat",
                "aspect_ratio": ratio,
            },
            VIDEO_MODEL_CATALOG,
        )


def test_grok_video_1080p_maps_to_seedance_standard():
    parsed = parse_grok_video_request(
        {
            "model": "grok-imagine-video",
            "prompt": "A quiet lake",
            "resolution": "1080p",
        },
        VIDEO_MODEL_CATALOG,
    )

    assert parsed["model"] == "firefly-seedance2"
    assert parsed["resolution"] == "1080p"


def test_grok_video_request_maps_image_data_url_as_first_frame():
    parsed = parse_grok_video_request(
        {
            "model": "grok-imagine-video",
            "image": {"url": "data:image/png;base64,AAAA"},
            "seconds": 4,
            "resolution": "720p",
        },
        VIDEO_MODEL_CATALOG,
    )

    assert parsed["image_refs"] == [
        {"url": "data:image/png;base64,AAAA", "role": "first_frame"}
    ]


def test_grok_video_request_rejects_file_id_input():
    with pytest.raises(ValueError, match="file_id"):
        parse_grok_video_request(
            {
                "model": "grok-imagine-video",
                "image": {"file_id": "file_TEST"},
            },
            VIDEO_MODEL_CATALOG,
        )


def test_grok_video_task_response_matches_xai_status_shape():
    pending = SimpleNamespace(
        status="running",
        progress=42.4,
        model="grok-imagine-video-1.5",
        video_url=None,
        duration=8,
        error=None,
    )
    done = SimpleNamespace(
        status="succeeded",
        progress=100,
        model="grok-imagine-video-1.5",
        video_url="http://127.0.0.1:6001/generated/RESULT.mp4",
        duration=8,
        error=None,
    )

    assert build_grok_video_task_response(pending) == {
        "status": "pending",
        "progress": 42,
        "model": "grok-imagine-video-1.5",
    }
    assert build_grok_video_task_response(done)["video"]["url"].endswith(
        "/generated/RESULT.mp4"
    )
    assert build_grok_video_task_response(done)["status"] == "done"


def test_official_seedance_preset_model_decodes_parameters_from_model_name():
    parsed = parse_seedance_official_request(
        {
            "model": "sd2-fast-4s-16x9-480p",
            "content": [{"type": "text", "text": "A paper boat"}],
        },
        VIDEO_MODEL_CATALOG,
    )

    assert parsed["model"] == "sd2-fast-4s-16x9-480p"
    assert parsed["response_model"] == "sd2-fast-4s-16x9-480p"
    assert parsed["official_model"] == "dreamina-seedance-2-0-fast-260128"
    assert parsed["duration"] == 4
    assert parsed["ratio"] == "16:9"
    assert parsed["resolution"] == "480p"


def test_official_seedance_preset_rejects_conflicting_duplicate_parameters():
    with pytest.raises(ValueError, match="fixes duration"):
        parse_seedance_official_request(
            {
                "model": "sd2-4s-16x9-1080p",
                "content": [{"type": "text", "text": "A paper boat"}],
                "duration": 8,
            },
            VIDEO_MODEL_CATALOG,
        )


def test_seedance_standard_accepts_1080p_request_parameters():
    assert resolve_video_request_parameters(
        {
            "duration": 15,
            "aspect_ratio": "21:9",
            "resolution": "1080p",
            "seed": 2468,
        },
        SEEDANCE_CONF,
    ) == (15, "21:9", "1080p", 2468)


def test_seedance_standard_payload_matches_adobe_schema():
    payload = AdobeClient()._build_video_payload(
        video_conf=SEEDANCE_CONF,
        prompt="A lantern glowing beside a calm lake",
        aspect_ratio="16:9",
        duration=4,
        resolution="1080p",
        generate_audio=False,
        seed=2468,
    )

    assert payload == {
        "modelId": "seedance",
        "modelVersion": "seedance_2.0",
        "prompt": "A lantern glowing beside a calm lake",
        "seeds": [2468],
        "size": {"width": 1920, "height": 1080},
        "generateAudio": False,
        "duration": 4,
        "generationMetadata": {"module": "text2video"},
        "generationSettings": {"aspectRatio": "16:9"},
        "referenceBlobs": [],
        "output": {"storeInputs": True},
    }


def test_seedance_standard_1080p_sizes_cover_landscape_and_portrait():
    client = AdobeClient()
    assert client._seedance_video_size("21:9", "1080p") == {
        "width": 2520,
        "height": 1080,
    }
    assert client._seedance_video_size("9:16", "1080p") == {
        "width": 1080,
        "height": 1920,
    }


def test_seedance_standard_chat_requests_are_counted_as_video():
    assert not RequestLogStore._is_image_generation_request(
        {
            "path": "/v1/chat/completions",
            "model": "firefly-seedance2",
        }
    )


def test_official_seedance_request_uses_current_model_id_and_adaptive_ratio():
    parsed = parse_seedance_official_request(
        {
            "model": "dreamina-seedance-2-0-260128",
            "content": [{"type": "text", "text": "A quiet lake at sunrise"}],
            "ratio": "adaptive",
            "resolution": "720p",
            "duration": 5,
            "generate_audio": True,
        },
        VIDEO_MODEL_CATALOG,
    )

    assert parsed["model"] == "firefly-seedance2"
    assert parsed["official_model"] == "dreamina-seedance-2-0-260128"
    assert parsed["ratio"] == "adaptive"
    assert parsed["upstream_ratio"] == "auto"


@pytest.mark.parametrize("ratio", ["9:33", "9:30", "933", "930"])
def test_official_seedance_request_rejects_custom_ratios(ratio):
    with pytest.raises(ValueError, match="ratio must be"):
        parse_seedance_official_request(
            {
                "model": "dreamina-seedance-2-0-260128",
                "content": [{"type": "text", "text": "A test video"}],
                "ratio": ratio,
            },
            VIDEO_MODEL_CATALOG,
        )


@pytest.mark.parametrize(
    ("model", "resolution"),
    [
        ("dreamina-seedance-2-0-fast-260128", "1080p"),
        ("dreamina-seedance-2-0-260128", "4k"),
    ],
)
def test_official_seedance_request_respects_adobe_resolution_limits(
    model, resolution
):
    with pytest.raises(ValueError, match="resolution"):
        parse_seedance_official_request(
            {
                "model": model,
                "content": [{"type": "text", "text": "A test video"}],
                "resolution": resolution,
            },
            VIDEO_MODEL_CATALOG,
        )


def test_official_seedance_request_maps_first_and_last_frames():
    parsed = parse_seedance_official_request(
        {
            "model": "dreamina-seedance-2-0-260128",
            "content": [
                {"type": "text", "text": "A smooth transition"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://HOST/first.png"},
                    "role": "first_frame",
                },
                {
                    "type": "image_url",
                    "image_url": {"url": "https://HOST/last.png"},
                    "role": "last_frame",
                },
            ],
        },
        VIDEO_MODEL_CATALOG,
    )

    assert [item["role"] for item in parsed["image_refs"]] == [
        "first_frame",
        "last_frame",
    ]


def test_official_seedance_request_rejects_duplicate_frame_roles():
    with pytest.raises(ValueError, match="only one first_frame"):
        parse_seedance_official_request(
            {
                "model": "dreamina-seedance-2-0-260128",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://HOST/first-a.png"},
                        "role": "first_frame",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://HOST/first-b.png"},
                        "role": "first_frame",
                    },
                ],
            },
            VIDEO_MODEL_CATALOG,
        )


def test_official_seedance_request_accepts_complete_multimodal_references():
    parsed = parse_seedance_official_request(
        {
            "model": "dreamina-seedance-2-0-260128",
            "content": [
                {"type": "text", "text": "Follow the references."},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://HOST/reference.png"},
                    "role": "reference_image",
                },
                {
                    "type": "video_url",
                    "video_url": {"url": "https://HOST/reference.mp4"},
                    "role": "reference_video",
                },
                {
                    "type": "audio_url",
                    "audio_url": {"url": "data:audio/mpeg;base64,SUQz"},
                    "role": "reference_audio",
                },
            ],
        },
        VIDEO_MODEL_CATALOG,
    )

    assert parsed["image_refs"] == [
        {"url": "https://HOST/reference.png", "role": "reference_image"}
    ]
    assert parsed["video_refs"] == [
        {"url": "https://HOST/reference.mp4", "role": "reference_video"}
    ]
    assert parsed["audio_refs"] == [
        {"url": "data:audio/mpeg;base64,SUQz", "role": "reference_audio"}
    ]


def test_official_seedance_request_rejects_audio_only_reference():
    with pytest.raises(ValueError, match="reference_audio requires"):
        parse_seedance_official_request(
            {
                "model": "dreamina-seedance-2-0-260128",
                "content": [
                    {
                        "type": "audio_url",
                        "audio_url": {"url": "https://HOST/reference.mp3"},
                        "role": "reference_audio",
                    }
                ],
            },
            VIDEO_MODEL_CATALOG,
        )


def test_official_seedance_request_rejects_untyped_frame_with_video_reference():
    with pytest.raises(ValueError, match="typed and untyped"):
        parse_seedance_official_request(
            {
                "model": "dreamina-seedance-2-0-260128",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://HOST/frame.png"},
                    },
                    {
                        "type": "video_url",
                        "video_url": {"url": "https://HOST/reference.mp4"},
                        "role": "reference_video",
                    },
                ],
            },
            VIDEO_MODEL_CATALOG,
        )


def test_official_seedance_request_respects_adobe_multimodal_reference_limit():
    content = [
        {
            "type": "image_url",
            "image_url": {"url": f"https://HOST/reference-image-{idx}.png"},
            "role": "reference_image",
        }
        for idx in range(7)
    ]
    content.extend(
        [
            {
                "type": "video_url",
                "video_url": {"url": "https://HOST/reference-video-1.mp4"},
                "role": "reference_video",
            },
            {
                "type": "video_url",
                "video_url": {"url": "https://HOST/reference-video-2.mp4"},
                "role": "reference_video",
            },
            {
                "type": "audio_url",
                "audio_url": {"url": "https://HOST/reference-audio.mp3"},
                "role": "reference_audio",
            },
        ]
    )
    with pytest.raises(ValueError, match="at most nine multimodal"):
        parse_seedance_official_request(
            {
                "model": "dreamina-seedance-2-0-260128",
                "content": content,
            },
            VIDEO_MODEL_CATALOG,
        )


def test_seedance_multimodal_payload_maps_adobe_reference_blobs():
    payload = AdobeClient()._build_video_payload(
        video_conf=SEEDANCE_CONF,
        prompt="Follow the references",
        aspect_ratio="16:9",
        duration=5,
        resolution="720p",
        source_image_ids=["IMAGE_ID"],
        source_video_ids=["VIDEO_ID"],
        source_audio_ids=["AUDIO_ID"],
        reference_mode="image",
    )

    assert payload["generationMetadata"] == {"module": "image2video"}
    assert payload["referenceBlobs"] == [
        {"id": "IMAGE_ID", "usage": "style"},
        {
            "id": "VIDEO_ID",
            "usage": "source",
            "mention": {"id": "seedance-video-ref-01", "label": "Video1"},
        },
        {
            "id": "AUDIO_ID",
            "usage": "source",
            "mention": {"id": "seedance-audio-ref-01", "label": "Audio1"},
        },
    ]


@pytest.mark.parametrize(
    ("media_type", "data_url", "expected_bytes", "expected_mime"),
    [
        ("video", "data:video/mp4;base64,AAAAIGZ0eXA=", b"\x00\x00\x00 ftyp", "video/mp4"),
        ("audio", "data:audio/mpeg;base64,SUQz", b"ID3", "audio/mpeg"),
    ],
)
def test_seedance_media_loader_accepts_data_urls(
    media_type, data_url, expected_bytes, expected_mime
):
    assert load_seedance_media_reference(data_url, media_type) == (
        expected_bytes,
        expected_mime,
    )


def test_seedance_media_loader_accepts_plain_base64():
    encoded = base64.b64encode(b"plain-video-data").decode("ascii")
    assert load_seedance_media_reference(encoded, "video") == (
        b"plain-video-data",
        "video/mp4",
    )


def test_seedance_media_loader_streams_url_in_memory(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "application/octet-stream"}

        def iter_content(self, chunk_size):
            assert chunk_size == 1024 * 1024
            yield b"video-"
            yield b"bytes"

        def close(self):
            calls.append("closed")

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr("api.routes.generation.requests.get", fake_get)
    assert load_seedance_media_reference(
        "https://HOST/reference.mp4", "video", proxies={"https": "PROXY"}
    ) == (b"video-bytes", "video/mp4")
    assert calls[0] == (
        "https://HOST/reference.mp4",
        {"timeout": 60, "proxies": {"https": "PROXY"}, "stream": True},
    )
    assert calls[1] == "closed"


@pytest.mark.parametrize(
    ("media_type", "expected_url"),
    [
        ("video", AdobeClient.video_upload_url),
        ("audio", AdobeClient.audio_upload_url),
    ],
)
def test_adobe_media_upload_uses_asset_storage_endpoint(
    monkeypatch, media_type, expected_url
):
    client = AdobeClient()
    captured = {}

    class FakeResponse:
        status_code = 200
        text = '{"assets":[{"id":"ASSET_ID"}]}'

        def json(self):
            return {"assets": [{"id": "ASSET_ID"}]}

    def fake_post_bytes(url, headers, payload):
        captured.update(url=url, headers=headers, payload=payload)
        return FakeResponse()

    monkeypatch.setattr(client, "_post_bytes", fake_post_bytes)
    media_id = client.upload_media(
        "TOKEN", b"reference-bytes", f"{media_type}/mp4", media_type
    )

    assert media_id == "ASSET_ID"
    assert captured["url"] == expected_url
    assert captured["payload"] == b"reference-bytes"
    assert captured["headers"]["authorization"] == "Bearer TOKEN"
    assert captured["headers"]["content-type"] == f"{media_type}/mp4"
