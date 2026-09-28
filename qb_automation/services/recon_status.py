"""Read-only bank activity sweep via QB SDK (no cleared-status available).

Findings from the Masters spike (2026-09-25, qbXML 13.0):
- ``CheckRet``/``DepositRet`` carry NO cleared-status element, and this
  install's processor rejects every report query (GeneralDetail with a
  transaction-detail type → 3110; CustomSummary/CustomDetail → parse error).
  So cleared-vs-reconciled cannot be read programmatically here.
- What CAN be read: per bank account, the dated transaction list
  (Check / Deposit / BillPaymentCheck) with payee, amount, ref.  The latest
  activity date per account is the honest, label-it-as-such proxy for
  "reconciled through".

Output: ``data/recon_status.json`` keyed by company.
"""

from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger(__name__)

_MUTATING_TOKENS = ("AddRq", "ModRq", "DelRq", "VoidRq")
_BANK_TYPES = {"Bank", "Checking", "Savings", "Money Market", "MoneyMarket"}


def _envelope(version: str, inner: str, request_id: str) -> str:
    qbxml = (
        f'<?xml version="1.0" encoding="utf-8"?>\n'
        f'<?qbxml version="{version}"?>\n'
        "<QBXML>\n<QBXMLMsgsRq onError=\"stopOnError\">\n"
        f"{inner}\n</QBXMLMsgsRq>\n</QBXML>\n"
    )
    for token in _MUTATING_TOKENS:
        assert token not in qbxml, f"recon query must stay read-only ({token})"
    return qbxml


def build_bank_account_query(version: str = "13.0") -> str:
    return _envelope(version, (
        '<AccountQueryRq requestID="recon-accts">\n'
        "<ActiveStatus>ActiveOnly</ActiveStatus>\n"
        "<OwnerID>0</OwnerID>\n"
        "</AccountQueryRq>\n"), "recon-accts")


def build_txn_query(kind: str, list_id: str, from_date: str,
                    version: str = "13.0") -> str:
    """``kind`` is Check, Deposit, or BillPaymentCheck."""
    from datetime import date as _date

    assert kind in ("Check", "Deposit", "BillPaymentCheck")
    # qbXML order (verified live): MaxReturned, TxnDateRangeFilter,
    # AccountFilter, IncludeLineItems. Any other order is a parse error.
    # Deposits need line items (no header Amount exists on DepositRet).
    lines = "true" if kind == "Deposit" else "false"
    return _envelope(version, (
        f'<{kind}QueryRq requestID="recon-{kind.lower()}">\n'
        '<MaxReturned>200</MaxReturned>\n'
        '<TxnDateRangeFilter>\n<FromTxnDate>' + from_date + '</FromTxnDate>\n'
        '<ToTxnDate>' + _date.today().isoformat() + '</ToTxnDate>\n</TxnDateRangeFilter>\n'
        '<AccountFilter>\n<ListID>' + list_id + '</ListID>\n</AccountFilter>\n'
        '<IncludeLineItems>' + lines + '</IncludeLineItems>\n'
        f'</{kind}QueryRq>\n'), f'recon-{kind.lower()}')


@dataclass
class QBBankAccount:
    list_id: str
    name: str
    full_name: str
    account_type: str
    balance: float | None = None


@dataclass
class QBActivity:
    txn_type: str
    txn_date: str
    payee: str
    amount: float
    ref: str


@dataclass
class CompanyRecon:
    company_key: str
    company_name: str
    company_file: str
    checked_at: str = ""
    lookback_days: int = 0
    accounts: list[dict] = field(default_factory=list)


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None and el.text else ""


def parse_bank_accounts(response_xml: str) -> list[QBBankAccount]:
    root = ET.fromstring(response_xml)
    out: list[QBBankAccount] = []
    for ret in root.iter("AccountRet"):
        atype = _text(ret.find("AccountType"))
        if atype not in _BANK_TYPES:
            continue
        balance = None
        bal_el = ret.find("Balance")
        if bal_el is not None and (bal_el.text or "").strip():
            try:
                balance = float(bal_el.text.strip())
            except ValueError:
                balance = None
        out.append(QBBankAccount(
            list_id=_text(ret.find("ListID")),
            name=_text(ret.find("Name")),
            full_name=_text(ret.find("FullName")),
            account_type=atype,
            balance=balance,
        ))
    return out


