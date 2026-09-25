"""Path constants and GCP/API settings.

Em-dash note: the Dropbox/Drive root folder names contain a Unicode em dash
(U+2014).  On some Windows/PowerShell configurations the character is
mangled, so roots are resolved with a glob (``Company*Real Estate``) instead
of a hardcoded literal.  Explicit env overrides always win.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("QB_DATA_DIR", str(PROJECT_ROOT / "data")))
HISTORICAL_INDEX_PATH = Path(
    os.getenv("QB_HISTORICAL_INDEX", str(DATA_DIR / "historical_index.json"))
)
STAGING_DIR = Path(os.getenv("QB_STAGING_DIR", str(DATA_DIR / "staged")))
QBXML_OUT_DIR = Path(os.getenv("QB_QBXML_DIR", str(DATA_DIR / "qbxml")))

for _d in (DATA_DIR, STAGING_DIR, QBXML_OUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _resolve_root(pattern: str, env_key: str, fallback: str) -> Path:
    override = os.getenv(env_key)
    if override:
        return Path(override)
    hits = glob.glob(pattern)
    if hits:
        return Path(hits[0])
    return Path(fallback)


REAL_ESTATE_ROOT: Path = _resolve_root(
    r"C:\Users\user\Dropbox\Company*Real Estate",
    "QB_REAL_ESTATE_ROOT",
    r"C:\Users\user\Dropbox\Company Documentation & Financial Backups — Real Estate",
)
DIALYSIS_ROOT: Path = _resolve_root(
    r"G:\My Drive\Abdeen*\Company*Dialysis",
    "QB_DIALYSIS_ROOT",
    r"G:\My Drive\Abdeen & Griffith\Company Documentation & Financial Backups — Dialysis",
)

WORKBENCH_DIRNAME_FRAGMENTS = ("Workbench",)
BACKUP_DIRNAME_FRAGMENTS = ("QuickBooks Backups",)

# --- QuickBooks Desktop company files (live .qbw, OneDrive) ------------------
# Read-only access via the SDK; the harvester never writes here.
QB_COMPANY_ROOT: Path = _resolve_root(
    r"C:\Users\user\OneDrive\Documents\QuickBooks\Company*",
    "QB_COMPANY_ROOT",
    r"C:\Users\user\OneDrive\Documents\QuickBooks\Company Files — QB",
)
CHART_OF_ACCOUNTS_PATH = Path(
    os.getenv("QB_CHART_PATH", str(DATA_DIR / "chart_of_accounts.json"))
)
VENDORS_PATH = Path(
    os.getenv("QB_VENDORS_PATH", str(DATA_DIR / "vendors.json"))
)
CLASSES_PATH = Path(
    os.getenv("QB_CLASSES_PATH", str(DATA_DIR / "classes.json"))
)
# Per-PDF page-rotation memory: {pdf_name: {"rotate": int, "rotate_pages": str}}.
# review-one auto-applies stored specs unless flags override them.
ROTATIONS_PATH = Path(
    os.getenv("QB_ROTATIONS_PATH", str(DATA_DIR / "rotations.json"))
)

# --- GCP / Gemini settings -------------------------------------------------
GCP_PROJECT = os.getenv("GCP_PROJECT", "")
GCP_LOCATION = os.getenv("GCP_LOCATION", "us-central1")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-pro")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")

# Indexer behaviour
PDF_SCAN_RECURSIVE = True
PDF_MAX_PER_COMPANY: int = int(os.getenv("QB_PDF_MAX_PER_COMPANY", "500"))
PDF_MAX_TEXT_CHARS: int = int(os.getenv("QB_PDF_MAX_TEXT_CHARS", "12000"))
FEW_SHOT_K: int = int(os.getenv("QB_FEW_SHOT_K", "5"))
