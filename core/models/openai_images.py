from __future__ import annotations

import base64
import io
import re
from dataclasses import dataclass
from math import gcd, log, sqrt
from typing import Optional

try:
    from PIL import Image
except Exception:
    Image = None

from .catalog import GPT_IMAGE_RATIO_SUFFIX_MAP, SUPPORTED_RATIOS
from .gemini import normalize_gemini_model_id
from .payloads import gpt_image_pixels_from_ratio


OPENAI_GPT_IMAGE_MODEL_VERSIONS = {
    "gpt-image-2": "2",
}
OPENAI_GEMINI_MODEL_PREFIX = "gpt-image-"
SUPPORTED_RESPONSE_FORMATS = {"url", "b64_json", "base64"}
SUPPORTED_OUTPUT_FORMATS = {"png", "jpeg", "webp"}
DEFAULT_OUTPUT_FORMAT = "png"
MAX_IMAGE_COUNT = 10
MAX_GPT_IMAGE_LONG_EDGE = 3840
MAX_GPT_IMAGE_PIXELS = 8_294_400
GPT_IMAGE_EDGE_ALIGNMENT = 16
SIZE_RE = re.compile(r"^(\d+)x(\d+)$")
RATIO_RE = re.compile(r"^\d+:\d+$")
DEFAULT_GPT_IMAGE_RATIO_SIZE_MAP = {
    "16:9": {"width": 2560, "height": 1440},
}


class OpenAIImageRequestError(ValueError):
    def __init__(self, message: str, param: Optional[str] = None) -> None:
        super().__init__(message)
        self.param = param


@dataclass(frozen=True)
class OpenAIImageGenerationOptions:
    n: int
    aspect_ratio: str
    output_resolution: str
    response_model: str
    response_format: str
    output_format: str
    output_compression: Optional[int]
    requested_size: Optional[dict[str, int]]
    is_native_gpt_image: bool
    upstream_model_id: Optional[str] = None
    upstream_model_version: Optional[str] = None
    resolved_model_id: Optional[str] = None


def is_native_gpt_image_model(model_id: Optional[str]) -> bool:
    return str(model_id or "").strip() in OPENAI_GPT_IMAGE_MODEL_VERSIONS


def normalize_openai_gemini_model_id(model_id: object) -> str | None:
    normalized = str(model_id or "").strip()
    if not normalized.startswith(OPENAI_GEMINI_MODEL_PREFIX):
        return None
    return normalize_gemini_model_id(normalized[len(OPENAI_GEMINI_MODEL_PREFIX) :])


def _nearest_ratio(
    width: int,
    height: int,
    supported_ratios: set[str],
) -> str:
    target = width / height

    def distance(ratio: str) -> tuple[float, str]:
        left, right = ratio.split(":", 1)
        candidate = int(left) / int(right)
        return abs(log(target / candidate)), ratio

    return min(supported_ratios, key=distance)


def _nearest_supported_ratio(width: int, height: int) -> str:
    return _nearest_ratio(width, height, SUPPORTED_RATIOS)


def parse_openai_gemini_size(raw_size: object) -> Optional[tuple[str, str]]:
    if raw_size is None:
        return None
    size = str(raw_size or "").strip().lower()
    if not size or size == "auto":
        return None
    if size in SUPPORTED_RATIOS:
        return size, "2K"
    if RATIO_RE.match(size):
        raise OpenAIImageRequestError(
            f"unsupported Gemini aspect ratio: {size}",
            "size",
        )
    match = SIZE_RE.match(size)
    if not match:
        raise OpenAIImageRequestError(
            "size must be auto, a supported ASPECT_RATIO, or WIDTHxHEIGHT",
            "size",
        )
    width = int(match.group(1))
    height = int(match.group(2))
    if width <= 0 or height <= 0:
        raise OpenAIImageRequestError("size dimensions must be positive", "size")
    requested_size = {"width": width, "height": height}
    return (
        _nearest_supported_ratio(width, height),
        output_resolution_from_size(requested_size),
    )


def parse_image_count(raw_n: object) -> int:
    if raw_n is None:
        return 1
    try:
        count = int(raw_n)
    except (TypeError, ValueError) as exc:
        raise OpenAIImageRequestError("n must be an integer", "n") from exc
    if count < 1 or count > MAX_IMAGE_COUNT:
        raise OpenAIImageRequestError(
            f"n must be between 1 and {MAX_IMAGE_COUNT}", "n"
        )
    return count


