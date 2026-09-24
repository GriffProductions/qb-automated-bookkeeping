"""Tests for the historical PDF indexer (offline, tmp dirs)."""

from pathlib import Path

from qb_automation.config import settings
from qb_automation.services import pdf_indexer


def _make_pdf(path: Path, text: str) -> None:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    # Minimal text PDF: pypdf writer with a single content stream is not parsed
    # by pdfplumber, so instead write a reportlab-free raw PDF if available.
    try:
        from reportlab.pdfgen import canvas  # type: ignore

        c = canvas.Canvas(str(path))
        c.drawString(72, 720, text)
        c.save()
        return
    except ImportError:
        pass
    # Fallback: plain-text file with .pdf suffix; extractor returns "" -> skipped.
    path.write_text(text, encoding="utf-8")


def test_extract_text_missing_file_returns_empty():
    assert pdf_indexer.extract_text_from_pdf(Path("no-such-file.pdf")) == ""


def test_build_index_writes_json(tmp_path: Path, monkeypatch):
    # Redirect roots to tmp dirs with one fake company folder
    re_root = tmp_path / "RE"
    co_dir = re_root / "Valencia - Victoria 120"
    co_dir.mkdir(parents=True)
    pdf = co_dir / "2025-01-01 Invoice N115530227 - Burrtec - 89.61 - Victoria 120.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    out = tmp_path / "historical_index.json"
    monkeypatch.setattr(settings, "REAL_ESTATE_ROOT", re_root)
    monkeypatch.setattr(settings, "DIALYSIS_ROOT", tmp_path / "DIA_MISSING")
    index = pdf_indexer.build_index(output_path=out, limit_per_company=5)
    assert out.exists()
    assert isinstance(index.entries, list)
