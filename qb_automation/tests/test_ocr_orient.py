"""Tests for confidence-based OCR auto-orientation (sideways check scans)."""

import pytest

from qb_automation.services.ocr import (
    _ocr_best_orientation,
    _ocr_text_and_conf,
    tesseract_available,
)

pytestmark = pytest.mark.skipif(
    not tesseract_available(), reason="tesseract binary missing")


def _text_image(text: str = "Pay to the order of State Farm 1139.74"):
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.load_default(size=64)
    except TypeError:  # older Pillow without size kwarg
        font = ImageFont.load_default()
    img = Image.new("RGB", (1400, 300), "white")
    ImageDraw.Draw(img).text((40, 80), text, fill="black", font=font)
    return img


def test_upright_scores_above_sideways():
    img = _text_image()
    _, conf_up = _ocr_text_and_conf(img)
    _, conf_side = _ocr_text_and_conf(img.rotate(90, expand=True))
    assert conf_up > conf_side


def test_sideways_recovers_to_readable():
    img = _text_image().rotate(90, expand=True)
    recovered = _ocr_best_orientation(img)
    hits = sum(w in recovered for w in ("Pay", "State", "Farm"))
    assert hits >= 2, recovered[:120]
