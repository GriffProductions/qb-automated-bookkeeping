"""QuickBooks Desktop SDK connection manager (qbXMLRP2 COM).

READ-ONLY contract: this module opens sessions and sends caller-supplied
qbXML.  All harvest/query builders live elsewhere and are covered by tests
asserting no Add/Mod/Delete requests are ever generated.

Auth model (first run per company file):
  1. With QuickBooks CLOSED, OpenConnection + BeginSession launches QB and
     raises COM error 0x80040422 ("auth required") OR shows the Allow dialog.
  2. The user clicks Allow.  For unattended harvests they must ALSO tick
     "Allow this application to access QuickBooks even if it is not running"
     (requires logging in as Admin, no password prompt afterwards).
  3. Authorizations persist per company file (Edit → Preferences →
     Integrated Applications), so the batch run only dialogs once per file.

BeginSession file mode 2 (qbFileOpenDoNotCare) never forces single-user.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

APP_ID = "QB Automated Bookkeeping"
APP_NAME = "QB Automated Bookkeeping"

#: Highest qbXML we generate; sessions negotiate down from here.
QBX_MAX = "16.0"


def negotiate_version(rp) -> str:
    """Highest qbXML version both sides support (advertised by the processor)."""
    try:
        advertised = str(rp.QBXMLVersionsForSession()).split(",")
        advertised = sorted((v.strip() for v in advertised if v.strip()),
                            key=lambda v: [int(x) for x in v.split(".")],
                            reverse=True)
        for v in advertised:
            if v <= QBX_MAX:
                return v
        return advertised[-1]
    except Exception:  # noqa: BLE001 - fall back to a safe floor
        return "13.0"

_AUTH_DENIED = "0x80040422"


class QBAuthRequired(RuntimeError):
    """QuickBooks is showing (or needs) the Allow-access dialog."""


class QBConnectionError(RuntimeError):
    pass


def _processor():
    try:
        import win32com.client
    except ImportError as exc:
        raise QBConnectionError(
            "pywin32 is required for the QB SDK channel (pip install pywin32)"
        ) from exc
    try:
        return win32com.client.Dispatch("QBXMLRP2.RequestProcessor")
    except Exception as exc:  # noqa: BLE001 - COM errors surface here
        raise QBConnectionError(f"QBXMLRP2 COM unavailable: {exc}") from exc


def _is_auth_error(exc: Exception) -> bool:
    text = str(exc).lower()
    # Numeric: match the auth HRESULT exactly (0x80040422 == -2147220446).
    # The old "2147220" prefix matched EVERY qbXML error (e.g. parse error
    # -2147220480), mislabeling real failures as auth prompts.
    numeric = ("80040422", "2147220446")
    if any(m in text for m in numeric):
        return True
    markers = (
        "grant permission", "integrated application", "automatic login",
        "allow access", "has not been granted", "authentication",
    )
    return any(m in text for m in markers)


@contextmanager
def qb_session(company_path: str | Path) -> Iterator[tuple[object, str]]:
    """Yield ``(processor, ticket)`` for one company file; always cleans up.

    Usage:
        with qb_session(r"...\\Valencia Paz.qbw") as (rp, ticket):
            rp.ProcessRequest(ticket, query_xml)
    """
    rp = _processor()
    ticket: str | None = None
    try:
        rp.OpenConnection("", APP_NAME)
    except Exception as exc:  # noqa: BLE001
        if _is_auth_error(exc):
            raise QBAuthRequired(
                "QuickBooks wants authorization: click Allow for "
                f"'{APP_NAME}' (tick 'even if QuickBooks is not running'), "
                "then re-run."
            ) from exc
        raise QBConnectionError(f"OpenConnection failed: {exc}") from exc
    try:
        ticket = rp.BeginSession(str(company_path), 2)
        log.info("Session on %s", company_path)
        yield rp, ticket
    except Exception as exc:  # noqa: BLE001
        if _is_auth_error(exc):
            raise QBAuthRequired(
                f"QuickBooks wants authorization for {company_path}: "
                f"click Allow for '{APP_NAME}' (tick 'even if QuickBooks "
                "is not running'), then re-run."
            ) from exc
        raise QBConnectionError(f"BeginSession failed for {company_path}: {exc}") from exc
    finally:
        try:
            if ticket:
                rp.EndSession(ticket)
        finally:
            try:
                rp.CloseConnection()
            except Exception:  # noqa: BLE001 - cleanup best-effort
                pass


def send_query(company_path: str | Path, qbxml: str) -> str:
    """Send one qbXML request envelope; return the raw response XML."""
    with qb_session(company_path) as (rp, ticket):
        return str(rp.ProcessRequest(ticket, qbxml))
