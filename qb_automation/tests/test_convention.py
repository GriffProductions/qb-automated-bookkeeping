"""Tests for the learned filename grammar (convention.py)."""

from qb_automation.services.convention import parse_filename, verify_conformance


def test_parse_full_transaction_name():
    p = parse_filename("2026-07-01 Invoice N1160151767 — Burrtec — 115.65 — Victoria 120.pdf")
    assert p.date == "2026-07-01" and not p.month_only
    assert p.vendor == "Burrtec"
    assert p.amount == 115.65
    assert p.tag == "Victoria 120"


def test_parse_statement_without_amount():
    p = parse_filename("2026-07-28 Owner Statement — Peace Realty — Lenox Crest.pdf")
    assert p.date == "2026-07-28"
    assert p.amount is None
    assert p.vendor == "Peace Realty"
    assert p.tag == "Lenox Crest"


def test_parse_month_only_and_unit():
    p = parse_filename("2026-08 Payment Confirmation — Daniel Stegall — Unit 200 — 106.00 — Del Monte 200201.pdf")
    assert p.date == "2026-08"
    assert p.month_only
    assert p.unit == "Unit 200"


def test_parse_negative_amount():
    p = parse_filename("2026-07-10 HOA Statement — Buena Vida — -1539.00 — Granada Circle.pdf")
    assert p.amount == -1539.00


def test_verify_accepts_canonical_rejects_hyphen():
    good = "2026-07-01 Invoice N1160151767 — Burrtec — 115.65 — Victoria 120.pdf"
    assert verify_conformance(good) == []
    bad = "2026-07-01 Invoice N1160151767 - Burrtec - 115.65 - Victoria 120.pdf"
    assert any("—" in i for i in verify_conformance(bad))


def test_verify_flags_missing_date_and_tag():
    issues = verify_conformance("Random scan.pdf", known_tags=["ARC"])
    assert any("date" in i for i in issues)
