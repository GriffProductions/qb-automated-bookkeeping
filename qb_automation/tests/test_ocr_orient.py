"""Tests for confidence-based OCR auto-orientation (sideways check scans)."""

import pytest

from qb_automation.services.ocr import (
    _ocr_best_orientation,
    _ocr_text_and_conf,
    apply_orientations,
    pil_to_pdf_angle,
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


def test_pil_to_pdf_angle_conversion():
    assert pil_to_pdf_angle(0) == 0
    assert pil_to_pdf_angle(90) == 270
    assert pil_to_pdf_angle(180) == 180
    assert pil_to_pdf_angle(270) == 90


def test_apply_orientations_sets_rotate_flag(tmp_path):
    from pypdf import PdfReader, PdfWriter

    src = tmp_path / "src.pdf"
    writer = PdfWriter()
    writer.add_blank_page(612, 792)
    writer.add_blank_page(612, 792)
    with open(src, "wb") as fh:
        writer.write(fh)
    dst = tmp_path / "dst.pdf"
    assert apply_orientations(src, dst, {0: 270, 1: 0}) == [1]
    reader = PdfReader(str(dst))
    assert int(reader.pages[0].get("/Rotate") or 0) == 90
    assert int(reader.pages[1].get("/Rotate") or 0) == 0
