"""Pydantic models for the extraction output and qbXML payload."""

from __future__ import annotations

import datetime as _dt
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

DocType = Literal["Payment", "Invoice", "Bill-and-Payment", "Mortgage", "Tax", "Insurance"]


class ExtractedTransaction(BaseModel):
    """Structured metadata for one source document."""

    company_name: str = Field(..., description="Company display name from the registry")
    date: _dt.date = Field(..., description="Transaction/document date (YYYY-MM-DD)")
    vendor: str = Field(..., min_length=1, description="Vendor / payee / issuer")
    amount: float = Field(..., description="Transaction total, positive for charges, negative for refunds/credits")
    doc_type: DocType = Field(...)
    unit_class: Optional[str] = Field(
        default=None,
        description="QuickBooks Class / unit tag, e.g. 'Unit 200', 'SCD', 'LCD'",
    )
    header_memo: str = Field(default="", description="Header-level memo (QB Memo field)")
    ledger_memo: str = Field(default="", description="Line-item memo (QB line Memo field)")
    check_no: Optional[str] = Field(
        default=None,
        description="Check number when the source includes a check image "
        "(routes qbXML to CheckAddRq and builds a 'Check N & …' filename)",
    )
    check_present: bool = Field(
        default=False,
        description="A check image is present but its number is illegible "
        "(e.g. rotated scan) — routes to CheckAddRq with a 'Check & …' head; "
        "supply the number via --set check=N when known",
    )
    lines: list[SplitLine] = Field(
        default_factory=list,
        description="Multi-line splits (LADWP electric/water, mortgage "
        "principal/interest).  Empty = legacy single line: amount books to "
        "the resolved account.",
    )
    doc_ref: Optional[str] = Field(
        default=None,
        description="Document reference for QB RefNumber (check/invoice/policy "
        "number) — the searchable field on import",
    )
    suggested_filename: str = Field(
        ...,
        description="Standardized filename WITHOUT extension, segments joined by "
        "' — ' (em dash), e.g. "
        "'2026-07-01 Invoice N1160151767 — Burrtec — 115.65 — Victoria 120'",
    )

    @field_validator("suggested_filename")
    @classmethod
    def filename_must_be_safe(cls, v: str) -> str:
        bad = set('<>:"/\\|?*')
        if any(ch in bad for ch in v):
            raise ValueError(f"suggested_filename contains illegal characters: {v!r}")
        if not v.strip():
            raise ValueError("suggested_filename must not be blank")
        if len(v) > 200:
            raise ValueError("suggested_filename too long (>200 chars)")
        return v.strip()

    @field_validator("vendor", "company_name")
    @classmethod
    def nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v.strip()


class QbXmlPayload(BaseModel):
    """Validated qbXML request ready to import into QuickBooks Desktop."""

    company_name: str
    qbxml: str
    request_type: Literal["BillAddRq", "CheckAddRq"]


class SplitLine(BaseModel):
    """One expense line of a multi-line Bill/Check.

    ``account`` must be an exact QB FullName (any account type — expense or
    liability); the builder validates it against the company chart and falls
    back with a warning rather than emitting an unknown name.
    """

    account: Optional[str] = Field(
        default=None,
        description="Exact QB FullName (any account type — expense or liability). "
        "None when unresolvable at extraction (e.g. mortgage note not matched); "
        "the builder substitutes suspense and warns rather than emitting blanks.",
    )
    amount: float = Field(...)
    memo: str = Field(default="")
