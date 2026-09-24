"""Tests for the OCR fallback (ocr.py)."""

import glob
import os

import pytest

from qb_automation.services.ocr import needs_ocr, ocr_pdf_to_text, tesseract_available


def test_needs_ocr_threshold():
    assert needs_ocr("")
    assert needs_ocr("   ")
    assert needs_ocr("x" * 50)
    assert not needs_ocr("x" * 500)


def test_ocr_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        ocr_pdf_to_text("no-such-file.pdf")


def test_ocr_scanned_workbench_pdf():
    """Integration: the Buena Vida HOA scan yields no embedded text — OCR must recover it."""
    if not tesseract_available():
        pytest.skip("tesseract stack not installed")
    roots = glob.glob(r"C:\Users\user\Dropbox\Company*Real Estate")
    if not roots:
        pytest.skip("storage root unavailable")
    cands = list(__import__("pathlib").Path(roots[0]).rglob("*Buena Vida*.pdf"))
    if not cands:
        pytest.skip("sample scan not found")
    text, pages = ocr_pdf_to_text(cands[0], max_pages=2)
    assert pages >= 1
    assert len(text) >= 50  # recovered real words, not empty
