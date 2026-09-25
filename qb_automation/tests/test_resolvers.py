"""Tests for QB name resolvers. Uses fixture rosters (never the live data files)."""

import pytest

from qb_automation.services import resolvers

FIXTURES = (
    {"dm": {"accounts": [
        {"full_name": "Repairs and Maintenance", "account_type": "Expense"},
        {"full_name": "Interest Expense", "account_type": "Expense"},
        {"full_name": "Insurance Expense", "account_type": "Expense"},
        {"full_name": "Taxes:State", "account_type": "Expense"},
        {"full_name": "Ask My Accountant", "account_type": "Expense"},
        {"full_name": "Rent Income", "account_type": "Income"},
    ]}},
    {"dm": {"vendors": [{"name": "Daniel Stegall"}, {"name": "Franchise Tax Board"}]}},
    {"dm": {"classes": [{"full_name": "200"}, {"full_name": "201"}]}},
)


@pytest.fixture(autouse=True)
def fixture_rosters(monkeypatch):
    monkeypatch.setattr(resolvers, "_cache", lambda: FIXTURES)


def test_vendor_exact_and_case_insensitive():
    assert resolvers.resolve_vendor("dm", "Daniel Stegall") == ("Daniel Stegall", True)
    assert resolvers.resolve_vendor("dm", "FRANCHISE TAX BOARD") == ("Franchise Tax Board", True)


def test_vendor_unknown_passes_through_with_warning():
    assert resolvers.resolve_vendor("dm", "Mystery LLC") == ("Mystery LLC", False)
    assert any("Mystery LLC" in w for w in
               resolvers.validation_warnings("dm", "Mystery LLC", "Invoice", None))


def test_account_vendor_map_and_defaults():
    assert resolvers.resolve_account("dm", "Daniel Stegall", "Invoice")[0] == \
        "Repairs and Maintenance"
    # Mortgage default is definitional → not a fallback.
    assert resolvers.resolve_account("dm", "Nobody", "Mortgage") == ("Interest Expense", False)
    # Unknown vendor+Invoice falls to doc default (flagged for review).
    acct, fallback = resolvers.resolve_account("dm", "Nobody", "Invoice")
    assert (acct, fallback) == ("Repairs and Maintenance", True)


def test_account_vendor_map_beats_doc_default():
    # FTB maps to Taxes:State (in chart); the generic Tax default (Taxes)
    # is NOT in the fixture chart, so the vendor hit must win, clean.
    assert resolvers.resolve_account("dm", "Franchise Tax Board", "Tax") == \
        ("Taxes:State", False)


def test_account_empty_chart():
    assert resolvers.resolve_account("nope", "V", "Invoice") == ("", True)


def test_class_unit_forms():
    assert resolvers.resolve_class("dm", "Unit 200") == "200"
    assert resolvers.resolve_class("dm", "200") == "200"
    assert resolvers.resolve_class("dm", "SCD") is None  # clinic code, no owner match
    assert resolvers.resolve_class("dm", "Unit 999") is None
    assert resolvers.resolve_class("dm", None) is None


def test_clean_transaction_has_no_warnings():
    assert resolvers.validation_warnings("dm", "Daniel Stegall", "Mortgage", "Unit 200") == []


def test_suggest_names_ranks_closest_first():
    candidates = ["Franchise Tax Board", "State Farm", "Daniel Stegall"]
    assert resolvers.suggest_names(candidates, "franchise tax bord")[0] == "Franchise Tax Board"
    assert resolvers.suggest_names(candidates, "zzz-no-match-zzz") == []
    assert resolvers.suggest_names([], "x") == []


def test_parenthetical_vendor_still_maps_account():
    # "(HOA) Daniel Stegall" must hit the same map entry as "Daniel Stegall".
    assert resolvers.resolve_account("dm", "(HOA) Daniel Stegall", "Invoice") == \
        ("Repairs and Maintenance", False)


def test_warnings_are_console_safe_ascii():
    for w in resolvers.validation_warnings("dm", "Mystery LLC", "Invoice", "Unit 999"):
        w.encode("cp1252")


def test_review_suggestions_only_flags_broken_refs():
    clean = resolvers.review_suggestions("dm", "Daniel Stegall", "Mortgage", "Unit 200")
    assert clean == {}
    broken = resolvers.review_suggestions("dm", "Mystery LLC", "Invoice", "Unit 999")
    assert "vendor" in broken and "class" in broken
    assert 1 <= len(broken["account"]) <= 4
    assert broken["account"][0] == "Repairs and Maintenance"  # fallback heads the list


def test_globalcare_facility_subaccounts():
    charts = {"arc": {"accounts": [
        {"full_name": "Billing Expense", "account_type": "Expense"},
        {"full_name": "Billing Expense:LCD", "account_type": "Expense"},
        {"full_name": "Billing Expense:NKC", "account_type": "Expense"},
        {"full_name": "Billing Expense:SCD", "account_type": "Expense"},
        {"full_name": "Repairs and Maintenance", "account_type": "Expense"},
    ]}}
    import qb_automation.services.resolvers as r
    old = r._cache
    r._cache = lambda: (charts, {}, {})  # noqa: E731
    try:
        assert r.resolve_account("arc", "GlobalCare", "Invoice", "Laurel Canyon Dialysis") == \
            ("Billing Expense:LCD", False)
        assert r.resolve_account("arc", "GlobalCare", "Invoice", "NKC") == \
            ("Billing Expense:NKC", False)
        # MHD has no subaccount → parent, flagged for review.
        assert r.resolve_account("arc", "GlobalCare", "Invoice", "MHD") == \
            ("Billing Expense", True)
    finally:
        r._cache = old


def test_vendor_alias_resolves_without_pick(monkeypatch):
    import qb_automation.services.resolvers as r
    charts = {"arc": {"accounts": [{"full_name": "Billing Expense", "account_type": "Expense"}]}}
    vendors = {"arc": {"vendors": [{"name": "Global Care Dialysis Consultancy"}]}}
    monkeypatch.setattr(r, "_cache", lambda: (charts, vendors, {}))
    assert r.resolve_vendor("arc", "GlobalCare") == ("Global Care Dialysis Consultancy", True)


def test_account_alias_water_variant(monkeypatch):
    import qb_automation.services.resolvers as r
    charts = {"tib": {"accounts": [
        {"full_name": "Utilities Expense:Water", "account_type": "Expense"},
        {"full_name": "Repairs and Maintenance", "account_type": "Expense"},
    ]}}
    monkeypatch.setattr(r, "_cache", lambda: (charts, {}, {}))
    assert r.resolve_account("tib", "SCV Water", "Bill-and-Payment", None) == \
        ("Utilities Expense:Water", False)
