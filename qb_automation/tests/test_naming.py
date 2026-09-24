"""Tests for em-dash canonical naming (naming.py)."""

from qb_automation.services.naming import (
    SEP,
    build_filename,
    filenames_equal,
    normalize_separators,
    sanitize_segment,
    split_segments,
)


def test_sep_is_em_dash():
    assert SEP == " — "


def test_normalize_folds_variants():
    assert normalize_separators("a - b") == f"a{SEP}b"
    assert normalize_separators("a – b") == f"a{SEP}b"
    assert normalize_separators("a — b") == f"a{SEP}b"
    # Intra-token hyphens (dates) untouched
    assert normalize_separators("2026-07-01 Invoice") == "2026-07-01 Invoice"


def test_build_uses_em_dash_and_skips_empties():
    name = build_filename("2026-07-01 Invoice", None, "Burrtec", "", "115.65", "Victoria 120")
    assert name == "2026-07-01 Invoice — Burrtec — 115.65 — Victoria 120"
    assert " - " not in name


def test_sanitize_strips_banned_but_keeps_em_dash():
    assert sanitize_segment('a<b>:"c — d"') == "abc — d"


def test_split_round_trip():
    original = "2026-07-01 Invoice N1160151767 — Burrtec — 115.65 — Victoria 120.pdf"
    assert split_segments(original) == [
        "2026-07-01 Invoice N1160151767", "Burrtec", "115.65", "Victoria 120",
    ]


def test_filenames_equal_ignores_separator_variant():
    a = "2026-07-01 Invoice — Burrtec — 115.65 — Victoria 120.pdf"
    b = "2026-07-01 Invoice - Burrtec - 115.65 - Victoria 120.pdf"
    assert filenames_equal(a, b)
    assert not filenames_equal(a, "2026-07-02 Invoice — Burrtec — 115.65 — Victoria 120.pdf")
