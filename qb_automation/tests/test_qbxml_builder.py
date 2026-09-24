"""Tests for qbXML builder + schema validation."""

from datetime import date

import pytest

from qb_automation.models.document_schema import ExtractedTransaction
from qb_automation.services.qbxml_builder import build_qbxml
from qb_automation.services.validator import ExtractionValidationError, validate_transaction


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
        assert "<ClassRef><FullName>Unit 200</FullName></ClassRef>" in payload.qbxml
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
