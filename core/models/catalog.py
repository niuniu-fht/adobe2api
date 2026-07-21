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
            model_id = f"gpt-image-{res}-{suffix}"
            MODEL_CATALOG[model_id] = {
                "provider": "openai",
                "upstream_model": "openai:firefly:gpt-image",
                "upstream_model_id": "gpt-image",
                "upstream_model_version": "2",
                "output_resolution": res.upper(),
                "aspect_ratio": ratio,
                "description": f"Firefly GPT Image ({res.upper()} {ratio})",
            }


_register_nano_banana_family(
    "nano-banana-pro",
    upstream_model_id="gemini-flash",
    upstream_model_version="nano-banana-2",
    family_label="Firefly Nano Banana Pro",
)
_register_nano_banana_family(
    "nano-banana",
    upstream_model_id="gemini-flash",
    upstream_model_version="nano-banana-2",
    family_label="Firefly Nano Banana",
)
_register_nano_banana_family(
    "nano-banana2",
    upstream_model_id="gemini-flash",
    upstream_model_version="nano-banana-3",
    family_label="Firefly Nano Banana 2",
    ratio_suffix_map=NANO_BANANA2_RATIO_SUFFIX_MAP,
)
_register_gpt_image_family()

for _image_conf in MODEL_CATALOG.values():
    _image_conf.setdefault("provider", "google")


DEFAULT_MODEL_ID = "nano-banana-pro-2k-16x9"

VIDEO_MODEL_CATALOG: dict[str, dict] = {
    "seedance2": {
        "hidden": True,
        "provider": "bytedance",
        "engine": "seedance2",
        "upstream_model_id": "seedance",
        "upstream_model_version": "seedance_2.0",
        "duration": 8,
        "aspect_ratio": "16:9",
        "resolution": "720p",
        "supported_resolutions": ("480p", "720p", "1080p"),
        "generate_audio": True,
        "prompt_max_length": 2500,
        "description": "Firefly Seedance 2.0 video model (4-15s, 480p/720p/1080p)",
    },
    "seedance2-fast": {
        "hidden": True,
        "provider": "bytedance",
        "engine": "seedance2-fast",
        "upstream_model_id": "seedance",
        "upstream_model_version": "seedance_2.0_fast",
        "duration": 8,
        "aspect_ratio": "16:9",
        "resolution": "720p",
        "supported_resolutions": ("480p", "720p"),
        "generate_audio": True,
        "prompt_max_length": 2500,
        "description": "Firefly Seedance 2.0 Fast video model (4-15s, 480p/720p)",
    },
}


def _register_seedance_preset_family(
    prefix: str,
    *,
    engine: str,
    upstream_model_version: str,
    resolutions: tuple[str, ...],
    family_label: str,
) -> None:
    for duration in (4, 6, 8):
        for ratio in ("16:9", "9:16"):
            for resolution in resolutions:
                model_id = (
                    f"{prefix}-{duration}s-{RATIO_SUFFIX_MAP[ratio]}-{resolution}"
                )
                VIDEO_MODEL_CATALOG[model_id] = {
                    "provider": "bytedance",
                    "engine": engine,
                    "upstream_model_id": "seedance",
                    "upstream_model_version": upstream_model_version,
                    "duration": duration,
                    "aspect_ratio": ratio,
                    "resolution": resolution,
                    "supported_resolutions": (resolution,),
                    "fixed_parameters": True,
                    "canonical_model": (
                        "seedance2-fast"
                        if engine == "seedance2-fast"
                        else "seedance2"
                    ),
                    "generate_audio": True,
                    "prompt_max_length": 2500,
                    "description": (
                        f"{family_label} ({duration}s {ratio} {resolution})"
                    ),
                }


_register_seedance_preset_family(
    "sd2",
    engine="seedance2",
    upstream_model_version="seedance_2.0",
    resolutions=("720p", "1080p"),
    family_label="Seedance 2.0",
)
_register_seedance_preset_family(
    "sd2-fast",
    engine="seedance2-fast",
    upstream_model_version="seedance_2.0_fast",
    resolutions=("480p", "720p"),
    family_label="Seedance 2.0 Fast",
)


