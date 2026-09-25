"""Tests for the fixed heuristic extractor (regression per 2026-09-24 sanity check)."""

from qb_automation.services.convention import verify_conformance
from qb_automation.services.llm_extractor import _heuristic_extract, extract_transaction


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


def test_arc_outgoing_to_spelled_out_clinic():
    text=("American Renal Care, LLC INVOICE 23421 Lyons Avenue INVOICE # 112 "
          "DATE: SEPTEMBER 13, 2026 TO: Northridge Kidney Center, LLC "
          "Due to Manager (10% of AUG 2026 collections) 8749.05")
    t = extract_transaction(text=text, company_key="arc", use_llm=False)
    assert t.date.isoformat() == "2026-09-13"
    assert t.vendor == "Northridge Kidney Center"
    assert "Invoice 112 to Northridge Kidney Center" in t.suggested_filename
    assert t.suggested_filename.endswith("ARC")
    assert " — Northridge Kidney Center — " not in t.suggested_filename.replace(
        "Invoice 112 to Northridge Kidney Center", "")


def test_globalcare_unit_spelled_out_not_code():
    text=("INVOICE DATE 8/31/2026 INVOICE # 565 www.globalcaredialysis.com "
          "Billing Service Charges on NKC collections TOTAL DUE 11941.08")
    t = extract_transaction(
        text=text, pdf_path="/fake/ARC-NKC Invoice Aug-26.pdf",
        company_key="arc", use_llm=False)
    assert t.vendor == "GlobalCare"
    assert t.unit_class == "Northridge Kidney Center"
    assert "GlobalCare — Northridge Kidney Center" in t.suggested_filename


def test_hoa_statement_keeps_statement_head():
    text=("HOA Statement West Creek and West Hills Community Association "
          "enroll in complimentary automatic payments (ACH) Amount Due 394.00 "
          "Statement Date 09/01/2026")
    t = extract_transaction(text=text, company_key="valencia_paz_28754", use_llm=False)
    assert t.doc_type == "Payment"
    assert t.suggested_filename.startswith("2026-09-01 HOA Statement")
    assert t.vendor == "West Creek and West Hills Community Association"
    assert t.amount == 394.00


def test_invoice_date_beats_due_date():
    text=("SCV Water Invoice Date 09/09/2026 Bill Period 07/27/2026 - 08/24/2026 "
          "Invoice 22152302 Total 370.04 Due Date 10/09/2026")
    t = extract_transaction(text=text, company_key="valencia_tiburon_24701", use_llm=False)
    assert t.date.isoformat() == "2026-09-09"
    assert t.doc_type == "Invoice"


def test_payment_date_with_proof_and_head():
    text=("Republic Services Invoice Number 306200895222 Invoice Date 08/20/2026 "
          "Total 63.46 Payment Confirmation Payment Date 09/10/2026 Amount Paid 63.46")
    t = extract_transaction(text=text, company_key="vegas_pompei_10341", use_llm=False)
    assert t.doc_type == "Bill-and-Payment"
    assert t.date.isoformat() == "2026-09-10"
    assert "Payment Confirmation and Invoice 306200895222" in t.suggested_filename
    assert t.vendor == "Republic Services"


def test_check_head_uses_check_date_and_number():
    text=("Bank of America check image Check 2670 Check Date 9/19/2026 "
          "Pay to Las Vegas Valley Water District water bill 91.64 Bill Date 09/09/2026")
    t = extract_transaction(text=text, company_key="vegas_sondrio_1920", use_llm=False)
    assert t.date.isoformat() == "2026-09-19"
    assert t.suggested_filename.startswith("2026-09-19 Check 2670 & Bill")
    assert t.vendor == "Las Vegas Valley Water District"


def test_mortgage_amount_is_outstanding_principal():
    text=("PNC Bank Mortgage Statement Statement Date 08/04/2026 "
          "Outstanding Principal 25,458.72 Amount Due 94.40 Next Due 09/01/2026")
    t = extract_transaction(text=text, company_key="valencia_seco_127", use_llm=False)
    assert t.doc_type == "Mortgage"
    assert t.amount == 25458.72


