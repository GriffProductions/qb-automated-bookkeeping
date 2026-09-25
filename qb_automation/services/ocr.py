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

# Mean Tesseract word-confidence below this retries other orientations.
# Garbage OCR from a sideways page scores ~10-35; readable text scores 70+.
ORIENT_MIN_CONF = 50.0


def _ocr_text_and_conf(bitmap, lang: str = "eng") -> tuple[str, float]:
    """One OCR pass returning ``(cleaned_text, mean_word_confidence)``."""
    import pytesseract

    try:
        data = pytesseract.image_to_data(
            bitmap, lang=lang, output_type=pytesseract.Output.DICT)
        confs = [float(c) for c in data["conf"] if float(c) >= 0]
        conf = sum(confs) / len(confs) if confs else 0.0
        text = " ".join(w for w in data["text"]
                        if isinstance(w, str) and w.strip())
    except Exception:  # noqa: BLE001 - fall back to plain string OCR
        text = pytesseract.image_to_string(bitmap, lang=lang) or ""
        conf = float(_alpha_score(text) > 0) * ORIENT_MIN_CONF
    return re.sub(r"\s+", " ", text).strip(), conf


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
    pages = ocr_pages_to_text(pdf_path, list(range(max_pages)), dpi=dpi, lang=lang)
    ordered = [pages[i] for i in sorted(pages)]
    return ("\n".join(t for t in ordered if t).strip(), len(ordered))


def ocr_pages_to_text(
    pdf_path: Path,
    pages: list[int],
    dpi: int = 200,
    lang: str = "eng",
    orientations: dict[int, int] | None = None,
) -> dict[int, str]:
    """OCR selected 0-based pages.  Returns ``{pageno: text}`` (missing = failed).

    Each page is auto-oriented by confidence (sideways check scans recover).
    When ``orientations`` is given it is filled with the winning angle per
    OCR'd page (0 = already upright) for downstream straightening.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RuntimeError("OCR needs pypdfium2 (pip install pypdfium2)") from exc
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("OCR needs pytesseract+Pillow (pip install pytesseract pillow)") from exc
    import shutil

    if shutil.which("tesseract") is None:
        raise RuntimeError("Tesseract binary not found on PATH")

    scale = dpi / 72.0
    out: dict[int, str] = {}
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        count = len(pdf)
        for i in pages:
            if i < 0 or i >= count:
                continue
            bitmap = pdf[i].render(scale=scale).to_pil()
            if bitmap.mode != "RGB":
                bitmap = bitmap.convert("RGB")
            page_text, angle = _ocr_best_orientation_angle(bitmap, lang=lang)
            if page_text:
                out[i] = page_text
            if orientations is not None:
                orientations[i] = angle
    finally:
        pdf.close()
    return out


def _alpha_score(s: str) -> int:
    return sum(1 for ch in s if ch.isalpha())


def _ocr_best_orientation(bitmap, lang: str = "eng") -> str:
    """OCR upright first; retry rotated when confidence says garbage.

    Sideways check scans yield thousands of garbage *letters* (so the old
    alpha-count trigger never fired) but very low mean word-confidence.
    We retry 90/180/270 and keep the most confident result — typically the
    difference between an unreadable check and "Check 2672 9/19/2026".
    Costs extra passes only when the upright read scores poorly.
    """
    return _ocr_best_orientation_angle(bitmap, lang=lang)[0]


def _ocr_best_orientation_angle(bitmap, lang: str = "eng") -> tuple[str, int]:
    """Like :func:`_ocr_best_orientation` but also returns the winning angle
    (0/90/180/270) so callers can straighten the page for human viewing."""
    best, best_conf = _ocr_text_and_conf(bitmap, lang=lang)
    best_angle = 0
    if best_conf >= ORIENT_MIN_CONF:
        return best, best_angle
    for angle in (90, 180, 270):
        try:
            rotated = bitmap.rotate(angle, expand=True)
            candidate, conf = _ocr_text_and_conf(rotated, lang=lang)
        except Exception:  # noqa: BLE001 - rotation/OCR best-effort
            continue
        if conf > best_conf:
            best, best_conf, best_angle = candidate, conf, angle
    return best, best_angle


def pil_to_pdf_angle(pil_ccw_degrees: int) -> int:
    """Convert a PIL counter-clockwise angle to PDF clockwise /Rotate degrees."""
    return (-pil_ccw_degrees) % 360


def apply_orientations(src: Path, dst: Path, orientations: dict[int, int]) -> list[int]:
    """Write ``src`` to ``dst`` with PDF /Rotate flags per 0-based page.

    ``orientations`` holds PIL counter-clockwise angles (as recorded during
    auto-orient); conversion to clockwise /Rotate is handled here.  Rotation
    is metadata-only (lossless).  Returns the 1-based pages straightened.
    """
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(src))
    writer = PdfWriter()
    straightened: list[int] = []
    for i, page in enumerate(reader.pages):
        deg = pil_to_pdf_angle(orientations.get(i, 0))
        if deg:
            writer.add_page(page.rotate(deg))
            straightened.append(i + 1)
        else:
            writer.add_page(page)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "wb") as fh:
        writer.write(fh)
    return straightened
