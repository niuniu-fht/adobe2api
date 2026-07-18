MAX_INPUT_IMAGES = 16
MAX_SINGLE_IMAGE_BYTES = 30 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 200 * 1024 * 1024


class ImageInputLimitError(ValueError):
    pass


def validate_input_image_count(count: int) -> None:
    if int(count) > MAX_INPUT_IMAGES:
        raise ImageInputLimitError(
            f"at most {MAX_INPUT_IMAGES} input images are supported"
        )


def add_input_image_bytes(total_bytes: int, image_bytes: int) -> int:
    updated_total = int(total_bytes) + int(image_bytes)
    if updated_total > MAX_TOTAL_IMAGE_BYTES:
        raise ImageInputLimitError("total input image size is too large, max 200MB")
    return updated_total
