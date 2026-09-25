"""Chart-of-accounts harvest via QB SDK AccountQuery (READ-ONLY).

For each live ``.qbw`` company file: open session → ``AccountQueryRq`` →
parse ``AccountRet`` → append to ``data/chart_of_accounts.json``.

Safety:
- The ONLY request this module emits is ``AccountQueryRq``.  A test asserts
  the outgoing XML contains no Add/Mod/Delete/Del/Void tokens.
- Nothing is written to the company folders; output goes to the repo data dir.
- Company-file resolution prefers the most recently modified top-level
  ``.qbw`` per folder (Valencia Paz keeps two generations).
"""

from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_MUTATING_TOKENS = ("AddRq", "ModRq", "DelRq", "VoidRq")


def build_company_query(qbxml_version: str = "13.0") -> str:
    return (
        f'<?xml version="1.0" encoding="utf-8"?>\n'
        f'<?qbxml version="{qbxml_version}"?>\n'
        "<QBXML>\n<QBXMLMsgsRq onError=\"stopOnError\">\n"
        '<CompanyQueryRq requestID="whoami-1">\n'
        "<OwnerID>0</OwnerID>\n"
        "</CompanyQueryRq>\n</QBXMLMsgsRq>\n</QBXML>\n"
    )


def parse_company_name(response_xml: str) -> str:
    """CompanyName (falling back to LegalCompanyName) from a CompanyQueryRs."""
    root = ET.fromstring(response_xml)
    for tag in ("CompanyName", "LegalCompanyName"):
        el = root.find(f".//{tag}")
        if el is not None and el.text and el.text.strip():
            return el.text.strip()
    return ""


def match_open_company(qb_name: str) -> str | None:
    """Resolve a QB-internal company name to a registry key."""
    from qb_automation.config.company_registry import (
        COMPANIES,
        get_company,
        match_qb_folder_to_company,
    )

    hit = match_qb_folder_to_company(qb_name)
    if hit is not None:
        return hit.key
    # Token-overlap fallback ("Valencia Paz, LLC" vs "VALENCIA PAZ LLC").
    import re as _re

    want = set(_re.findall(r"[a-z0-9]+", qb_name.lower())) - {"llc", "inc"}
    best: tuple[int, str] | None = None
    for c in COMPANIES:
        have = set(_re.findall(r"[a-z0-9]+", c.display_name.lower()))
        overlap = len(want & have)
        if overlap and (best is None or overlap > best[0]):
            best = (overlap, c.key)
    return best[1] if best else None


def harvest_open_file() -> CompanyChart:
    """Harvest whichever company file is currently open in QuickBooks.

    Uses BeginSession("") — no path, no file switching, no unattended grant
    needed.  The open file's own CompanyName resolves the registry key.
    """
    from qb_automation.config.company_registry import get_company
    from qb_automation.services.qb_connection import negotiate_version, qb_session

    with qb_session("") as (rp, ticket):
        version = negotiate_version(rp)
        who = str(rp.ProcessRequest(ticket, build_company_query(version)))
        qb_name = parse_company_name(who)
        key = match_open_company(qb_name) if qb_name else None
        if key is None:
            raise QBConnectionError(
                f"Could not map open company {qb_name!r} to the registry.")
        company = get_company(key)
        response = str(rp.ProcessRequest(ticket, build_account_query(version)))
    return CompanyChart(
        company_key=key,
        company_name=company.display_name if company else (qb_name or key),
        company_file=f"<open file: {qb_name}>",
        harvested_at=datetime.now(timezone.utc).isoformat(),
        qbxml_version=version,
        accounts=parse_account_response(response),
    )


class QBConnectionError(RuntimeError):
    pass


