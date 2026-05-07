from __future__ import annotations

SUPPORTED_RATIOS = {
    "1:1",
    "1:8",
    "1:4",
    "5:4",
    "9:16",
    "21:9",
    "4:1",
    "16:9",
    "4:3",
    "3:2",
    "4:5",
    "3:4",
    "8:1",
    "2:3",
}
RATIO_SUFFIX_MAP = {
    "1:1": "1x1",
    "16:9": "16x9",
    "9:16": "9x16",
    "4:3": "4x3",
    "3:4": "3x4",
}
NANO_BANANA2_RATIO_SUFFIX_MAP = {
    **RATIO_SUFFIX_MAP,
    "1:8": "1x8",
    "1:4": "1x4",
    "4:1": "4x1",
    "8:1": "8x1",
}
GPT_IMAGE_RATIO_SUFFIX_MAP = {
    "1:1": "1x1",
    "5:4": "5x4",
    "9:16": "9x16",
    "21:9": "21x9",
    "16:9": "16x9",
    "3:2": "3x2",
    "4:3": "4x3",
    "4:5": "4x5",
    "3:4": "3x4",
    "2:3": "2x3",
}

MODEL_CATALOG: dict[str, dict] = {}


def _register_nano_banana_family(
    prefix: str,
    *,
    upstream_model_id: str,
    upstream_model_version: str,
    family_label: str,
    ratio_suffix_map: dict[str, str] = RATIO_SUFFIX_MAP,
) -> None:
    for res in ("1k", "2k", "4k"):
        for ratio, suffix in ratio_suffix_map.items():
            model_id = f"{prefix}-{res}-{suffix}"
            MODEL_CATALOG[model_id] = {
                "upstream_model": "google:firefly:colligo:nano-banana-pro",
                "upstream_model_id": upstream_model_id,
                "upstream_model_version": upstream_model_version,
                "output_resolution": res.upper(),
                "aspect_ratio": ratio,
                "description": f"{family_label} ({res.upper()} {ratio})",
            }


def _register_gpt_image_family() -> None:
    for res in ("1k", "2k", "4k"):
        for ratio, suffix in GPT_IMAGE_RATIO_SUFFIX_MAP.items():
            model_id = f"firefly-gpt-image-{res}-{suffix}"
            MODEL_CATALOG[model_id] = {
                "upstream_model": "openai:firefly:gpt-image",
                "upstream_model_id": "gpt-image",
                "upstream_model_version": "2",
                "output_resolution": res.upper(),
                "aspect_ratio": ratio,
                "description": f"Firefly GPT Image ({res.upper()} {ratio})",
            }


_register_nano_banana_family(
    "firefly-nano-banana-pro",
    upstream_model_id="gemini-flash",
    upstream_model_version="nano-banana-2",
    family_label="Firefly Nano Banana Pro",
)
_register_nano_banana_family(
    "firefly-nano-banana",
    upstream_model_id="gemini-flash",
    upstream_model_version="nano-banana-2",
    family_label="Firefly Nano Banana",
)
_register_nano_banana_family(
    "firefly-nano-banana2",
    upstream_model_id="gemini-flash",
    upstream_model_version="nano-banana-3",
    family_label="Firefly Nano Banana 2",
    ratio_suffix_map=NANO_BANANA2_RATIO_SUFFIX_MAP,
)
_register_gpt_image_family()

DEFAULT_MODEL_ID = "firefly-nano-banana-pro-2k-16x9"

