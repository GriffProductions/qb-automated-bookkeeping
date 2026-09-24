"""OCR fallback for scanned / image-only PDFs.

Pipeline order in :func:`pdf_indexer.extract_text_from_pdf`:
pdfplumber → pypdf → (this module) Tesseract OCR → ``""``.

Rendering uses ``pypdfium2`` (already a pdfplumber dependency — no poppler
needed on Windows); recognition uses the system Tesseract binary
(5.5 confirmed on this machine) via ``pytesseract``.  Both are optional:
if either import/binary is missing we log once and return ``""`` so the
pipeline degrades to "flag for manual review" instead of crashing.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

MIN_TEXT_CHARS = 100  # below this a PDF counts as "needs OCR"


def needs_ocr(text: str, min_chars: int = MIN_TEXT_CHARS) -> bool:
    return len((text or "").strip()) < min_chars


def tesseract_available() -> bool:
    try:
        import pytesseract  # noqa: F401

        import shutil

        if shutil.which("tesseract") is None:
            return False
        pytesseract.get_tesseract_version()
        return True
    except Exception:  # noqa: BLE001
        return False


def ocr_pdf_to_text(
    pdf_path: Path,
    max_pages: int = 4,
    dpi: int = 200,
    lang: str = "eng",
) -> tuple[str, int]:
    """OCR the first ``max_pages`` pages.  Returns ``(text, pages_ocrd)``.

    Raises:
        FileNotFoundError: if ``pdf_path`` does not exist.
        RuntimeError: if the OCR stack (pypdfium2/pytesseract/binary) is missing.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RuntimeError("OCR needs pypdfium2 (pip install pypdfium2)") from exc
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("OCR needs pytesseract+Pillow (pip install pytesseract pillow)") from exc
    import shutil

    if shutil.which("tesseract") is None:
        raise RuntimeError("Tesseract binary not found on PATH")

    scale = dpi / 72.0
    text_parts: list[str] = []
    pages_done = 0
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        for i, page in enumerate(pdf):
            if i >= max_pages:
                break
            bitmap = page.render(scale=scale).to_pil()
            if bitmap.mode != "RGB":
                bitmap = bitmap.convert("RGB")
            page_text = pytesseract.image_to_string(bitmap, lang=lang) or ""
            page_text = re.sub(r"\s+", " ", page_text).strip()
            if page_text:
                text_parts.append(page_text)
            pages_done += 1
    finally:
        pdf.close()
    return (" ".join(text_parts).strip(), pages_done)
