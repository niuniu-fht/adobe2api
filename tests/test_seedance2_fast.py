import pytest

from api.routes.generation import resolve_video_request_parameters
from core.adobe_client import AdobeClient
from core.models.catalog import VIDEO_MODEL_CATALOG
from core.stores import RequestLogStore


SEEDANCE_CONF = VIDEO_MODEL_CATALOG["firefly-seedance2-fast"]


def test_seedance_fast_catalog_uses_adobe_model_ids():
    assert SEEDANCE_CONF["engine"] == "seedance2-fast"
    assert SEEDANCE_CONF["upstream_model_id"] == "seedance"
    assert SEEDANCE_CONF["upstream_model_version"] == "seedance_2.0_fast"


def test_seedance_fast_preset_parameters_are_fixed_for_chat_compatibility():
    preset_conf = VIDEO_MODEL_CATALOG["sd2-fast-6s-9x16-720p"]
    assert resolve_video_request_parameters({}, preset_conf) == (
        6,
        "9:16",
        "720p",
        None,
    )
    with pytest.raises(ValueError, match="fixes resolution"):
        resolve_video_request_parameters({"resolution": "480p"}, preset_conf)


def test_seedance_fast_request_parameters_override_defaults():
    assert resolve_video_request_parameters(
        {
            "duration": 4,
            "aspect_ratio": "9:16",
            "resolution": "480p",
            "seed": 42,
        },
        SEEDANCE_CONF,
    ) == (4, "9:16", "480p", 42)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"duration": 3}, "duration"),
        ({"aspect_ratio": "2:1"}, "aspect_ratio"),
        ({"resolution": "1080p"}, "resolution"),
        ({"seed": -1}, "seed"),
    ],
)
def test_seedance_fast_request_parameters_reject_unsupported_values(data, message):
    with pytest.raises(ValueError, match=message):
        resolve_video_request_parameters(data, SEEDANCE_CONF)


def test_seedance_fast_text_payload_matches_adobe_schema():
    payload = AdobeClient()._build_video_payload(
        video_conf=SEEDANCE_CONF,
        prompt="A paper boat moving across a rain puddle",
        aspect_ratio="16:9",
        duration=4,
        resolution="720p",
        generate_audio=False,
        seed=1234,
    )

    assert payload == {
        "modelId": "seedance",
        "modelVersion": "seedance_2.0_fast",
        "prompt": "A paper boat moving across a rain puddle",
        "seeds": [1234],
        "size": {"width": 1280, "height": 720},
        "generateAudio": False,
        "duration": 4,
        "generationMetadata": {"module": "text2video"},
        "generationSettings": {"aspectRatio": "16:9"},
        "referenceBlobs": [],
        "output": {"storeInputs": True},
    }
    assert "fps" not in payload
    assert "n" not in payload


def test_seedance_fast_frame_and_media_references_use_distinct_usages():
    client = AdobeClient()
    frame_payload = client._build_video_payload(
        video_conf=SEEDANCE_CONF,
        prompt="Animate the transition",
        aspect_ratio="9:16",
        duration=8,
        resolution="480p",
        source_image_ids=["FIRST", "LAST"],
        reference_mode="frame",
    )
    media_payload = client._build_video_payload(
        video_conf=SEEDANCE_CONF,
        prompt="Use these references",
        aspect_ratio="4:3",
        duration=8,
        resolution="480p",
        source_image_ids=["STYLE_A", "STYLE_B"],
        reference_mode="image",
    )

    assert frame_payload["size"] == {"width": 480, "height": 854}
    assert frame_payload["referenceBlobs"] == [
        {"id": "FIRST", "usage": "frame", "order": 1},
        {"id": "LAST", "usage": "frame", "order": 2},
    ]
    assert media_payload["size"] == {"width": 640, "height": 480}
    assert media_payload["referenceBlobs"] == [
        {"id": "STYLE_A", "usage": "style"},
        {"id": "STYLE_B", "usage": "style"},
    ]


def test_seedance_chat_requests_are_counted_as_video():
    assert not RequestLogStore._is_image_generation_request(
        {
            "path": "/v1/chat/completions",
            "model": "firefly-seedance2-fast",
        }
    )