def _register_sora_family(
    prefix: str,
    *,
    upstream_model: str,
    family_label: str,
) -> None:
    for duration in (4, 8, 12):
        for ratio in ("9:16", "16:9"):
            model_id = f"{prefix}-{duration}s-{RATIO_SUFFIX_MAP[ratio]}"
            VIDEO_MODEL_CATALOG[model_id] = {
                "provider": "openai",
                "engine": "sora2",
                "upstream_model": upstream_model,
                "duration": duration,
                "aspect_ratio": ratio,
                "resolution": "720p",
                "fixed_parameters": True,
                "description": f"{family_label} ({duration}s {ratio})",
            }


_register_sora_family(
    "sora2",
    upstream_model="openai:firefly:colligo:sora2",
    family_label="Sora 2 video model",
)
_register_sora_family(
    "sora2-pro",
    upstream_model="openai:firefly:colligo:sora2-pro",
    family_label="Sora 2 Pro video model",
)


for dur in (4, 6, 8):
    for ratio in ("16:9", "9:16"):
        for res in ("1080p", "720p"):
            model_id = f"veo31-{dur}s-{RATIO_SUFFIX_MAP[ratio]}-{res}"
            VIDEO_MODEL_CATALOG[model_id] = {
                "provider": "google",
                "engine": "veo31-standard",
                "upstream_model": "google:firefly:colligo:veo31",
                "duration": dur,
                "aspect_ratio": ratio,
                "resolution": res,
                "fixed_parameters": True,
                "description": f"Veo 3.1 video model ({dur}s {ratio} {res})",
            }

for dur in (4, 6, 8):
    for ratio in ("16:9", "9:16"):
        for res in ("1080p", "720p"):
            model_id = f"veo31-ref-{dur}s-{RATIO_SUFFIX_MAP[ratio]}-{res}"
            VIDEO_MODEL_CATALOG[model_id] = {
                "provider": "google",
                "engine": "veo31-standard",
                "upstream_model": "google:firefly:colligo:veo31",
                "duration": dur,
                "aspect_ratio": ratio,
                "resolution": res,
                "reference_mode": "image",
                "fixed_parameters": True,
                "description": f"Veo 3.1 reference video model ({dur}s {ratio} {res})",
            }

for dur in (4, 6, 8):
    for ratio in ("16:9", "9:16"):
        for res in ("1080p", "720p"):
            model_id = f"veo31-fast-{dur}s-{RATIO_SUFFIX_MAP[ratio]}-{res}"
            VIDEO_MODEL_CATALOG[model_id] = {
                "provider": "google",
                "engine": "veo31-fast",
                "upstream_model": "google:firefly:colligo:veo31-fast",
                "duration": dur,
                "aspect_ratio": ratio,
                "resolution": res,
                "fixed_parameters": True,
                "description": f"Veo 3.1 Fast video model ({dur}s {ratio} {res})",
            }

for dur in (5, 15):
    for ratio in ("16:9", "9:16"):
        model_id = f"kling-o3-{dur}s-{RATIO_SUFFIX_MAP[ratio]}"
        VIDEO_MODEL_CATALOG[model_id] = {
            "provider": "kling",
            "engine": "kling-o3",
            "upstream_model": "kling:firefly:colligo:o3",
            "duration": dur,
            "aspect_ratio": ratio,
            "resolution": "1080p",
            "fixed_parameters": True,
            "description": f"Kling O3 video model ({dur}s {ratio})",
        }

for dur in (5, 10, 15):
    for ratio in ("16:9", "9:16"):
        model_id = f"kling3-{dur}s-{RATIO_SUFFIX_MAP[ratio]}"
        VIDEO_MODEL_CATALOG[model_id] = {
            "provider": "kling",
            "engine": "kling3",
            "upstream_model": "kling:firefly:colligo:3.0",
            "duration": dur,
            "aspect_ratio": ratio,
            "resolution": "720p",
            "generate_audio": True,
            "fixed_parameters": True,
            "description": f"Kling 3.0 video model ({dur}s {ratio} 720p)",
        }