def image_generation_batch_sizes(image_count: int) -> list[int]:
    if image_count <= 0:
        return []
    if image_count <= 2:
        return [image_count]
    remaining = image_count
    batches: list[int] = []
    if remaining % 2 == 1:
        batches.append(1)
        remaining -= 1
    batches.extend([2] * (remaining // 2))
    return batches


def parse_response_format(raw_format: object, *, force_b64_json: bool) -> str:
    if force_b64_json:
        return "b64_json"
    response_format = str(raw_format or "url").strip().lower()
    if response_format == "base64":
        response_format = "b64_json"
    if response_format not in SUPPORTED_RESPONSE_FORMATS:
        raise OpenAIImageRequestError(
            "response_format must be one of url, b64_json, or base64",
            "response_format",
        )
    return response_format


def parse_output_format(raw_format: object) -> str:
    output_format = str(raw_format or DEFAULT_OUTPUT_FORMAT).strip().lower()
    if output_format == "jpg":
        output_format = "jpeg"
    if output_format not in SUPPORTED_OUTPUT_FORMATS:
        raise OpenAIImageRequestError(
            "output_format must be one of png, jpeg, or webp",
            "output_format",
        )
    return output_format


def parse_output_compression(raw_compression: object) -> Optional[int]:
    if raw_compression is None:
        return None
    try:
        value = int(raw_compression)
    except (TypeError, ValueError) as exc:
        raise OpenAIImageRequestError(
            "output_compression must be an integer between 0 and 100",
            "output_compression",
        ) from exc
    if value < 0 or value > 100:
        raise OpenAIImageRequestError(
            "output_compression must be between 0 and 100",
            "output_compression",
        )
    return value


def parse_requested_size(raw_size: object) -> Optional[dict[str, int]]:
    if raw_size is None:
        return None
    size = str(raw_size or "").strip().lower()
    if not size or size == "auto":
        return None
    if size in DEFAULT_GPT_IMAGE_RATIO_SIZE_MAP:
        return dict(DEFAULT_GPT_IMAGE_RATIO_SIZE_MAP[size])
    match = SIZE_RE.match(size)
    if not match:
        raise OpenAIImageRequestError(
            "size must be auto or formatted as WIDTHxHEIGHT",
            "size",
        )
    width = int(match.group(1))
    height = int(match.group(2))
    if width <= 0 or height <= 0:
        raise OpenAIImageRequestError("size dimensions must be positive", "size")
    return {"width": width, "height": height}


def aspect_ratio_from_size(size: Optional[dict[str, int]]) -> str:
    if not size:
        return "1:1"
    width = int(size["width"])
    height = int(size["height"])
    divisor = gcd(width, height)
    return f"{width // divisor}:{height // divisor}"


def gpt_image_aspect_ratio_from_size(
    size: Optional[dict[str, int]],
) -> str:
    if not size:
        return "1:1"
    exact_ratio = aspect_ratio_from_size(size)
    if exact_ratio in GPT_IMAGE_RATIO_SUFFIX_MAP:
        return exact_ratio
    return _nearest_ratio(
        int(size["width"]),
        int(size["height"]),
        set(GPT_IMAGE_RATIO_SUFFIX_MAP),
    )


def output_resolution_from_size(size: Optional[dict[str, int]]) -> str:
    if not size:
        return "2K"
    largest_edge = max(int(size["width"]), int(size["height"]))
    if largest_edge <= 1280:
        return "1K"
    if largest_edge <= 2560:
        return "2K"
    return "4K"


def gpt_image_output_resolution_from_size(
    size: Optional[dict[str, int]],
) -> str:
    if not size:
        return "2K"
    width = int(size["width"])
    height = int(size["height"])
    ratio = gpt_image_aspect_ratio_from_size(size)

    def distance(resolution: str) -> tuple[float, int]:
        candidate = gpt_image_pixels_from_ratio(ratio, resolution)
        if candidate is None:
            return float("inf"), 0
        candidate_width = int(candidate["width"])
        candidate_height = int(candidate["height"])
        score = abs(log(width / candidate_width)) + abs(
            log(height / candidate_height)
        )
        return score, int(resolution[0])

    return min(("1K", "2K", "4K"), key=distance)


def normalize_gpt_image_size(
    size: Optional[dict[str, int]],
) -> Optional[dict[str, int]]:
    if not size:
        return None
    width = int(size["width"])
    height = int(size["height"])
    longest_edge = max(width, height)
    total_pixels = width * height
    scale = min(
        1.0,
        MAX_GPT_IMAGE_LONG_EDGE / longest_edge,
        sqrt(MAX_GPT_IMAGE_PIXELS / total_pixels),
    )

    def align_edge(value: float) -> int:
        lower = int(value // GPT_IMAGE_EDGE_ALIGNMENT) * GPT_IMAGE_EDGE_ALIGNMENT
        upper = lower + GPT_IMAGE_EDGE_ALIGNMENT
        aligned = lower if value - lower <= upper - value else upper
        return max(
            GPT_IMAGE_EDGE_ALIGNMENT,
            min(MAX_GPT_IMAGE_LONG_EDGE, aligned),
        )

    target_width = width * scale
    target_height = height * scale
    aligned_width = align_edge(target_width)
    aligned_height = align_edge(target_height)

    while aligned_width * aligned_height > MAX_GPT_IMAGE_PIXELS:
        candidates: list[tuple[int, int]] = []
        if aligned_width > GPT_IMAGE_EDGE_ALIGNMENT:
            candidates.append(
                (aligned_width - GPT_IMAGE_EDGE_ALIGNMENT, aligned_height)
            )
        if aligned_height > GPT_IMAGE_EDGE_ALIGNMENT:
            candidates.append(
                (aligned_width, aligned_height - GPT_IMAGE_EDGE_ALIGNMENT)
            )
        if not candidates:
            break

        def distance(candidate: tuple[int, int]) -> float:
            candidate_width, candidate_height = candidate
            return abs(log(candidate_width / target_width)) + abs(
                log(candidate_height / target_height)
            )

        aligned_width, aligned_height = min(candidates, key=distance)

    return {"width": aligned_width, "height": aligned_height}


def gpt_image_model_id_from_size(size: Optional[dict[str, int]]) -> Optional[str]:
    ratio = gpt_image_aspect_ratio_from_size(size)
    suffix = GPT_IMAGE_RATIO_SUFFIX_MAP.get(ratio)
    if not suffix:
        return None
    resolution = gpt_image_output_resolution_from_size(size).lower()
    return f"firefly-gpt-image-{resolution}-{suffix}"


def build_native_gpt_image_options(
    data: dict,
    *,
    model_id_override: Optional[str] = None,
    response_model: Optional[str] = None,
    upstream_model_version: Optional[str] = None,
) -> OpenAIImageGenerationOptions:
    model_id = str(model_id_override or data.get("model") or "").strip()
    if model_id not in OPENAI_GPT_IMAGE_MODEL_VERSIONS:
        raise OpenAIImageRequestError(f"Invalid model: {model_id}", "model")

    raw_size = data.get("size")
    is_auto_size = raw_size is None or str(raw_size or "").strip().lower() in {
        "",
        "auto",
    }
    try:
        parsed_size = None if is_auto_size else parse_requested_size(raw_size)
    except OpenAIImageRequestError:
        is_auto_size = True
        parsed_size = None

    requested_size = normalize_gpt_image_size(parsed_size)
    aspect_ratio = (
        "auto" if is_auto_size else gpt_image_aspect_ratio_from_size(requested_size)
    )
    output_resolution = (
        "auto" if is_auto_size else gpt_image_output_resolution_from_size(requested_size)
    )
    output_format = parse_output_format(data.get("output_format"))
    model_version = str(
        upstream_model_version or OPENAI_GPT_IMAGE_MODEL_VERSIONS[model_id]
    )
    return OpenAIImageGenerationOptions(
        n=parse_image_count(data.get("n")),
        aspect_ratio=aspect_ratio,
        output_resolution=output_resolution,
        response_model=str(response_model or model_id).strip() or model_id,
        response_format=parse_response_format(
            data.get("response_format"),
            force_b64_json=False,
        ),
        output_format=output_format,
        output_compression=parse_output_compression(data.get("output_compression")),
        requested_size=requested_size,
        is_native_gpt_image=True,
        upstream_model_id="gpt-image",
        upstream_model_version=model_version,
        resolved_model_id=(
            None if is_auto_size else gpt_image_model_id_from_size(requested_size)
        ),
    )


def build_legacy_image_options(
    data: dict,
    *,
    ratio: str,
    output_resolution: str,
    resolved_model_id: str,
) -> OpenAIImageGenerationOptions:
    return OpenAIImageGenerationOptions(
        n=parse_image_count(data.get("n")),
        aspect_ratio=ratio,
        output_resolution=output_resolution,
        response_model=resolved_model_id,
        response_format=parse_response_format(
            data.get("response_format"),
            force_b64_json=False,
        ),
        output_format=parse_output_format(data.get("output_format")),
        output_compression=parse_output_compression(data.get("output_compression")),
        requested_size=None,
        is_native_gpt_image=False,
        resolved_model_id=resolved_model_id,
    )


def encode_image_response_item(
    image_bytes: bytes,
    *,
    image_url: str,
    response_format: str,
    output_format: str,
    output_compression: Optional[int],
) -> dict[str, str]:
    if response_format == "url":
        return {"url": image_url}

    encoded_bytes = convert_image_bytes(
        image_bytes,
        output_format=output_format,
        output_compression=output_compression,
    )
    return {"b64_json": base64.b64encode(encoded_bytes).decode("ascii")}


def convert_image_bytes(
    image_bytes: bytes,
    *,
    output_format: str,
    output_compression: Optional[int],
) -> bytes:
    if output_format == "png" and output_compression is None:
        return image_bytes
    if Image is None:
        raise OpenAIImageRequestError(
            "Pillow is required for output_format conversion",
            "output_format",
        )

    with Image.open(io.BytesIO(image_bytes)) as image:
        out = io.BytesIO()
        normalized_format = "JPEG" if output_format == "jpeg" else output_format.upper()
        save_kwargs: dict[str, int] = {}
        if output_format in {"jpeg", "webp"} and output_compression is not None:
            save_kwargs["quality"] = output_compression
        if output_format == "jpeg" and image.mode in {"RGBA", "LA", "P"}:
            image = image.convert("RGB")
        image.save(out, format=normalized_format, **save_kwargs)
        return out.getvalue()
