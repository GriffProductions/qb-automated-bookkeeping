"""Validation of raw extraction dicts against the Pydantic models."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from qb_automation.models.document_schema import ExtractedTransaction


class ExtractionValidationError(ValueError):
    pass


def validate_transaction(data: dict[str, Any]) -> ExtractedTransaction:
    try:
        return ExtractedTransaction.model_validate(data)
    except ValidationError as exc:
        raise ExtractionValidationError(str(exc)) from exc


def validate_transaction_json(raw_json: str) -> ExtractedTransaction:
    try:
        return ExtractedTransaction.model_validate_json(raw_json)
    except ValidationError as exc:
        raise ExtractionValidationError(str(exc)) from exc
