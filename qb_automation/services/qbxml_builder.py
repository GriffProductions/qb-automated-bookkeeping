"""Convert a validated ExtractedTransaction into QuickBooks Desktop qbXML.

- ``Invoice`` / ``Mortgage`` / ``Tax`` / ``Insurance`` → ``<BillAddRq>``
- ``Payment`` / ``Bill-and-Payment``                  → ``<CheckAddRq>``
  (a combined doc books the bill side as the check's expense lines)

``unit_class`` maps to ``<ClassRef><FullName>`` on both header and lines.
Header memo → ``<Memo>``; ledger memo → line-item ``<Memo>``.

Vendor / account / class names are constrained to the harvested QB rosters
via :mod:`qb_automation.services.resolvers` so imports are not rejected on
name mismatches.  When no roster is available (unknown company / dev without
data files) the builder falls back to the legacy placeholder.
"""

from __future__ import annotations

import html
from datetime import date

from qb_automation.models.document_schema import ExtractedTransaction, QbXmlPayload
from qb_automation.services import resolvers

_BILL_TYPES = {"Invoice", "Mortgage", "Tax", "Insurance"}

_QBXML_HEAD = '<?xml version="1.0" encoding="utf-8"?>\n<?qbxml version="13.0"?>\n<QBXML>\n<QBXMLMsgsRq onError="stopOnError">\n'
_QBXML_TAIL = "</QBXMLMsgsRq>\n</QBXML>\n"

_LEGACY_ACCOUNT = " Uncategorized Expense "


def _esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def _txn_date(value: date | str) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def validate_for_import(txn: ExtractedTransaction) -> list[str]:
    """Review notes for one transaction (empty = clean import)."""
    key = resolvers.company_key_for_display(txn.company_name)
    if key is None:
        return [f"Company {txn.company_name!r} not in registry — names unvalidated"]
    return resolvers.validation_warnings(key, txn.vendor, txn.doc_type, txn.unit_class)


def build_qbxml(txn: ExtractedTransaction, request_id: int = 1,
                forced_account: str | None = None) -> QbXmlPayload:
    # A check image makes it a Check even when the label says Mortgage/Invoice.
    # Numberless (illegible) checks route the same way — the head just omits N.
    is_bill = txn.doc_type in _BILL_TYPES and not (txn.check_no or txn.check_present)
    request_type = "BillAddRq" if is_bill else "CheckAddRq"
    txn_date = _txn_date(txn.date)
    ref = f"{txn.vendor} {_txn_date(txn.date)} {txn.amount:.2f}".strip()

    key = resolvers.company_key_for_display(txn.company_name)
    if key is None:
        vendor_name, account_name, class_name = txn.vendor, _LEGACY_ACCOUNT, txn.unit_class
    elif forced_account is not None:
        vendor_name, _ = resolvers.resolve_vendor(key, txn.vendor)
        account_name = forced_account  # user-picked from validated options
        class_name = resolvers.resolve_class(key, txn.unit_class)
    else:
        vendor_name, _ = resolvers.resolve_vendor(key, txn.vendor)
        account_name, _ = resolvers.resolve_account(key, txn.vendor, txn.doc_type, txn.unit_class)
        if not account_name:
            account_name = _LEGACY_ACCOUNT
        class_name = resolvers.resolve_class(key, txn.unit_class)

    class_xml = (
        f"<ClassRef><FullName>{_esc(class_name)}</FullName></ClassRef>"
        if class_name
        else ""
    )
    vendor_xml = _esc(vendor_name)
    account_xml = _esc(account_name)
    header_memo = _esc(txn.header_memo or ref)
    line_memo = _esc(txn.ledger_memo or ref)

    if is_bill:
        body = (
            f'<BillAddRq requestID="{request_id}">\n<BillAdd>\n'
            f"<VendorRef><FullName>{vendor_xml}</FullName></VendorRef>\n"
            f"<TxnDate>{txn_date}</TxnDate>\n"
            f"<RefNumber>{_esc(ref[:20])}</RefNumber>\n"
            f"<Memo>{header_memo}</Memo>\n"
            f"{class_xml}\n"
            "<ExpenseLineAdd>\n"
            f"<Amount>{txn.amount:.2f}</Amount>\n"
            f"<Memo>{line_memo}</Memo>\n"
            f"{class_xml}\n"
            f"<AccountRef><FullName>{account_xml}</FullName></AccountRef>\n"
            "</ExpenseLineAdd>\n"
            "</BillAdd>\n</BillAddRq>\n"
        )
    else:
        body = (
            f'<CheckAddRq requestID="{request_id}">\n<CheckAdd>\n'
            f"<PayeeEntityRef><FullName>{vendor_xml}</FullName></PayeeEntityRef>\n"
            f"<TxnDate>{txn_date}</TxnDate>\n"
            f"<RefNumber>{_esc(ref[:20])}</RefNumber>\n"
            f"<Memo>{header_memo}</Memo>\n"
            f"{class_xml}\n"
            "<ExpenseLineAdd>\n"
            f"<Amount>{txn.amount:.2f}</Amount>\n"
            f"<Memo>{line_memo}</Memo>\n"
            f"{class_xml}\n"
            f"<AccountRef><FullName>{account_xml}</FullName></AccountRef>\n"
            "</ExpenseLineAdd>\n"
            "</CheckAdd>\n</CheckAddRq>\n"
        )
    return QbXmlPayload(
        company_name=txn.company_name, qbxml=_QBXML_HEAD + body + _QBXML_TAIL, request_type=request_type
    )


def build_qbxml_batch(txns: list[ExtractedTransaction]) -> QbXmlPayload | None:
    """Combine multiple transactions into one QBXMLMsgsRq envelope (bill+check mix).

    Returns a single QbXmlPayload with request_type of the first txn; individual
    request envelopes are concatenated inside one <QBXMLMsgsRq>.  Returns None
    for an empty list.
    """
    if not txns:
        return None
    bodies: list[str] = []
    for i, txn in enumerate(txns, start=1):
        payload = build_qbxml(txn, request_id=i)
        inner = payload.qbxml.split("<QBXMLMsgsRq onError=\"stopOnError\">", 1)[1]
        inner = inner.rsplit("</QBXMLMsgsRq>", 1)[0]
        bodies.append(inner)
    combined = _QBXML_HEAD + "".join(bodies) + _QBXML_TAIL
    return QbXmlPayload(
        company_name=txns[0].company_name, qbxml=combined, request_type=build_qbxml(txns[0]).request_type
    )
