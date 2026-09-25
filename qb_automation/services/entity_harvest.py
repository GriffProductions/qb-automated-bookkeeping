"""Vendor + Class harvest via QB SDK (READ-ONLY).

Mirrors ``chart_harvest.py``: for each live ``.qbw`` company file, open a
session and emit exactly one query envelope:

- ``VendorQueryRq`` (ActiveOnly) -> ``VendorRet`` records
- ``ClassQueryRq``  (ActiveOnly) -> ``ClassRet`` records

Safety: both builders assert no Add/Mod/Del/Void tokens. Output goes to
``data/vendors.json`` and ``data/classes.json`` — never to company folders.
Unattended run works when each file granted "even if QuickBooks is not
running" (already done for all 26).
"""

from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_MUTATING_TOKENS = ("AddRq", "ModRq", "DelRq", "VoidRq")


def build_vendor_query(qbxml_version: str = "13.0") -> str:
    qbxml = (
        f'<?xml version="1.0" encoding="utf-8"?>\n'
        f'<?qbxml version="{qbxml_version}"?>\n'
        "<QBXML>\n<QBXMLMsgsRq onError=\"stopOnError\">\n"
        '<VendorQueryRq requestID="harvest-vendors-1">\n'
        "<ActiveStatus>ActiveOnly</ActiveStatus>\n"
        "<OwnerID>0</OwnerID>\n"
        "</VendorQueryRq>\n</QBXMLMsgsRq>\n</QBXML>\n"
    )
    for token in _MUTATING_TOKENS:
        assert token not in qbxml, f"vendor query must stay read-only ({token})"
    return qbxml


def build_class_query(qbxml_version: str = "13.0") -> str:
    # NOTE: ClassQueryRq rejects <OwnerID> (parse error 0x80040400 on v13) —
    # unlike Account/Vendor queries. ActiveStatus alone is accepted.
    qbxml = (
        f'<?xml version="1.0" encoding="utf-8"?>\n'
        f'<?qbxml version="{qbxml_version}"?>\n'
        "<QBXML>\n<QBXMLMsgsRq onError=\"stopOnError\">\n"
        '<ClassQueryRq requestID="harvest-classes-1">\n'
        "<ActiveStatus>ActiveOnly</ActiveStatus>\n"
        "</ClassQueryRq>\n</QBXMLMsgsRq>\n</QBXML>\n"
    )
    for token in _MUTATING_TOKENS:
        assert token not in qbxml, f"class query must stay read-only ({token})"
    return qbxml


@dataclass
class QBVendor:
    list_id: str
    name: str
    is_active: bool = True


@dataclass
class QBClass:
    list_id: str
    name: str
    full_name: str
    sublevel: int = 0


@dataclass
class CompanyVendors:
    company_key: str
    company_name: str
    company_file: str
    harvested_at: str = ""
    qbxml_version: str = ""
    vendors: list[QBVendor] = field(default_factory=list)

    @property
    def vendor_names(self) -> list[str]:
        return [v.name for v in self.vendors]


@dataclass
class CompanyClasses:
    company_key: str
    company_name: str
    company_file: str
    harvested_at: str = ""
    qbxml_version: str = ""
    classes: list[QBClass] = field(default_factory=list)

    @property
    def class_names(self) -> list[str]:
        return [c.full_name for c in self.classes]


def parse_vendor_response(response_xml: str) -> list[QBVendor]:
    root = ET.fromstring(response_xml)
    vendors: list[QBVendor] = []
    for ret in root.iter("VendorRet"):
        def text(tag: str) -> str:
            el = ret.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""

        is_active = text("IsActive").lower() != "false"
        vendors.append(QBVendor(
            list_id=text("ListID"),
            name=text("Name"),
            is_active=is_active,
        ))
    return vendors


def parse_class_response(response_xml: str) -> list[QBClass]:
    root = ET.fromstring(response_xml)
    classes: list[QBClass] = []
    for ret in root.iter("ClassRet"):
        def text(tag: str) -> str:
            el = ret.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""

        try:
            sublevel = int(text("Sublevel") or 0)
        except ValueError:
            sublevel = 0
        classes.append(QBClass(
            list_id=text("ListID"),
            name=text("Name"),
            full_name=text("FullName") or text("Name"),
            sublevel=sublevel,
        ))
    return classes


def harvest_vendors(company_key: str, company_name: str, qbw: Path) -> CompanyVendors:
    """One VendorQuery round trip for a single company file."""
    from qb_automation.services.qb_connection import negotiate_version, qb_session

    with qb_session(qbw) as (rp, ticket):
        version = negotiate_version(rp)
        response = str(rp.ProcessRequest(ticket, build_vendor_query(version)))
    return CompanyVendors(
        company_key=company_key,
        company_name=company_name,
        company_file=str(qbw),
        harvested_at=datetime.now(timezone.utc).isoformat(),
        qbxml_version=version,
        vendors=parse_vendor_response(response),
    )


def harvest_classes(company_key: str, company_name: str, qbw: Path) -> CompanyClasses:
    """One ClassQuery round trip for a single company file."""
    from qb_automation.services.qb_connection import negotiate_version, qb_session

    with qb_session(qbw) as (rp, ticket):
        version = negotiate_version(rp)
        response = str(rp.ProcessRequest(ticket, build_class_query(version)))
    return CompanyClasses(
        company_key=company_key,
        company_name=company_name,
        company_file=str(qbw),
        harvested_at=datetime.now(timezone.utc).isoformat(),
        qbxml_version=version,
        classes=parse_class_response(response),
    )


def load_entities(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def merge_entities(existing: dict, new_items: list) -> dict:
    for c in new_items:
        existing[c.company_key] = asdict(c)
    return existing
