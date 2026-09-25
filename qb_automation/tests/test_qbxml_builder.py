"""Tests for qbXML builder + schema validation."""

from datetime import date

import pytest

from qb_automation.models.document_schema import ExtractedTransaction
from qb_automation.services import resolvers
from qb_automation.services.qbxml_builder import build_qbxml, validate_for_import
from qb_automation.services.validator import ExtractionValidationError, validate_transaction


@pytest.fixture(autouse=True)
def fixture_rosters(monkeypatch):
    charts = {"valencia_dm_200_201": {"accounts": [
        {"full_name": "Repairs and Maintenance", "account_type": "Expense"},
        {"full_name": "Interest Expense", "account_type": "Expense"},
        {"full_name": "Insurance Expense", "account_type": "Expense"},
    ]}}
    vendors = {"valencia_dm_200_201": {"vendors": [{"name": "Daniel Stegall"}]}}
    classes = {"valencia_dm_200_201": {"classes": [{"full_name": "200"}, {"full_name": "201"}]}}
    monkeypatch.setattr(resolvers, "_cache", lambda: (charts, vendors, classes))


def _txn(doc_type="Invoice", unit="Unit 200") -> ExtractedTransaction:
    return ExtractedTransaction(
        company_name="Valencia - Del Monte 200 & 201",
        date=date(2026, 8, 9),
        vendor="Daniel Stegall",
        amount=106.00,
        doc_type=doc_type,
        unit_class=unit,
        header_memo="AC repairs Unit 200",
        ledger_memo="Invoice 259 air conditioner repairs",
        suggested_filename="2026-08-09 Payment Confirmation - Daniel Stegall - 106.00 - Del Monte 200201",
    )


def test_bill_types_produce_billaddrq():
    for dt in ("Invoice", "Mortgage", "Tax", "Insurance"):
        payload = build_qbxml(_txn(doc_type=dt))
        assert payload.request_type == "BillAddRq"
        assert "<BillAddRq" in payload.qbxml
        # "Unit 200" resolves to the QB class "200".
        assert "<ClassRef><FullName>200</FullName></ClassRef>" in payload.qbxml
        assert "<Memo>AC repairs Unit 200</Memo>" in payload.qbxml
        assert "<Memo>Invoice 259 air conditioner repairs</Memo>" in payload.qbxml


def test_payment_types_produce_checkaddrq():
    for dt in ("Payment", "Bill-and-Payment"):
        payload = build_qbxml(_txn(doc_type=dt))
        assert payload.request_type == "CheckAddRq"
        assert "<CheckAddRq" in payload.qbxml


def test_no_unit_class_omits_classref():
    payload = build_qbxml(_txn(unit=None))
    assert "ClassRef" not in payload.qbxml


def test_validator_rejects_bad_filename():
    with pytest.raises(ExtractionValidationError):
        validate_transaction(
            {
                "company_name": "ARC - American Renal Care",
                "date": "2026-07-20",
                "vendor": "GlobalCare",
                "amount": 53071.48,
                "doc_type": "Invoice",
                "suggested_filename": 'bad/name:with*chars?',
            }
        )


def test_validator_accepts_good_payload():
    txn = validate_transaction(_txn().model_dump(mode="json"))
    assert txn.vendor == "Daniel Stegall"


def test_builder_emits_resolved_names():
    payload = build_qbxml(_txn())
    assert "<VendorRef><FullName>Daniel Stegall</FullName></VendorRef>" in payload.qbxml
    assert "<AccountRef><FullName>Repairs and Maintenance</FullName></AccountRef>" in payload.qbxml
    assert validate_for_import(_txn()) == []


def test_builder_flags_unknown_vendor():
    txn = _txn()
    txn.vendor = "Mystery LLC"
    payload = build_qbxml(txn)
    # Unknown vendor passes through (never silently swapped) but warns.
    assert "<VendorRef><FullName>Mystery LLC</FullName></VendorRef>" in payload.qbxml
    assert any("Mystery LLC" in w for w in validate_for_import(txn))


def test_numberless_check_routes_checkaddrq():
    txn = _txn(doc_type="Insurance")
    txn.check_no = None
    txn.check_present = True
    payload = build_qbxml(txn)
    assert payload.request_type == "CheckAddRq"
    assert "<CheckAddRq" in payload.qbxml