def build_account_query(qbxml_version: str = "13.0") -> str:
    qbxml = (
        f'<?xml version="1.0" encoding="utf-8"?>\n'
        f'<?qbxml version="{qbxml_version}"?>\n'
        "<QBXML>\n<QBXMLMsgsRq onError=\"stopOnError\">\n"
        '<AccountQueryRq requestID="harvest-1">\n'
        "<ActiveStatus>ActiveOnly</ActiveStatus>\n"
        "<OwnerID>0</OwnerID>\n"
        "</AccountQueryRq>\n</QBXMLMsgsRq>\n</QBXML>\n"
    )
    for token in _MUTATING_TOKENS:
        assert token not in qbxml, f"harvest query must stay read-only ({token})"
    return qbxml


@dataclass
class QBAccount:
    list_id: str
    name: str
    full_name: str
    account_type: str
    number: str = ""
    sublevel: int = 0
    balance: str = ""


@dataclass
class CompanyChart:
    company_key: str
    company_name: str
    company_file: str
    harvested_at: str = ""
    qbxml_version: str = ""
    accounts: list[QBAccount] = field(default_factory=list)

    @property
    def expenses(self) -> list[QBAccount]:
        return [a for a in self.accounts if a.account_type == "Expense"]

    @property
    def expense_names(self) -> list[str]:
        return [a.full_name for a in self.expenses]


def parse_account_response(response_xml: str) -> list[QBAccount]:
    """Parse an AccountQueryRs envelope into QBAccount records."""
    root = ET.fromstring(response_xml)
    accounts: list[QBAccount] = []
    for ret in root.iter("AccountRet"):
        def text(tag: str) -> str:
            el = ret.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""

        try:
            sublevel = int(text("Sublevel") or 0)
        except ValueError:
            sublevel = 0
        accounts.append(QBAccount(
            list_id=text("ListID"),
            name=text("Name"),
            full_name=text("FullName"),
            account_type=text("AccountType"),
            number=text("AccountNumber"),
            sublevel=sublevel,
            balance=text("Balance"),
        ))
    return accounts


def resolve_company_file(folder: Path) -> Path | None:
    """Newest top-level ``.qbw`` in a company folder (skips backups/subdirs)."""
    candidates = [p for p in folder.glob("*.qbw") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def iter_company_files(qb_root: Path) -> list[tuple[str, Path]]:
    """Map registry companies → live .qbw paths. Returns ``(key, qbw)`` hits."""
    from qb_automation.config.company_registry import COMPANIES, Division

    hits: list[tuple[str, Path]] = []
    if not qb_root.exists():
        return hits
    for child in sorted(qb_root.rglob("*")):
        if not child.is_dir():
            continue
        qbw = resolve_company_file(child)
        if qbw is None:
            continue
        from qb_automation.config.company_registry import match_qb_folder_to_company

        company = match_qb_folder_to_company(child.name)
        if company is None or company.division == Division.UNASSIGNED:
            continue
        # First (shallowest) match per company wins.
        if not any(k == company.key for k, _ in hits):
            hits.append((company.key, qbw))
    return hits


def harvest_company(company_key: str, company_name: str, qbw: Path) -> CompanyChart:
    """Open session → AccountQuery → parsed chart (single round trip)."""
    from qb_automation.services.qb_connection import negotiate_version, qb_session

    with qb_session(qbw) as (rp, ticket):
        version = negotiate_version(rp)
        response = str(rp.ProcessRequest(ticket, build_account_query(version)))
    accounts = parse_account_response(response)
    return CompanyChart(
        company_key=company_key,
        company_name=company_name,
        company_file=str(qbw),
        harvested_at=datetime.now(timezone.utc).isoformat(),
        qbxml_version=version,
        accounts=accounts,
    )


def save_charts(charts: list[CompanyChart], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {c.company_key: asdict(c) for c in charts}
    # Redact ListIDs? No — ListIDs are required later for qbXML Add Mod refs.
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_charts(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def merge_charts(existing: dict, new: list[CompanyChart]) -> dict:
    """Merge harvest into existing file content (per-company overwrite)."""
    for c in new:
        existing[c.company_key] = asdict(c)
    return existing
