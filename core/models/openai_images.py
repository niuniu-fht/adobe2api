from __future__ import annotations

import base64
import io
import re
from dataclasses import dataclass
from math import gcd
from typing import Optional

try:
    from PIL import Image
except Exception:
    Image = None

from .catalog import GPT_IMAGE_RATIO_SUFFIX_MAP


OPENAI_GPT_IMAGE_MODEL_VERSIONS = {
    "gpt-image-2": "2",
}
SUPPORTED_RESPONSE_FORMATS = {"url", "b64_json"}
SUPPORTED_OUTPUT_FORMATS = {"png", "jpeg", "webp"}
DEFAULT_OUTPUT_FORMAT = "png"
MAX_IMAGE_COUNT = 10
SIZE_RE = re.compile(r"^(\d+)x(\d+)$")


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


def parse_response_format(raw_format: object, *, force_b64_json: bool) -> str:
    if force_b64_json:
        return "b64_json"
    response_format = str(raw_format or "url").strip()
    if response_format not in SUPPORTED_RESPONSE_FORMATS:
        raise OpenAIImageRequestError(
            "response_format must be one of url or b64_json",
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


def output_resolution_from_size(size: Optional[dict[str, int]]) -> str:
    if not size:
        return "2K"
    largest_edge = max(int(size["width"]), int(size["height"]))
    if largest_edge <= 1280:
        return "1K"
    if largest_edge <= 2560:
        return "2K"
    return "4K"


def gpt_image_model_id_from_size(size: Optional[dict[str, int]]) -> Optional[str]:
    ratio = aspect_ratio_from_size(size)
    suffix = GPT_IMAGE_RATIO_SUFFIX_MAP.get(ratio)
    if not suffix:
        return None
    return f"firefly-gpt-image-{output_resolution_from_size(size).lower()}-{suffix}"


def build_native_gpt_image_options(data: dict) -> OpenAIImageGenerationOptions:
    model_id = str(data.get("model") or "").strip()
    if model_id not in OPENAI_GPT_IMAGE_MODEL_VERSIONS:
        raise OpenAIImageRequestError(f"Invalid model: {model_id}", "model")

    requested_size = parse_requested_size(data.get("size"))
    output_format = parse_output_format(data.get("output_format"))
    return OpenAIImageGenerationOptions(
        n=parse_image_count(data.get("n")),
        aspect_ratio=aspect_ratio_from_size(requested_size),
        output_resolution=output_resolution_from_size(requested_size),
        response_model=model_id,
        response_format=parse_response_format(
            data.get("response_format"),
            force_b64_json=True,
        ),
        output_format=output_format,
        output_compression=parse_output_compression(data.get("output_compression")),
        requested_size=requested_size,
        is_native_gpt_image=True,
        upstream_model_id="gpt-image",
        upstream_model_version=OPENAI_GPT_IMAGE_MODEL_VERSIONS[model_id],
        resolved_model_id=gpt_image_model_id_from_size(requested_size),
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
