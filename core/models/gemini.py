from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any

from .image_limits import (
    MAX_INPUT_IMAGES,
    MAX_SINGLE_IMAGE_BYTES,
    ImageInputLimitError,
    add_input_image_bytes,
    validate_input_image_count,
)


GEMINI_IMAGE_MODELS: dict[str, dict[str, str]] = {
    "gemini-3.1-flash-image": {
        "family_prefix": "nano-banana2",
        "display_name": "Nano Banana 2",
        "description": "Gemini 3.1 Flash Image compatible Nano Banana 2 model",
    },
    "gemini-3-pro-image": {
        "family_prefix": "nano-banana-pro",
        "display_name": "Nano Banana Pro",
        "description": "Gemini 3 Pro Image compatible Nano Banana Pro model",
    },
}

GEMINI_MODEL_ALIASES: dict[str, str] = {
    "nano-banana-2": "gemini-3.1-flash-image",
    "nanobanana2": "gemini-3.1-flash-image",
    "nanobanna2": "gemini-3.1-flash-image",
    "nano-banana-pro": "gemini-3-pro-image",
    "nanobananapro": "gemini-3-pro-image",
    "nanobannapro": "gemini-3-pro-image",
}

_SUPPORTED_IMAGE_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
}
_ASPECT_RATIO_ENUM_ALIASES = {
    "ASPECT_RATIO_UNSPECIFIED": "1:1",
    "ASPECT_RATIO_ONE_BY_ONE": "1:1",
    "ASPECT_RATIO_TWO_BY_THREE": "2:3",
    "ASPECT_RATIO_THREE_BY_TWO": "3:2",
    "ASPECT_RATIO_THREE_BY_FOUR": "3:4",
    "ASPECT_RATIO_FOUR_BY_THREE": "4:3",
    "ASPECT_RATIO_FOUR_BY_FIVE": "4:5",
    "ASPECT_RATIO_FIVE_BY_FOUR": "5:4",
    "ASPECT_RATIO_NINE_BY_SIXTEEN": "9:16",
    "ASPECT_RATIO_SIXTEEN_BY_NINE": "16:9",
    "ASPECT_RATIO_TWENTY_ONE_BY_NINE": "21:9",
    "ASPECT_RATIO_ONE_BY_EIGHT": "1:8",
    "ASPECT_RATIO_EIGHT_BY_ONE": "8:1",
    "ASPECT_RATIO_ONE_BY_FOUR": "1:4",
    "ASPECT_RATIO_FOUR_BY_ONE": "4:1",
}
_IMAGE_SIZE_ENUM_ALIASES = {
    "IMAGE_SIZE_UNSPECIFIED": "2K",
    "IMAGE_SIZE_FIVE_TWELVE": "1K",
    "IMAGE_SIZE_ONE_K": "1K",
    "IMAGE_SIZE_TWO_K": "2K",
    "IMAGE_SIZE_FOUR_K": "4K",
    "IMAGE_SIZE_1K": "1K",
    "IMAGE_SIZE_2K": "2K",
    "IMAGE_SIZE_4K": "4K",
}


class GeminiRequestError(ValueError):
    pass


@dataclass(frozen=True)
class GeminiGenerateOptions:
    canonical_model_id: str
    prompt: str
    input_images: list[tuple[bytes, str]]
    aspect_ratio: str
    image_size: str
    candidate_count: int


def normalize_gemini_model_id(model_id: object) -> str | None:
    normalized = str(model_id or "").strip()
    if normalized.startswith("models/"):
        normalized = normalized[7:]
    normalized = GEMINI_MODEL_ALIASES.get(normalized, normalized)
    if normalized in GEMINI_IMAGE_MODELS:
        return normalized
    return None


def gemini_model_resources() -> list[dict[str, Any]]:
    resources = []
    for model_id, config in GEMINI_IMAGE_MODELS.items():
        resources.append(
            {
                "name": f"models/{model_id}",
                "baseModelId": model_id,
                "version": model_id,
                "displayName": config["display_name"],
                "description": config["description"],
                "supportedGenerationMethods": ["generateContent"],
            }
        )
    return resources


def gemini_model_resource(model_id: object) -> dict[str, Any] | None:
    canonical_model_id = normalize_gemini_model_id(model_id)
    if canonical_model_id is None:
        return None
    for resource in gemini_model_resources():
        if resource["baseModelId"] == canonical_model_id:
            return resource
    return None


def _dict_value(data: Any, *keys: str) -> Any:
    if not isinstance(data, dict):
        return None
    for key in keys:
        if key in data:
            return data.get(key)
    return None


def _normalize_aspect_ratio(value: Any) -> str:
    normalized = str(value or "1:1").strip()
    return _ASPECT_RATIO_ENUM_ALIASES.get(normalized.upper(), normalized)


def _normalize_image_size(value: Any) -> str:
    normalized = str(value or "2K").strip().upper()
    return _IMAGE_SIZE_ENUM_ALIASES.get(normalized, normalized)


def _parts_from_content(content: Any) -> list[dict]:
    if not isinstance(content, dict):
        return []
    parts = content.get("parts")
    return [part for part in parts if isinstance(part, dict)] if isinstance(parts, list) else []


