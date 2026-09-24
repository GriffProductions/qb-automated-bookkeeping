"""Canonical filename conventions — the em dash is load-bearing.

The user separates filename segments with ``' — '`` (space + U+2014 + space):

    2026-07-01 Invoice N1160151767 — Burrtec — 115.65 — Victoria 120.pdf

This module is the single source of truth for that convention:

- :data:`SEP` — the canonical separator.  Builders MUST use it; the heuristic
  extractor, the Gemini system prompt, and the tests all reference it.
- :func:`normalize_separators` — folds variant separators (``' - '``,
  ``' – '`` en dash, ``' — '`` em dash, extra whitespace) to :data:`SEP` for
  comparison/matching.  Intra-token hyphens (``2026-07-01``) are untouched —
  only dash runs surrounded by whitespace are treated as separators.
- :func:`build_filename` — assembles a convention-compliant stem (no extension).
- :func:`split_segments` — parses a filename back into segments.
- :func:`filenames_equal` — separator-insensitive equality for sanity checks.

Windows/PowerShell note: PowerShell 5.1 mangles U+2014 on display and the
``bash`` tool may transliterate it.  All file I/O here goes through Python
with explicit UTF-8, and roots are resolved via glob so no literal ``—`` ever
needs to survive a shell round-trip.  U+2014 is legal on NTFS — it only looks
broken in legacy consoles.
"""

from __future__ import annotations

import re

EM_DASH = "\u2014"
EN_DASH = "\u2013"
SEP = f" {EM_DASH} "

# Any dash run (hyphen, en dash, em dash, multiples) surrounded by whitespace.
_SEP_RUN_RE = re.compile(r"\s+[-\u2013\u2014]+(?:\s+[-\u2013\u2014]+)*\s+")

# Characters illegal on Windows filenames.  Em/en dashes are NOT in this set.
BANNED_CHARS = set('<>:"/\\|?*')
_BANNED_RE = re.compile(r'[<>:"/\\|?*]')


def normalize_separators(name: str) -> str:
    """Fold all spaced dash-variant separators to the canonical ``' — '``."""
    return _SEP_RUN_RE.sub(SEP, name).strip()


def sanitize_segment(segment: str) -> str:
    """Strip illegal chars, collapse whitespace.  Preserves em dashes inside text."""
    cleaned = _BANNED_RE.sub("", segment)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def build_filename(*segments: str | None) -> str:
    """Join non-empty sanitized segments with the canonical separator.

    Returns the stem WITHOUT extension — callers append ``.pdf``.
    """
    parts = [sanitize_segment(s) for s in segments if s and s.strip()]
    if not parts:
        raise ValueError("build_filename needs at least one non-empty segment")
    return SEP.join(parts)


def split_segments(filename: str) -> list[str]:
    """Parse ``name.pdf`` (or bare stem) into segments, separator-insensitive."""
    stem = filename[:-4] if filename.lower().endswith(".pdf") else filename
    return [s.strip() for s in normalize_separators(stem).split(SEP)]


def filenames_equal(a: str, b: str) -> bool:
    """True when two filenames match after separator normalization + casefold."""
    norm = lambda s: normalize_separators(s[:-4] if s.lower().endswith(".pdf") else s).casefold()
    return norm(a) == norm(b)


def amount_segment(amount: float) -> str:
    return f"{amount:.2f}"
