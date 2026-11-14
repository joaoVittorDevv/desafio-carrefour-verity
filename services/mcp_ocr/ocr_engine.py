"""Read an allowlisted local image and extract text without publishing raw OCR output."""

import math
import os
import stat
from pathlib import Path

from security.pii_guardrail import GuardrailViolation


INPUT_ROOT = Path("/app/inputs")
EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def extract_text(file_path: str, *, engine=None) -> tuple[str, float, str]:
    try:
        if not isinstance(file_path, str) or not file_path or "\x00" in file_path:
            raise GuardrailViolation()
        root = INPUT_ROOT.resolve(strict=True)
        path = Path(file_path).resolve(strict=True)
        if not path.is_file() or path.suffix.lower() not in EXTENSIONS:
            raise GuardrailViolation()
        if path == root or not path.is_relative_to(root):
            raise GuardrailViolation()
        # Walk from the trusted root without following newly swapped symlinks.
        parts = path.relative_to(root).parts
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            image_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(image_fd, "rb") as image_file:
                if not stat.S_ISREG(os.fstat(image_file.fileno()).st_mode):
                    raise GuardrailViolation()
                data = image_file.read(MAX_IMAGE_BYTES + 1)
        finally:
            os.close(fd)
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise GuardrailViolation()
        import cv2
        import numpy as np
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise GuardrailViolation()
        image = cv2.fastNlMeansDenoising(image)
        image = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(image)
        if engine is None:
            from rapidocr_onnxruntime import RapidOCR
            engine = RapidOCR()
        lines, _ = engine(image)
        if not lines:
            raise GuardrailViolation()
        text = "\n".join(line[1] for line in lines)
        scores = [line[2] for line in lines]
        if not text.strip() or not all(isinstance(score, (int, float)) and math.isfinite(score) and 0 <= score <= 1 for score in scores):
            raise GuardrailViolation()
        return text, sum(scores) / len(scores), path.name
    except Exception:
        raise GuardrailViolation() from None