def test_ladwp_routes_to_sip():
    text=("Los Angeles DWP Bill Date Sep 9 2026 Account 123 total 616.05 "
          "Healthcare Investment Properties LLC")
    t = extract_transaction(text=text, company_key="hip", use_llm=False)
    assert t.vendor == "LADWP"
    assert t.company_name == "SIP - Sylmar Investment Properties"
    assert t.suggested_filename.endswith("SIP")


def test_street_address_number_is_not_invoice_no():
    text=("American Renal Care, LLC INVOICE 23421 Lyons Avenue INVOICE # 118 "
          "DATE: SEPTEMBER 13, 2026 TO: Santa Clarita Dialysis, LLC total 100.00")
    t = extract_transaction(text=text, company_key="arc", use_llm=False)
    assert "Invoice 118 to Santa Clarita Dialysis" in t.suggested_filename
    assert "23421" not in t.suggested_filename


def test_doc_month_date_beats_stale_filename():
    text="State Farm renewal Prepared SEP 02 2026 Premium 1127.00 Amount Due 1139.74"
    t = extract_transaction(
        text=text, pdf_path="/fake/2026-10-30 Insurance Renewal — State Farm — Masters.pdf",
        company_key="valencia_masters_24655", use_llm=False)
    assert t.date.isoformat() == "2026-09-02"


def test_verb_check_is_not_payment_proof():
    text=("HOA Statement West Creek Amount Due 394.00 Invoice Date 09/11/2026 "
          "To check your current balance, make an on-line payment gateway visit.")
    t = extract_transaction(text=text, company_key="valencia_paz_28754", use_llm=False)
    assert t.doc_type == "Payment"
    assert t.suggested_filename.startswith("2026-09-11 HOA Statement")


def test_privacy_policy_footer_is_not_insurance():
    text=("Republic Services Invoice Number 306200895222 Invoice Date 08/20/2026 "
          "Total 63.46 sent under Terms and in accordance with our Privacy Policy. "
          "Payment Confirmation Payment Date 09/10/2026 Amount Paid 63.46")
    t = extract_transaction(text=text, company_key="vegas_pompei_10341", use_llm=False)
    assert t.doc_type == "Bill-and-Payment"
    assert "Invoice 306200895222" in t.suggested_filename


def test_refund_amount_beats_summary_totals():
    text=("STATE WARRANT BANK & CORP TAX REFUND TAX YEAR 2025 Total credits "
          "and payments 53772.00 WARRANT AMOUNT DOLLARS 2790.61 CENTS")
    t = extract_transaction(text=text, company_key="hip", use_llm=False)
    assert t.amount == 2790.61


def test_total_charges_beats_prior_balance():
    text=("Previous Account Balance 616.05 Payment Received Thank you "
          "Total LADWP Charges 594.47 Total New Charges 594.47 THIS IS YOUR BILL")
    t = extract_transaction(text=text, company_key="sip", use_llm=False)
    assert t.amount == 594.47


def test_zero_coupon_amount_skipped():
    text=("SCV Water Invoice Date 09/09/2026 Invoice 22152302 "
          "Total Amount Due Now 184.81 Payment Coupon 0.00 Balance Forward 370.04")
    t = extract_transaction(text=text, company_key="valencia_tiburon_24701", use_llm=False)
    assert t.amount == 184.81
    assert t.date.isoformat() == "2026-09-09"


def test_prepared_date_beats_policy_period():
    text=("State Farm renewal Policy Period OCT 30 2026 to OCT 30 2027 "
          "Prepared SEP 02 2026 Premium 1127.00")
    t = extract_transaction(text=text, company_key="valencia_masters_24655", use_llm=False)
    assert t.date.isoformat() == "2026-09-02"


def test_globalcare_is_invoice_with_invoice_amount():
    text=("INVOICE DATE 8/31/2026 INVOICE # 564 www.globalcaredialysis.com "
          "Billing Service Charges TOTAL DUE 881727.95 "
          "backup collections summary LCD 2000000.00 NKC 3000000.00")
    t = extract_transaction(text=text, company_key="arc", use_llm=False)
    assert t.doc_type == "Invoice"
    assert t.amount == 881727.95  # invoice total, not the backup summary max


