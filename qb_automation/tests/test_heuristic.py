"""Tests for the fixed heuristic extractor (regression per 2026-09-24 sanity check)."""

from qb_automation.services.convention import verify_conformance
from qb_automation.services.llm_extractor import extract_transaction


def test_taxed_column_is_not_tax():
    t = extract_transaction(
        text="DESCRIPTION TAXED AMOUNT 2.25 % Billing Service Charges TOTAL DUE 24243.90 "
             "INVOICE 548 www.globalcaredialysis.com BILL TO American Renal Care",
        company_key="arc",
        use_llm=False,
    )
    assert t.doc_type == "Invoice"  # was misclassified as Tax via substring "tax"
    assert t.amount == 24243.90     # total, not the 2.25 rate
    assert t.vendor == "GlobalCare"


def test_date_comes_from_filename_not_today():
    t = extract_transaction(
        text="Owner Statement Period 01 Jul 2026-28 Jul 2026 Peace Realty",
        pdf_path="/fake/2026-07-28 Owner Statement — Peace Realty — Lenox Crest.pdf",
        company_key="vegas_lenox_9837",
        use_llm=False,
    )
    assert str(t.date) == "2026-07-28"


def test_mdy_date_in_text():
    t = extract_transaction(
        text="Statement date 07/31/2026 Account ****2154 balance 34561.01",
        company_key="valencia_paz_28754",
        use_llm=False,
    )
    assert str(t.date) == "2026-07-31"


def test_combined_bill_and_payment():
    t = extract_transaction(
        text="BofA Check 2835 Sewer Services Bill amount 91.64 payment confirmed",
        company_key="vegas_ivypoint_1920",
        use_llm=False,
    )
    assert t.doc_type == "Bill-and-Payment"


def test_suggested_filename_uses_em_dash_and_conforms():
    t = extract_transaction(
        text="Invoice N1160151767 Burrtec Waste total 115.65 Unit 200",
        pdf_path="/fake/2026-07-01 Invoice N1160151767 — Burrtec — Victoria.pdf",
        company_key="valencia_dm_200_201",
        use_llm=False,
    )
    assert " — " in t.suggested_filename
    assert " - " not in t.suggested_filename.replace("2026-07-01", "")
    assert verify_conformance(t.suggested_filename + ".pdf") == []


def test_facility_abbreviation_spelled_out_as_vendor():
    # Facility as the acting vendor -> full name, never the abbreviation.
    t = extract_transaction(
        text="Invoice for management fees from Healthcare Investment Properties "
             "amount 70000.00 due upon receipt",
        company_key="arc",
        use_llm=False,
    )
    assert t.vendor == "Healthcare Investment Properties"


def test_letterhead_vendor_beats_bill_to_block():
    # Earliest mention (letterhead) wins over the BILL TO customer block.
    t = extract_transaction(
        text="Los Angeles DWP bill Sep 9 2026 amount 616.05 "
             "Healthcare Investment Properties LLC c/o Sylmar",
        company_key="sip",
        use_llm=False,
    )
    assert t.vendor == "LADWP"


def test_short_alias_needs_boundaries():
    # "ship"/"relationship" must not resolve to HIP; "ARC" bounded does.
    t = extract_transaction(
        text="Please ship the relationship paperwork promptly, total 10.00",
        company_key="arc",
        use_llm=False,
    )
    assert t.vendor == "Unknown Vendor"


def test_filename_clinic_wins_multi_clinic_text():
    text = "Collection backup LCD 100.00 NKC 200.00 SCD 300.00 INVOICE 564 total 50.00"
    t = extract_transaction(
        text=text,
        pdf_path="/fake/ARC-LCD Invoice Aug-26.pdf",
        company_key="arc",
        use_llm=False,
    )
    assert t.unit_class == "LCD"