def _parse_txn_list(response_xml: str, ret_tag: str, txn_type: str) -> list[QBActivity]:
    root = ET.fromstring(response_xml)
    out: list[QBActivity] = []
    for ret in root.iter(ret_tag):
        payee = _text(ret.find("PayeeEntityRef/FullName"))
        if not payee:
            payee = _text(ret.find("VendorRef/FullName"))
        raw_amount = _text(ret.find("Amount"))
        if not raw_amount and ret_tag == "DepositRet":
            # Deposits total their DepositLineRet lines, not a header Amount.
            total = 0.0
            for line in ret.iter("DepositLineRet"):
                try:
                    total += float(_text(line.find("Amount")) or "0")
                except ValueError:
                    continue
            raw_amount = str(total)
        try:
            amount = float(raw_amount or "0")
        except ValueError:
            amount = 0.0
        out.append(QBActivity(
            txn_type=txn_type,
            txn_date=_text(ret.find("TxnDate")),
            payee=payee,
            amount=amount,
            ref=_text(ret.find("RefNumber")),
        ))
    return out


def parse_check_response(response_xml: str) -> list[QBActivity]:
    return _parse_txn_list(response_xml, "CheckRet", "Check")


def parse_deposit_response(response_xml: str) -> list[QBActivity]:
    activities = _parse_txn_list(response_xml, "DepositRet", "Deposit")
    # Deposits have no payee; keep the ref (often the deposit slip id).
    return activities


def parse_billpay_response(response_xml: str) -> list[QBActivity]:
    return _parse_txn_list(response_xml, "BillPaymentCheckRet", "BillPay")


def sweep_company(company_key: str, company_name: str, qbw,
                  lookback_days: int = 120, per_account: int = 10) -> CompanyRecon:
    """One headless session: bank accounts + recent activity each.

    Phase logic per (account, kind): last ``lookback_days`` first; only when
    empty, widen (1y, 3y, all-time) until something surfaces.  The first phase
    with hits gives the true newest date without pulling whole histories.
    """
    from qb_automation.services.qb_connection import negotiate_version, qb_session

    today = date.today()
    phases = [today - timedelta(days=lookback_days),
              today - timedelta(days=365),
              today - timedelta(days=365 * 3),
              date(2020, 1, 1)]
    recon = CompanyRecon(company_key=company_key, company_name=company_name,
                         company_file=str(qbw), lookback_days=lookback_days)
    with qb_session(qbw) as (rp, ticket):
        version = negotiate_version(rp)
        accts_xml = str(rp.ProcessRequest(ticket, build_bank_account_query(version)))
        accounts = parse_bank_accounts(accts_xml)
        for acct in accounts:
            activities: list[QBActivity] = []
            wide = False
            for kind, parser in (("Check", parse_check_response),
                                 ("Deposit", parse_deposit_response),
                                 ("BillPaymentCheck", parse_billpay_response)):
                for phase, start in enumerate(phases):
                    try:
                        xml = str(rp.ProcessRequest(
                            ticket, build_txn_query(kind, acct.list_id,
                                                    start.isoformat(), version)))
                    except Exception as exc:  # noqa: BLE001 - one bad kind skips
                        log.debug("recon %s %s skipped: %s", company_key, kind, exc)
                        break
                    found = parser(xml)
                    if found:
                        activities.extend(found)
                        if phase > 0:
                            wide = True
                        break
            activities.sort(key=lambda a: (a.txn_date or "", a.txn_type), reverse=True)
            recent = activities[:per_account]
            dates = [a.txn_date for a in activities if a.txn_date]
            recon.accounts.append({
                "name": acct.name,
                "full_name": acct.full_name,
                "type": acct.account_type,
                "balance": acct.balance,
                "txn_count": len(activities),
                "beyond_lookback": wide and not any(
                    a.txn_date >= (today - timedelta(days=lookback_days)).isoformat()
                    for a in activities if a.txn_date),
                "newest_txn_date": max(dates) if dates else None,
                "oldest_txn_date": min(dates) if dates else None,
                "recent": [asdict(a) for a in recent],
            })
    recon.checked_at = datetime.now(timezone.utc).isoformat()
    return recon


def load_status(path) -> dict:
    from pathlib import Path as _Path
    path = _Path(path)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))