def test_ladwp_is_bill_with_utility_memo():
    text=("Los Angeles DWP Bill Date Sep 9 2026 Account 123 total 616.05 "
          "enroll in autopay to pay your bill")
    t = extract_transaction(text=text, company_key="sip", use_llm=False)
    assert t.doc_type == "Invoice"
    # Memo follows the vendor, lowercase preserved (memo-style segment).
    assert "LADWP \u2014 Electricity & water \u2014 616.05" in t.suggested_filename
    assert "Electricity & water" in t.ledger_memo


def test_hoa_vendor_invoice_word_stays_statement():
    text=("West Creek and West Hills Community Association Invoice "
          "Amount Due 394.00 Statement Date 09/01/2026")
    t = extract_transaction(text=text, company_key="valencia_paz_28754", use_llm=False)
    assert t.doc_type == "Payment"
    assert t.suggested_filename.startswith("2026-09-01 HOA Statement")


def test_single_page_invoice_stub_is_not_payment():
    text=("SCV Water Invoice Date 09/09/2026 Invoice 22152302 Total 184.81 "
          "detach and remit stub with your payment, payment due 10/09/2026")
    t = _heuristic_extract(text, "valencia_tiburon_24701", n_pages=1)
    assert t.doc_type == "Invoice"
    strong = text + " Amount Paid 184.81 payment confirmation"
    t2 = _heuristic_extract(strong, "valencia_tiburon_24701", n_pages=1)
    assert t2.doc_type == "Bill-and-Payment"


def test_check_with_renewal_certificate_head():
    text=("State Farm Bank check image Check 2672 Check Date 9/19/2026 "
          "Pay 829.27 homeowners renewal certificate pages attached")
    t = _heuristic_extract(text, "valencia_seco_127", n_pages=3)
    assert t.date.isoformat() == "2026-09-19"
    assert t.suggested_filename.startswith("2026-09-19 Check 2672 & Renewal Certificate")


def test_rotated_check_without_number_still_routes_check():
    text=("State Farm renewal premium 1139.74 back of check image "
          "pay to the order of State Farm")
    t = _heuristic_extract(text, "valencia_masters_24655", n_pages=3)
    assert t.check_no is None
    assert t.check_present is True
    assert "Check &" in t.suggested_filename


def test_check_verb_is_not_check_present():
    text=("HOA Statement West Creek Amount Due 394.00 "
          "To check your current balance visit the payment gateway.")
    t = _heuristic_extract(text, "valencia_paz_28754", n_pages=1)
    assert t.check_present is False
    assert "Check" not in t.suggested_filename.split(" \u2014 ")[0]


def test_spaced_check_digits_with_phone_and_date_decoys():
    # OCR of a check scan: phone, fractional routing date, then spaced number.
    text=("Atioic Checks 1-800-224-7021 11-35/1210 2 6 7 2 OMARAN ABDEEN "
          "24701 TIBURON STREET PAY TO State Farm Bank of America 1139.74")
    t = _heuristic_extract(text, "valencia_masters_24655", n_pages=3)
    assert t.check_no == "2672"
    assert "Check 2672" in t.suggested_filename


def test_check_year_is_not_check_number():
    text="Cole background checks 2024 completed, all clear, total 10.00"
    t = _heuristic_extract(text, "arc", n_pages=1)
    assert t.check_no is None
    assert t.check_present is False


def test_bill_without_invoice_number_heads_as_bill():
    text=("Los Angeles DWP Bill Date Sep 9 2026 Account 123 total 616.05 "
          "THIS IS YOUR BILL pay by due date")
    t = extract_transaction(text=text, company_key="sip", use_llm=False)
    assert t.doc_type == "Invoice"  # routing unchanged: still a bill
    assert t.suggested_filename.startswith("2026-09-09 Bill \u2014 LADWP")
    assert "Invoice" not in t.suggested_filename.split(" \u2014 ")[0]


def test_numbered_bill_keeps_invoice_head():
    text=("SCV Water Invoice Date 09/09/2026 Invoice 22152302 Total 184.81")
    t = extract_transaction(text=text, company_key="valencia_tiburon_24701", use_llm=False)
    assert t.suggested_filename.startswith("2026-09-09 Invoice \u2014 Invoice 22152302")
