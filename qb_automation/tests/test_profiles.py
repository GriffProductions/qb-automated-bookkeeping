"""Tests for per-company naming profiles (company_profile.py)."""

from qb_automation.services.company_profile import (
    build_profiles,
    format_profile_for_prompt,
    load_profiles,
    save_profiles,
)

CORPUS = [
    ("arc", "2026-06-30 Invoice 548 for Laurel Canyon Dialysis — June billing — 24243.90 — ARC", "f1"),
    ("arc", "2026-07-31 Invoice 556 For LCD — GlobalCare Dialysis Consultancy — July billing — 16911.07 — ARC", "f2"),
    ("arc", "2026-08-31 Bank Statement — Chase 6615 — ARC", "f3"),
    ("valencia_victoria_120", "2025-01-01 Invoice N115530227 — Burrtec Waste Industries, Inc. — Q1, 2025 — 89.61 — Victoria 120", "f4"),
    ("valencia_victoria_120", "2025-02-04 Mortgage Statement — Mr. Cooper — 166958.08 — Victoria 120", "f5"),
]


def test_build_profiles_aggregates():
    profiles = build_profiles(CORPUS)
    arc = profiles["arc"]
    assert arc.file_count == 3
    assert arc.tags_seen.get("ARC") == 3
    assert arc.with_amount_rate == round(2 / 3, 2)
    assert "LCD" in arc.units_seen
    vic = profiles["valencia_victoria_120"]
    assert vic.vendors_seen.get("Burrtec Waste Industries, Inc.") == 1
    assert vic.doc_phrases.get("mortgage statement") == 1


def test_prompt_block_contains_context():
    profiles = build_profiles(CORPUS)
    block = format_profile_for_prompt(profiles["arc"])
    assert "American Renal Care" in block
    assert "24243.90" in block or "Invoice 556" in block
    assert " — " in block


def test_save_load_round_trip(tmp_path):
    profiles = build_profiles(CORPUS)
    path = save_profiles(profiles, tmp_path / "profiles.json")
    loaded = load_profiles(path)
    assert loaded["arc"].file_count == 3
    assert loaded["valencia_victoria_120"].tags_seen.get("Victoria 120") == 2
