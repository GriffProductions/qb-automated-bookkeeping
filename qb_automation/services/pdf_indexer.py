"""Scan company subfolders, extract PDF text, build the few-shot memory index.

For every historical PDF found under the Real Estate and Dialysis roots, we
store ``{actual file name, leading text excerpt}`` pairs in
``data/historical_index.json``.  The LLM extractor later loads the top-K
entries for a company as dynamic few-shot examples so generated file names
match the user's conventions.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from qb_automation.config import settings
from qb_automation.config.company_registry import Division, match_folder_to_company
from qb_automation.models.historical_index import HistoricalEntry, HistoricalIndex

log = logging.getLogger(__name__)

_AMOUNT_RE = re.compile(r"(?<![\d.,])-?\$?\s?(\d[\d,]*\.\d{2})(?!\d)")


def extract_text_from_pdf(
    pdf_path: Path, max_chars: int | None = None, enable_ocr: bool = True,
    orientations: dict[int, int] | None = None,
) -> str:
    """Extract text: pdfplumber → pypdf → Tesseract OCR (for scans).

    ``orientations`` is filled with the winning auto-orient angle per OCR'd
    page when given (for downstream page straightening).
    """
    text, _method = extract_text_with_provenance(
        pdf_path, max_chars, enable_ocr, orientations=orientations)
    return text


def extract_text_with_provenance(
    pdf_path: Path, max_chars: int | None = None, enable_ocr: bool = True,
    orientations: dict[int, int] | None = None,
) -> tuple[str, str]:
    """Same as :func:`extract_text_from_pdf` plus the method used.

    Method is one of ``pdfplumber`` | ``pypdf`` | ``ocr`` | ``empty``.
    Pages are handled individually: embedded text wins per page, and only
    image-only pages pay for OCR (with confidence auto-orientation).  This
    catches check images hiding inside mixed PDFs whose other pages carry
    real text (e.g. renewal certificate + sideways check scan).
    """
    from qb_automation.services.ocr import MIN_TEXT_CHARS, needs_ocr, ocr_pages_to_text

    max_chars = max_chars or settings.PDF_MAX_TEXT_CHARS
    text, method = "", "empty"
    try:
        import pdfplumber

        with pdfplumber.open(str(pdf_path)) as pdf:
            plumber_parts = [(p.extract_text() or "") for p in pdf.pages]
        n = len(plumber_parts)
    except Exception as exc:  # noqa: BLE001 - optional dep / scanned PDFs
        log.debug("pdfplumber failed for %s: %s", pdf_path, exc)
        plumber_parts = []
        n = 0
    if not plumber_parts:
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(pdf_path))
            n = len(reader.pages)
            plumber_parts = [""] * n
            pypdf_parts = [(p.extract_text() or "") for p in reader.pages]
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not extract text from %s: %s", pdf_path, exc)
            return "", "empty"
    else:
        pypdf_parts = [""] * n
    if n == 0:
        return "", "empty"

    parts: list[str] = []
    ocr_used = False
    empty_pages: list[int] = []
    for i in range(n):
        page_text = (plumber_parts[i] if i < len(plumber_parts) else "").strip()
        if not page_text and i < len(pypdf_parts):
            page_text = (pypdf_parts[i] or "").strip()
        if needs_ocr(page_text, MIN_TEXT_CHARS):
            empty_pages.append(i)
        parts.append(page_text)
    if empty_pages and enable_ocr:
        try:
            # Cap OCR pages (front-loaded content decides amounts/dates).
            occluded = ocr_pages_to_text(Path(pdf_path), empty_pages[:6],
                                         orientations=orientations)
            for i, ocr_text in occluded.items():
                if len(ocr_text.strip()) > len(parts[i].strip()):
                    parts[i] = ocr_text
                    ocr_used = True
        except Exception as exc:  # noqa: BLE001 - OCR stack optional
            log.debug("OCR fallback failed for %s: %s", pdf_path, exc)
    text = "\n".join(parts).strip()
    if text.strip():
        method = "ocr" if ocr_used else ("pdfplumber" if any(
            p.strip() for p in plumber_parts) else "pypdf")
    else:
        method = "empty"
    # Normalize whitespace but PRESERVE line breaks — downstream amount/date
    # logic depends on line context ("Amount Due $X" lines, ranges, tables).
    lines = [re.sub(r"[ \t\u00a0]+", " ", ln).strip() for ln in text.splitlines()]
    text = "\n".join([ln for ln in lines if ln]).strip()
    if not text:
        method = "empty"
    return text[:max_chars], method


def _is_skipped_dir(path: Path) -> bool:
    name = path.name
    frags = settings.BACKUP_DIRNAME_FRAGMENTS + settings.WORKBENCH_DIRNAME_FRAGMENTS
    lowered = name.lower()
    return any(f.lower().strip(" -") in lowered for f in frags)


def _iter_company_pdfs() -> "list[tuple]":
    """Yield (company, pdf_path) for historical PDFs (workbench excluded)."""
    from qb_automation.config.company_registry import COMPANIES  # local to avoid cycle

    results: list[tuple] = []
    for root in (settings.REAL_ESTATE_ROOT, settings.DIALYSIS_ROOT):
        if not root.exists():
            log.warning("Root does not exist, skipping: %s", root)
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir() or _is_skipped_dir(child):
                continue
            company = match_folder_to_company(child.name)
            if company is None or company.division == Division.UNASSIGNED:
                log.debug("No company match for folder: %s", child)
                continue
            pdfs = sorted(child.rglob("*.pdf")) if settings.PDF_SCAN_RECURSIVE else sorted(child.glob("*.pdf"))
            for pdf in pdfs[: settings.PDF_MAX_PER_COMPANY]:
                results.append((company, pdf))
    return results


def build_index(output_path: Path | None = None, limit_per_company: int | None = None) -> HistoricalIndex:
    """Full scan → HistoricalIndex, persisted to ``data/historical_index.json``."""
    output_path = output_path or settings.HISTORICAL_INDEX_PATH
    limit = limit_per_company or settings.PDF_MAX_PER_COMPANY
    entries: list[HistoricalEntry] = []
    counts: dict[str, int] = {}
    for company, pdf in _iter_company_pdfs():
        if counts.get(company.key, 0) >= limit:
            continue
        text, method = extract_text_with_provenance(pdf)
        if not text:
            log.info("Skipping image-only PDF with no OCR text: %s", pdf.name)
            continue
        m = _AMOUNT_RE.search(text)
        entries.append(
            HistoricalEntry(
                company_key=company.key,
                company_name=company.display_name,
                file_name=pdf.name,
                text_excerpt=text[:2000],
                source_path=str(pdf),
                text_chars=len(text),
                extraction_method=method,
                amount_hint=float(m.group(1).replace(",", "")) if m else None,
            )
        )
        counts[company.key] = counts.get(company.key, 0) + 1
    index = HistoricalIndex(entries=entries)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(index.model_dump(mode="json"), indent=2), encoding="utf-8")
    log.info("Wrote %d historical entries to %s", len(entries), output_path)
    return index


def load_index(path: Path | None = None) -> HistoricalIndex:
    path = path or settings.HISTORICAL_INDEX_PATH
    if not path.exists():
        return HistoricalIndex(entries=[])
    data = json.loads(path.read_text(encoding="utf-8"))
    return HistoricalIndex.model_validate(data)


def relevant_examples(company_key: str, k: int | None = None, path: Path | None = None) -> list[HistoricalEntry]:
    """Return up to K historical entries for a company (most recent first)."""
    k = k or settings.FEW_SHOT_K
    index = load_index(path)
    matches = [e for e in index.entries if e.company_key == company_key]
    return matches[-k:] if matches else []
