from types import SimpleNamespace

import pytest

from security import GuardrailViolation
from services.mcp_ocr import ocr_engine


class StubCv2:
    IMREAD_GRAYSCALE = 0

    def imdecode(self, data, mode):
        return "image" if bytes(data) == b"image" else None

    def fastNlMeansDenoising(self, image):
        return image

    def createCLAHE(self, **kwargs):
        return SimpleNamespace(apply=lambda image: image)


def test_extract_text_from_allowlisted_image_with_mock_ocr(tmp_path, monkeypatch):
    import sys
    monkeypatch.setattr(ocr_engine, "INPUT_ROOT", tmp_path)
    monkeypatch.setitem(sys.modules, "cv2", StubCv2())
    image = tmp_path / "order.png"
    image.write_bytes(b"image")
    result = ocr_engine.extract_text(str(image), engine=lambda image: (
        [(None, "Hemograma", 0.9), (None, "Glicemia", 0.8)], None))
    assert result == ("Hemograma\nGlicemia", pytest.approx(0.85), "order.png")


def test_extract_text_blocks_path_traversal_symlink_bad_extension_and_corrupt_image(tmp_path, monkeypatch):
    import sys
    monkeypatch.setattr(ocr_engine, "INPUT_ROOT", tmp_path / "inputs")
    monkeypatch.setitem(sys.modules, "cv2", StubCv2())
    root = tmp_path / "inputs"
    root.mkdir()
    outside = tmp_path / "sensitive.png"
    outside.write_bytes(b"image")
    (root / "link.png").symlink_to(outside)
    (root / "wrong.txt").write_bytes(b"image")
    (root / "empty.png").write_bytes(b"corrupt")
    for path in (outside, root / "link.png", root / "wrong.txt", root / "empty.png"):
        with pytest.raises(GuardrailViolation) as exc:
            ocr_engine.extract_text(str(path), engine=lambda image: ([(None, "safe", 0.9)], None))
        assert str(path) not in str(exc.value)


def test_extract_text_blocks_invalid_or_empty_ocr(tmp_path, monkeypatch):
    import sys
    monkeypatch.setattr(ocr_engine, "INPUT_ROOT", tmp_path)
    monkeypatch.setitem(sys.modules, "cv2", StubCv2())
    image = tmp_path / "order.webp"
    image.write_bytes(b"image")
    for lines in ([], [(None, "safe", float("nan"))]):
        with pytest.raises(GuardrailViolation):
            ocr_engine.extract_text(str(image), engine=lambda image: (lines, None))
