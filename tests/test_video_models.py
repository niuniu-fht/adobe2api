import pytest

from api.routes.generation import resolve_video_request_parameters
from core.adobe_client import AdobeClient
from core.models.catalog import MODEL_CATALOG, VIDEO_MODEL_CATALOG


def test_public_model_ids_do_not_use_firefly_prefix():
    public_ids = list(MODEL_CATALOG)
    public_ids.extend(
        model_id
        for model_id, conf in VIDEO_MODEL_CATALOG.items()
        if not conf.get("hidden", False)
    )

    assert public_ids
    assert all(not model_id.startswith("firefly-") for model_id in public_ids)


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
