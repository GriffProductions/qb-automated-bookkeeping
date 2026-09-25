"""Resolve free-text vendors/accounts/classes to exact QB list names.

Every ``VendorRef`` / ``AccountRef`` / ``ClassRef`` FullName in emitted qbXML
must match the target company file EXACTLY or QuickBooks rejects the whole
import.  The extractor produces free text ("AT&T", "LADWP", "Unit 200"), so
this module constrains each value to the harvested rosters
(``data/vendors.json``, ``data/chart_of_accounts.json``, ``data/classes.json``).

Resolution never invents names: account fallback chain is
explicit-map → doc-type default → per-company suspense
(``Ask My Accountant`` → ``Unknown`` → first expense account alphabetically),
and every fallback is reported via :func:`validation_warnings` so dry runs
surface what needs human review.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from functools import lru_cache
from pathlib import Path

from qb_automation.config import settings

log = logging.getLogger(__name__)


# Extractor spelling (normalized) → exact QB vendor Name.  Checked before
# fuzzy matching so known aliases resolve cleanly with no picks.
VENDOR_ALIASES: dict[str, str] = {
    "globalcare": "Global Care Dialysis Consultancy",
}


def _norm(s: str) -> str:
    """Lowercase alphanumeric fold for fuzzy name matching."""
    return re.sub(r"[^a-z0-9]+", "", s.lower())


# Canonical vendor (matched case-insensitively, see KNOWN_VENDORS) → expense
# account.  Validated per-company at resolve time; entries missing from a
# company's chart fall through to DOC_TYPE_DEFAULTS.
VENDOR_ACCOUNT_MAP: dict[str, str] = {
    # Utilities
    "ladwp": "Utilities",
    "sce": "Utilities",
    "socal gas": "Utilities:Gas",
    "scv water": "Utilities:Water",
    "las vegas valley water district": "Utilities:Water",
    "rayne water": "Utilities:Water",
    "at&t": "Telephone Expense",
    # Waste haulers book to Utilities where no dedicated waste account exists.
    "republic services": "Utilities",
    "burrtec": "Utilities",
    "waste management": "Utilities",
    # Insurance
    "state farm": "Insurance Expense",
    # Tax authorities
    "franchise tax board": "Taxes:State",
    "la county treasurer & tax collector": "Taxes:Property",
    "clark county treasurer": "Taxes:Property",
    "state of california": "Taxes:State",
    # Lenders (single-line mortgage booking → interest leg)
    "newrez": "Interest Expense",
    "pnc bank": "Interest Expense",
    "mr. cooper": "Interest Expense",
    "onity mortgage": "Interest Expense",
    "f&m bank": "Interest Expense",
    # HOA / property management (keys in paren-stripped, lowered form —
    # resolve_account strips "(HOA) " prefixes before lookup)
    "west creek and west hills community association": "HOA Fees",
    "west creek / west hills": "HOA Fees",
    "valencia fairways": "HOA Fees",
    "summerlin north": "HOA Fees",
    "siena villas hoa": "HOA Fees",
    "scenic hills": "Property Management Fee",
    "peace realty": "Property Management Fee",
    "brad management": "Property Management Fee",
    "utopia management": "Property Management Fee",
    "valencia fairways": "HOA Fees",
    "buena vida": "HOA Fees",
    # Maintenance
    "daniel stegall": "Repairs and Maintenance",
    "k&s air conditioning": "Repairs and Maintenance",
    "culligan of sylmar": "Repairs and Maintenance",
    # Professional
    "briscoe economics group": "Professional Fees",
    "lucove say & co.": "Accounting Expense",
    "ballard rosenberg": "Legal Fees",
}

# DocType → expense account when no vendor rule hits.  Validated per-company.
DOC_TYPE_DEFAULTS: dict[str, str] = {
    "Mortgage": "Interest Expense",
    "Tax": "Taxes",
    "Insurance": "Insurance Expense",
    "Invoice": "Repairs and Maintenance",
    "Payment": "Repairs and Maintenance",
    "Bill-and-Payment": "Repairs and Maintenance",
}

# Per-company suspense preference when nothing resolves.
_SUSPENSE_CANDIDATES = ("Ask My Accountant", "Unknown")


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _cache() -> tuple[dict, dict, dict]:
    return (
        _load_json(settings.CHART_OF_ACCOUNTS_PATH),
        _load_json(settings.VENDORS_PATH),
        _load_json(settings.CLASSES_PATH),
    )


def company_key_for_display(company_name: str) -> str | None:
    """Registry key for an ExtractedTransaction.company_name (display name)."""
    from qb_automation.config.company_registry import COMPANIES

    want = company_name.strip().lower()
    for c in COMPANIES:
        if c.display_name.lower() == want:
            return c.key
    return None


def expense_names(company_key: str) -> list[str]:
    charts, _, _ = _cache()
    entry = charts.get(company_key, {})
    return [a["full_name"] for a in entry.get("accounts", [])
            if a.get("account_type") == "Expense"]


def vendor_names(company_key: str) -> list[str]:
    _, vendors, _ = _cache()
    entry = vendors.get(company_key, {})
    return [v["name"] for v in entry.get("vendors", [])]


def class_names(company_key: str) -> list[str]:
    _, _, classes = _cache()
    entry = classes.get(company_key, {})
    return [c["full_name"] for c in entry.get("classes", [])]


# Vendors whose expense account is a per-facility subaccount of a parent:
# value is the parent account; the facility code (from unit_class) selects
# the subaccount, e.g. GlobalCare + LCD → "Billing Expense:LCD".
FACILITY_SUBACCOUNT_VENDORS: dict[str, str] = {
    "globalcare": "Billing Expense",
}

# Spelled-out facility / code / keyword → subaccount code.
_FACILITY_CODES: dict[str, str] = {
    "lcd": "LCD", "laurel canyon dialysis": "LCD", "laurel": "LCD",
    "nkc": "NKC", "northridge kidney center": "NKC", "northridge": "NKC",
    "north ridge kidney center": "NKC",
    "scd": "SCD", "santa clarita dialysis": "SCD", "santa clarita": "SCD",
    "mhd": "MHD", "mission hills dialysis": "MHD", "mission hills": "MHD",
}


def _facility_code(unit_class: str | None) -> str | None:
    if not unit_class:
        return None
    lowered = unit_class.strip().lower()
    if lowered in _FACILITY_CODES:
        return _FACILITY_CODES[lowered]
    for keyword, code in sorted(_FACILITY_CODES.items(), key=lambda kv: -len(kv[0])):
        if keyword in lowered:
            return code
    return None


# Account name variants across company files (normalized want → alternates
# tried in order).  Tiburon-style charts use "Utilities Expense:Water"
# where most files use "Utilities:Water".
ACCOUNT_ALIASES: dict[str, list[str]] = {
    "utilities:water": ["Utilities:Water", "Utilities Expense:Water", "Utilities"],
    "utilities:gas": ["Utilities:Gas", "Utilities Expense:Gas", "Utilities"],
    "telephone expense": ["Telephone Expense", "Utilities Expense:Phone", "Utilities"],
}


def _validated(want: str, candidates: list[str]) -> str | None:
    """Exact then case-insensitive match of ``want`` in ``candidates``."""
    if want in candidates:
        return want
    lowered = want.lower()
    for c in candidates:
        if c.lower() == lowered:
            return c
    for alt in ACCOUNT_ALIASES.get(lowered, []):
        if alt in candidates:
            return alt
        for c in candidates:
            if c.lower() == alt.lower():
                return c
    return None


def resolve_account(company_key: str, vendor: str, doc_type: str,
                    unit_class: str | None = None) -> tuple[str, bool]:
    """Return ``(FullName, is_fallback)`` guaranteed present in the chart
    (or ``""`` when the company has no chart / no expense accounts)."""
    expenses = expense_names(company_key)
    if not expenses:
        return "", True
    # Strip QB parenthetical prefixes ("(HOA) Valencia Fairways") for the
    # map lookup — the roster spelling rarely matches the map key verbatim.
    map_key = re.sub(r"^\([^)]*\)\s*", "", vendor.strip().lower())
    # Per-facility subaccount vendors (GlobalCare + LCD → Billing Expense:LCD).
    # Missing subaccount (MHD has none) falls back to the parent account.
    parent = FACILITY_SUBACCOUNT_VENDORS.get(map_key)
    if parent is not None:
        code = _facility_code(unit_class)
        if code is not None:
            hit = _validated(f"{parent}:{code}", expenses)
            if hit is not None:
                return hit, False
        hit = _validated(parent, expenses)
        if hit is not None:
            return hit, code is not None
    hit = _validated(VENDOR_ACCOUNT_MAP.get(map_key, ""), expenses)
    if hit is not None:
        return hit, False
    hit = _validated(DOC_TYPE_DEFAULTS.get(doc_type, ""), expenses)
    if hit is not None:
        # Doc-type defaults are approximations — flag for review, except the
        # three types whose mapping is definitional.
        return hit, doc_type not in ("Mortgage", "Tax", "Insurance")
    for suspense in _SUSPENSE_CANDIDATES:
        hit = _validated(suspense, expenses)
        if hit is not None:
            return hit, True
    return sorted(expenses)[0], True


def resolve_vendor(company_key: str, vendor: str) -> tuple[str, bool]:
    """Return ``(QB vendor Name, is_exact)``.  Falls back to the input text
    (``is_exact=False``) so the builder never silently swaps payees; the
    warning tells the reviewer the import may bounce."""
    names = vendor_names(company_key)
    if not names:
        return vendor, False
    for name in names:
        if name == vendor:
            return name, True
    alias = VENDOR_ALIASES.get(_norm(vendor))
    if alias is not None:
        hit = _validated(alias, names)
        if hit is not None:
            return hit, True
    want_norm = _norm(vendor)
    for name in names:
        if _norm(name) == want_norm:
            return name, True
    return vendor, False


def resolve_class(company_key: str, unit_class: str | None) -> str | None:
    """Return a valid ClassRef FullName or None (omit the ref).

    Tries exact, case-insensitive, "Unit N" ↔ "N" stripped/prefixed forms.
    ARC clinic codes (SCD/NKC/…) never match owner classes → None.
    """
    if not unit_class:
        return None
    names = class_names(company_key)
    if not names:
        return None
    hit = _validated(unit_class, names)
    if hit is not None:
        return hit
    m = re.fullmatch(r"(?i)unit\s+(\S+)", unit_class.strip())
    if m:
        hit = _validated(m.group(1), names)
        if hit is not None:
            return hit
    else:
        hit = _validated(f"Unit {unit_class.strip()}", names)
        if hit is not None:
            return hit
    return None


def validation_warnings(company_key: str, vendor: str, doc_type: str,
                        unit_class: str | None) -> list[str]:
    """Human-review notes for one transaction (empty = clean import)."""
    warnings: list[str] = []
    account, acct_fallback = resolve_account(company_key, vendor, doc_type, unit_class)
    if not account:
        warnings.append(f"No chart harvested for company {company_key!r}")
    elif acct_fallback:
        # NOTE: ASCII "->" only — cards print to cp1252 consoles.
        warnings.append(f"Account fallback -> {account!r} (vendor={vendor!r} doc={doc_type})")
    _, vendor_exact = resolve_vendor(company_key, vendor)
    if not vendor_exact:
        warnings.append(f"Vendor {vendor!r} not in {company_key} vendor list — import may reject")
    if unit_class and resolve_class(company_key, unit_class) is None:
        warnings.append(f"Class {unit_class!r} not in {company_key} classes — ClassRef omitted")
    return warnings


def llm_context_block(company_key: str, max_vendors: int = 80) -> str:
    """Candidate lists for the extractor prompt (empty when unharvested)."""
    expenses = expense_names(company_key)
    vendors = vendor_names(company_key)
    classes = class_names(company_key)
    if not expenses and not vendors:
        return ""
    lines = [f"QuickBooks lists for this company (pick EXACTLY these spellings):"]
    if vendors:
        shown = vendors[:max_vendors]
        lines.append(f"Vendors ({len(vendors)}): " + "; ".join(shown)
                     + ("; …" if len(vendors) > max_vendors else ""))
    if expenses:
        lines.append("Expense accounts: " + "; ".join(expenses))
    if classes:
        lines.append("Classes: " + "; ".join(classes))
    return "\n".join(lines) + "\n"


def suggest_names(candidates: list[str], want: str, n: int = 3) -> list[str]:
    """Top-N closest roster spellings to ``want`` (normalized similarity).

    Returns original spellings, best first.  Empty when nothing is close.
    """
    if not candidates or not want or not want.strip():
        return []
    folded = {c: _norm(c) for c in candidates}
    want_norm = _norm(want)
    scored = sorted(
        ((difflib.SequenceMatcher(None, want_norm, norm).ratio(), c)
         for c, norm in folded.items()),
        reverse=True,
    )
    return [c for score, c in scored[:n] if score >= 0.35]


def review_suggestions(company_key: str, vendor: str, doc_type: str,
                       unit_class: str | None) -> dict[str, list[str]]:
    """Top-3 correction candidates for each ref that failed to resolve.

    Keys present only for refs needing attention: ``vendor`` (not in vendor
    list), ``account`` (fell back), ``class`` (omitted).  The resolved
    fallback account heads its list so pick-1 always means "best known".
    """
    out: dict[str, list[str]] = {}
    names = vendor_names(company_key)
    _, vendor_exact = resolve_vendor(company_key, vendor)
    if not vendor_exact and names:
        out["vendor"] = suggest_names(names, vendor)
    expenses = expense_names(company_key)
    account, is_fallback = resolve_account(company_key, vendor, doc_type, unit_class)
    if is_fallback and account and expenses:
        alts = [a for a in suggest_names(expenses, vendor) if a != account]
        out["account"] = [account, *alts][:3]
        # Always include in-chart suspense options as stable picks.
        for suspense in _SUSPENSE_CANDIDATES:
            hit = _validated(suspense, expenses)
            if hit is not None and hit not in out["account"] and len(out["account"]) < 4:
                out["account"].append(hit)
    if unit_class and resolve_class(company_key, unit_class) is None:
        cls = class_names(company_key)
        if cls:
            out["class"] = suggest_names(cls, unit_class) or cls[:3]
    return out
