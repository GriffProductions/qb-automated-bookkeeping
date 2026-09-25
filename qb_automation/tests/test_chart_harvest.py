"""Tests for the read-only chart harvest (chart_harvest.py). No live QB needed."""

import time

from qb_automation.services.chart_harvest import (
    CompanyChart,
    build_account_query,
    iter_company_files,
    merge_charts,
    parse_account_response,
    resolve_company_file,
)

RESPONSE = """<?xml version="1.0" encoding="utf-8"?>
<QBXML><QBXMLMsgsRs><AccountQueryRs requestID="harvest-1" statusCode="0">
<AccountRet><ListID>1-123</ListID><TimeCreated>2020-01-01</TimeCreated>
<TimeModified>2026-01-01</TimeModified><EditSequence>1</EditSequence>
<Name>Repairs and Maintenance</Name><FullName>Repairs and Maintenance</FullName>
<IsActive>true</IsActive><Sublevel>0</Sublevel>
<AccountType>Expense</AccountType><AccountNumber>6550</AccountNumber>
<Balance>1137.00</Balance></AccountRet>
<AccountRet><ListID>1-124</ListID><Name>Rent Income</Name>
<FullName>Rent Income</FullName><IsActive>true</IsActive><Sublevel>0</Sublevel>
<AccountType>Income</AccountType></AccountRet>
</AccountQueryRs></QBXMLMsgsRs></QBXML>"""


def test_harvest_query_is_read_only():
    xml = build_account_query()
    assert "AccountQueryRq" in xml
    for token in ("AddRq", "ModRq", "DelRq", "VoidRq", "Add ", "Mod "):
        assert token not in xml


def test_parse_account_response():
    accounts = parse_account_response(RESPONSE)
    assert len(accounts) == 2
    exp = accounts[0]
    assert (exp.name, exp.full_name, exp.account_type, exp.number) == (
        "Repairs and Maintenance", "Repairs and Maintenance", "Expense", "6550")
    chart = CompanyChart(company_key="x", company_name="X", company_file="f.qbw",
                         accounts=accounts)
    assert chart.expense_names == ["Repairs and Maintenance"]


def test_resolve_picks_newest_qbw(tmp_path):
    old = tmp_path / "Co.qbw"
    new = tmp_path / "Co, LLC.qbw"
    old.write_bytes(b"old")
    time.sleep(0.02)
    new.write_bytes(b"new")
    assert resolve_company_file(tmp_path) == new
    assert resolve_company_file(tmp_path / "missing") is None


def test_iter_company_files_matches_registry(tmp_path, monkeypatch):
    from qb_automation.config import settings

    co_dir = tmp_path / "Valencia Paz"
    co_dir.mkdir()
    (co_dir / "Valencia Paz.qbw").write_bytes(b"fake-qbw")
    monkeypatch.setattr(settings, "QB_COMPANY_ROOT", tmp_path)
    hits = dict(iter_company_files(tmp_path))
    assert hits.get("valencia_paz_28754") == co_dir / "Valencia Paz.qbw"


def test_merge_overwrites_per_company():
    old = {"a": {"accounts": []}}
    new = [CompanyChart(company_key="a", company_name="A", company_file="f",
                        accounts=[])]
    assert merge_charts(old, new)["a"]["company_file"] == "f"
