"""Pydantic schema for historical PDF pattern entries (few-shot memory)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class HistoricalEntry(BaseModel):
    company_key: str = Field(..., description="Stable company key from company_registry")
    company_name: str = Field(...)
    file_name: str = Field(..., description="Actual user-given file name (with .pdf)")
    text_excerpt: str = Field(
        ..., description="Leading characters of extracted PDF text used for pattern learning"
    )
    source_path: str = Field(..., description="Absolute source path the entry was indexed from")
    indexed_at: datetime = Field(default_factory=datetime.utcnow)
    text_chars: int = Field(default=0)
    extraction_method: str = Field(
        default="text", description="How text was obtained: text | ocr | empty"
    )
    vendor_hint: Optional[str] = Field(default=None)
    amount_hint: Optional[float] = Field(default=None)


class HistoricalIndex(BaseModel):
    version: int = 1
    entries: list[HistoricalEntry] = Field(default_factory=list)