VIDEO_MODEL_CATALOG: dict[str, dict] = {
    "firefly-sora2-4s-9x16": {
        "duration": 4,
        "aspect_ratio": "9:16",
        "description": "Firefly Sora2 video model (4s 9:16)",
    },
    "firefly-sora2-4s-16x9": {
        "duration": 4,
        "aspect_ratio": "16:9",
        "description": "Firefly Sora2 video model (4s 16:9)",
    },
    "firefly-sora2-8s-9x16": {
        "duration": 8,
        "aspect_ratio": "9:16",
        "description": "Firefly Sora2 video model (8s 9:16)",
    },
    "firefly-sora2-8s-16x9": {
        "duration": 8,
        "aspect_ratio": "16:9",
        "description": "Firefly Sora2 video model (8s 16:9)",
    },
    "firefly-sora2-12s-9x16": {
        "duration": 12,
        "aspect_ratio": "9:16",
        "description": "Firefly Sora2 video model (12s 9:16)",
    },
    "firefly-sora2-12s-16x9": {
        "duration": 12,
        "aspect_ratio": "16:9",
        "description": "Firefly Sora2 video model (12s 16:9)",
    },
}

for dur in (4, 8, 12):
    for ratio in ("9:16", "16:9"):
        model_id = f"firefly-sora2-pro-{dur}s-{RATIO_SUFFIX_MAP[ratio]}"
        VIDEO_MODEL_CATALOG[model_id] = {
            "duration": dur,
            "aspect_ratio": ratio,
            "upstream_model": "openai:firefly:colligo:sora2-pro",
            "description": f"Firefly Sora2 Pro video model ({dur}s {ratio})",
        }

for dur in (4, 6, 8):
    for ratio in ("16:9", "9:16"):
        for res in ("1080p", "720p"):
            model_id = f"firefly-veo31-{dur}s-{RATIO_SUFFIX_MAP[ratio]}-{res}"
            VIDEO_MODEL_CATALOG[model_id] = {
                "engine": "veo31-standard",
                "upstream_model": "google:firefly:colligo:veo31",
                "duration": dur,
                "aspect_ratio": ratio,
                "resolution": res,
                "description": f"Firefly Veo31 video model ({dur}s {ratio} {res})",
            }

for dur in (4, 6, 8):
    for ratio in ("16:9", "9:16"):
        for res in ("1080p", "720p"):
            model_id = f"firefly-veo31-ref-{dur}s-{RATIO_SUFFIX_MAP[ratio]}-{res}"
            VIDEO_MODEL_CATALOG[model_id] = {
                "engine": "veo31-standard",
                "upstream_model": "google:firefly:colligo:veo31",
                "duration": dur,
                "aspect_ratio": ratio,
                "resolution": res,
                "reference_mode": "image",
                "description": f"Firefly Veo31 Ref video model ({dur}s {ratio} {res})",
            }

for dur in (4, 6, 8):
    for ratio in ("16:9", "9:16"):
        for res in ("1080p", "720p"):
            model_id = f"firefly-veo31-fast-{dur}s-{RATIO_SUFFIX_MAP[ratio]}-{res}"
            VIDEO_MODEL_CATALOG[model_id] = {
                "engine": "veo31-fast",
                "upstream_model": "google:firefly:colligo:veo31-fast",
                "duration": dur,
                "aspect_ratio": ratio,
                "resolution": res,
                "description": f"Firefly Veo31 Fast video model ({dur}s {ratio} {res})",
            }

for dur in (5, 15):
    for ratio in ("16:9", "9:16"):
        model_id = f"firefly-kling-o3-{dur}s-{RATIO_SUFFIX_MAP[ratio]}"
        VIDEO_MODEL_CATALOG[model_id] = {
            "engine": "kling-o3",
            "upstream_model": "kling:firefly:colligo:o3",
            "duration": dur,
            "aspect_ratio": ratio,
            "resolution": "1080p",
            "description": f"Firefly Kling O3 video model ({dur}s {ratio})",
        }

for dur in (5, 10, 15):
    for ratio in ("16:9", "9:16"):
        model_id = f"firefly-kling3-{dur}s-{RATIO_SUFFIX_MAP[ratio]}"
        VIDEO_MODEL_CATALOG[model_id] = {
            "engine": "kling3",
            "upstream_model": "kling:firefly:colligo:3.0",
            "duration": dur,
            "aspect_ratio": ratio,
            "resolution": "720p",
            "generate_audio": True,
            "description": f"Firefly Kling 3.0 video model ({dur}s {ratio} 720p)",
        }