def _decode_inline_image(part: dict) -> tuple[bytes, str] | None:
    inline_data = _dict_value(part, "inlineData", "inline_data")
    if not isinstance(inline_data, dict):
        return None
    mime_type = str(
        _dict_value(inline_data, "mimeType", "mime_type") or "image/jpeg"
    ).split(";", 1)[0].strip().lower()
    if mime_type == "image/jpg":
        mime_type = "image/jpeg"
    if mime_type not in _SUPPORTED_IMAGE_MIME_TYPES:
        raise GeminiRequestError(f"unsupported inline image MIME type: {mime_type}")
    encoded = str(inline_data.get("data") or "").strip()
    if not encoded:
        raise GeminiRequestError("inlineData.data is required")
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise GeminiRequestError("inlineData.data must be valid base64") from exc
    if not image_bytes:
        raise GeminiRequestError("inline image is empty")
    if len(image_bytes) > MAX_SINGLE_IMAGE_BYTES:
        raise GeminiRequestError("inline image is too large, max 30MB")
    return image_bytes, mime_type


def parse_gemini_generate_request(
    data: Any,
    model_id: object,
) -> GeminiGenerateOptions:
    if not isinstance(data, dict):
        raise GeminiRequestError("request body must be a JSON object")
    canonical_model_id = normalize_gemini_model_id(model_id)
    if canonical_model_id is None:
        raise GeminiRequestError(f"model not found: {model_id}")

    text_parts: list[str] = []
    input_images: list[tuple[bytes, str]] = []
    total_input_image_bytes = 0
    system_instruction = _dict_value(data, "systemInstruction", "system_instruction")
    for part in _parts_from_content(system_instruction):
        text = str(part.get("text") or "").strip()
        if text:
            text_parts.append(text)

    contents = data.get("contents")
    if isinstance(contents, dict):
        contents = [contents]
    if not isinstance(contents, list):
        raise GeminiRequestError("contents is required")
    for content in contents:
        for part in _parts_from_content(content):
            text = str(part.get("text") or "").strip()
            if text:
                text_parts.append(text)
            inline_image = _decode_inline_image(part)
            if inline_image is not None:
                input_images.append(inline_image)
                try:
                    validate_input_image_count(len(input_images))
                    total_input_image_bytes = add_input_image_bytes(
                        total_input_image_bytes, len(inline_image[0])
                    )
                except ImageInputLimitError as exc:
                    raise GeminiRequestError(str(exc)) from exc

    prompt = "\n".join(text_parts).strip()
    if not prompt:
        raise GeminiRequestError("contents must include a text part")

    generation_config = _dict_value(data, "generationConfig", "generation_config")
    generation_config = generation_config if isinstance(generation_config, dict) else {}
    image_config = _dict_value(generation_config, "imageConfig", "image_config")
    image_config = image_config if isinstance(image_config, dict) else {}
    response_format = _dict_value(
        generation_config, "responseFormat", "response_format"
    )
    response_format = response_format if isinstance(response_format, dict) else {}
    response_image_config = _dict_value(response_format, "image")
    response_image_config = (
        response_image_config if isinstance(response_image_config, dict) else {}
    )
    aspect_ratio = _normalize_aspect_ratio(
        _dict_value(image_config, "aspectRatio", "aspect_ratio")
        or _dict_value(response_image_config, "aspectRatio", "aspect_ratio")
        or _dict_value(generation_config, "aspectRatio", "aspect_ratio")
        or "1:1"
    )
    image_size = _normalize_image_size(
        _dict_value(image_config, "imageSize", "image_size")
        or _dict_value(response_image_config, "imageSize", "image_size")
        or _dict_value(generation_config, "imageSize", "image_size")
        or "2K"
    )
    if image_size not in {"1K", "2K", "4K"}:
        raise GeminiRequestError("imageSize must be 1K, 2K, or 4K")
    try:
        candidate_count = int(
            _dict_value(generation_config, "candidateCount", "candidate_count") or 1
        )
    except (TypeError, ValueError) as exc:
        raise GeminiRequestError("generationConfig.candidateCount must be an integer") from exc
    if candidate_count < 1 or candidate_count > 10:
        raise GeminiRequestError(
            "generationConfig.candidateCount must be between 1 and 10"
        )

    return GeminiGenerateOptions(
        canonical_model_id=canonical_model_id,
        prompt=prompt,
        input_images=input_images,
        aspect_ratio=aspect_ratio,
        image_size=image_size,
        candidate_count=candidate_count,
    )


def build_gemini_generate_response(
    *,
    model_id: str,
    images_base64: list[str],
    mime_type: str = "image/png",
    response_id: str,
) -> dict[str, Any]:
    candidates = []
    for index, encoded_image in enumerate(images_base64):
        candidates.append(
            {
                "content": {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": mime_type,
                                "data": encoded_image,
                            }
                        }
                    ],
                    "role": "model",
                },
                "finishReason": "STOP",
                "index": index,
            }
        )
    return {
        "candidates": candidates,
        "usageMetadata": {
            "promptTokenCount": 0,
            "candidatesTokenCount": 0,
            "totalTokenCount": 0,
        },
        "modelVersion": model_id,
        "responseId": response_id,
    }
