"""Your naming grammar, learned from the 176-file workbench corpus (2026-09-24).

Canonical shape::

    DATE DocPhrase [ref/detail] — [PayMethod] — Vendor — [Unit] — [detail] — [amount] — Tag.pdf

Rules observed in your files:
- ``DATE`` is ``YYYY-MM-DD``; monthly statements use ``YYYY-MM``.
- Head ``DocPhrase`` vocabulary: Bank/Owner/HOA/Mortgage Statement, Payment
  Confirmation, Bill, Invoice <num>, Check <num>, ``<Bank> <last4> Payment``,
  LLC Statement Of Information, Cash Flow / General Ledger / Income Statement.
- Middle segments carry vendor, pay-method (``BofA 2265``, ``Visa 4267``,
  ``BoN 0556`` …), unit tags (``Unit 200``) or ARC clinic codes, and detail.
- ``amount`` (``1234.56``, negatives like ``-1539.00`` allowed) sits
  second-to-last when present; statements (bank/owner/ledger) omit it.
- Last segment is always the short company tag (``Tiburon``, ``ARC`` …).
- Segments are joined with ``' — '`` (see :mod:`naming`).

:func:`parse_filename` recovers this structure; :func:`verify_conformance`
scores a candidate name so the pipeline (and tests) can reject
non-conformant suggestions before anything hits disk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from qb_automation.services.naming import normalize_separators, split_segments

DATE_FULL_RE = re.compile(r"^(20\d{2})-(\d{2})-(\d{2})\s+(.*)$")
DATE_MONTH_RE = re.compile(r"^(20\d{2})-(\d{2})\s+(.*)$")
AMOUNT_RE = re.compile(r"^-?[\d,]+\.\d{2}$")
UNIT_RE = re.compile(r"(?i)\bunit\s+(\d+[A-Za-z]?)\b")
CLINIC_RE = re.compile(r"\b(SCD|NKC|LCD|MHD)\b")
PAY_METHOD_RE = re.compile(r"(?i)\b(visa|bofa|bon|chase|f&m|pnc|wells)\s*[\d*]{3,6}\b")
INVOICE_REF_RE = re.compile(r"(?i)\b(invoice|inv|check|bill|policy|cert(?:ificate)?)\s*(?:#|n[o.]*)?\s*([A-Za-z0-9][A-Za-z0-9-]{1,24})")

# Head-phrase vocabulary mapped to the internal doc_type enum.
DOC_PHRASES: dict[str, str] = {
    "bank statement": "Payment",
    "owner statement": "Payment",
    "hoa statement": "Payment",
    "cash flow statement": "Payment",
    "general ledger": "Payment",
    "income statement": "Payment",
    "payment confirmation": "Payment",
    "payment confirmation and invoice": "Bill-and-Payment",
    "payment": "Payment",
    "check": "Bill-and-Payment",
    "bill": "Invoice",
    "invoice": "Invoice",
    "mortgage statement": "Mortgage",
    "tax": "Tax",
    "property tax": "Tax",
    "insurance": "Insurance",
    "renewal certificate": "Insurance",
    "llc statement": "Payment",
}


@dataclass
class ParsedFilename:
    raw: str
    date: str = ""
    month_only: bool = False
    doc_phrase: str = ""
    doc_type_guess: str = "Payment"
    pay_method: str | None = None
    vendor: str | None = None
    unit: str | None = None
    detail: list[str] = field(default_factory=list)
    amount: float | None = None
    tag: str = ""
    segments: list[str] = field(default_factory=list)


def parse_filename(filename: str) -> ParsedFilename:
    """Parse a (candidate or historical) filename into its grammar slots."""
    segs = split_segments(filename)
    p = ParsedFilename(raw=filename, segments=segs)
    if not segs:
        return p
    # Head: date + doc phrase
    head = segs[0]
    rest = head
    m = DATE_FULL_RE.match(head)
    if m:
        p.date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        rest = m.group(4)
    else:
        m2 = DATE_MONTH_RE.match(head)
        if m2:
            p.date = f"{m2.group(1)}-{m2.group(2)}"
            p.month_only = True
            rest = m2.group(3)
    lowered = rest.lower()
    for phrase, dtype in sorted(DOC_PHRASES.items(), key=lambda kv: -len(kv[0])):
        if phrase in lowered:
            p.doc_phrase = phrase
            p.doc_type_guess = dtype
            break
    if not p.doc_phrase:
        p.doc_phrase = rest[:60]
    # Unit/clinic codes often live in the head itself ("Invoice 556 For LCD").
    mu_head = UNIT_RE.search(rest)
    if mu_head:
        p.unit = f"Unit {mu_head.group(1)}"
    else:
        mc_head = CLINIC_RE.search(rest)
        if mc_head:
            p.unit = mc_head.group(1)
    # Tail: company tag
    if len(segs) >= 2:
        p.tag = segs[-1]
    # Middles: amount / unit / pay-method / vendor / detail
    middles = segs[1:-1] if len(segs) >= 3 else []
    for s in middles:
        if AMOUNT_RE.match(s.replace(",", "")) and p.amount is None:
            try:
                p.amount = float(s.replace(",", ""))
                continue
            except ValueError:
                pass
        mu = UNIT_RE.search(s)
        if mu:
            p.unit = f"Unit {mu.group(1)}"
            continue
        mc = CLINIC_RE.search(s)
        if mc and p.unit is None:
            p.unit = mc.group(1)
            continue
        if PAY_METHOD_RE.search(s) and p.pay_method is None:
            p.pay_method = s
            continue
        if p.vendor is None:
            p.vendor = s
        else:
            p.detail.append(s)
    return p


def verify_conformance(filename: str, known_tags: list[str] | None = None) -> list[str]:
    """Return a list of conformance issues; empty means the name follows your grammar."""
    issues: list[str] = []
    stem = filename[:-4] if filename.lower().endswith(".pdf") else filename
    # Separator must be canonical em dash
    if " - " in stem or " – " in stem:
        issues.append("uses '-'/'–' separator instead of canonical ' — '")
    p = parse_filename(filename)
    if not p.date:
        issues.append("missing YYYY-MM-DD (or YYYY-MM) date prefix")
    if not p.doc_phrase:
        issues.append("unrecognized head doc phrase")
    if not p.tag:
        issues.append("missing trailing company tag")
    elif known_tags and not any(p.tag.casefold() == t.casefold() for t in known_tags):
        issues.append(f"tag {p.tag!r} not in company registry")
    if len(p.segments) < 3:
        issues.append("fewer than 3 segments (want DATE-head — … — Tag)")
    return issues


def doc_phrase_for(doc_type: str, statement: bool = False) -> str:
    """Map internal doc_type → the head phrase you would use."""
    mapping = {
        "Payment": "Payment Confirmation",
        "Invoice": "Invoice",
        "Bill-and-Payment": "Payment & Bill",
        "Mortgage": "Mortgage Statement",
        "Tax": "Tax Payment",
        "Insurance": "Insurance Renewal",
    }
    if statement:
        return "Bank Statement"
    return mapping.get(doc_type, doc_type)
