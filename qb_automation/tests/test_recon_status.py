"""Tests for read-only recon_status builders/parsers. No live QB needed."""

from qb_automation.services.recon_status import (
    build_bank_account_query,
    build_txn_query,
    parse_bank_accounts,
    parse_billpay_response,
    parse_check_response,
    parse_deposit_response,
)

ACCT_RESPONSE = """<?xml version="1.0"?>
<QBXML><QBXMLMsgsRs><AccountQueryRs requestID="r" statusCode="0">
<AccountRet><ListID>A-1</ListID><Name>Chase 1630</Name>
<FullName>Chase 1630</FullName><AccountType>Bank</AccountType>
<Balance>1234.56</Balance></AccountRet>
<AccountRet><ListID>A-2</ListID><Name>Rent Income</Name>
<FullName>Rent Income</FullName><AccountType>Income</AccountType></AccountRet>
</AccountQueryRs></QBXMLMsgsRs></QBXML>"""

CHECK_RESPONSE = """<?xml version="1.0"?>
<QBXML><QBXMLMsgsRs><CheckQueryRs requestID="r" statusCode="0">
<CheckRet><TxnID>T-1</TxnID>
<AccountRef><ListID>A-1</ListID><FullName>Chase 1630</FullName></AccountRef>
<PayeeEntityRef><ListID>V-1</ListID><FullName>Mr. Cooper</FullName></PayeeEntityRef>
<RefNumber>Online</RefNumber><TxnDate>2026-09-01</TxnDate>
<Amount>2000.00</Amount></CheckRet>
</CheckQueryRs></QBXMLMsgsRs></QBXML>"""

DEPOSIT_RESPONSE = """<?xml version="1.0"?>
<QBXML><QBXMLMsgsRs><DepositQueryRs requestID="r" statusCode="0">
<DepositRet><TxnID>D-1</TxnID>
<AccountRef><ListID>A-1</ListID><FullName>Chase 1630</FullName></AccountRef>
<RefNumber>DEP-9</RefNumber><TxnDate>2026-09-05</TxnDate>
<DepositLineRet><PaymentMethodRef><FullName>Check</FullName></PaymentMethodRef>
<Amount>3100.00</Amount></DepositLineRet>
<DepositLineRet><Amount>200.00</Amount></DepositLineRet>
</DepositRet>
</DepositQueryRs></QBXMLMsgsRs></QBXML>"""


def test_queries_are_read_only():
    for xml in (build_bank_account_query(),
                build_txn_query("Check", "A-1", "2026-05-28"),
                build_txn_query("Deposit", "A-1", "2026-05-28"),
                build_txn_query("BillPaymentCheck", "A-1", "2026-05-28")):
        assert "QueryRq" in xml
        for token in ("AddRq", "ModRq", "DelRq", "VoidRq"):
            assert token not in xml


def test_bank_filter_and_balance():
    accts = parse_bank_accounts(ACCT_RESPONSE)
    assert len(accts) == 1  # income account excluded
    assert accts[0].name == "Chase 1630"
    assert accts[0].balance == 1234.56


def test_check_parse():
    txns = parse_check_response(CHECK_RESPONSE)
    assert len(txns) == 1
    assert (txns[0].payee, txns[0].amount, txns[0].ref) == ("Mr. Cooper", 2000.00, "Online")


def test_deposit_amount_sums_lines():
    # DepositRet carries line Amounts, not a header Amount.
    txns = parse_deposit_response(DEPOSIT_RESPONSE)
    assert len(txns) == 1
    assert txns[0].txn_date == "2026-09-05"
    assert txns[0].amount == 3300.00
