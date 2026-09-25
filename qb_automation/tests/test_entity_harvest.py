"""Tests for read-only vendor + class harvest (entity_harvest.py). No live QB needed."""

from qb_automation.services.entity_harvest import (
    CompanyClasses,
    CompanyVendors,
    build_class_query,
    build_vendor_query,
    merge_entities,
    parse_class_response,
    parse_vendor_response,
)

VENDOR_RESPONSE = """<?xml version="1.0" encoding="utf-8"?>
<QBXML><QBXMLMsgsRs><VendorQueryRs requestID="harvest-vendors-1" statusCode="0">
<VendorRet><ListID>1-111</ListID><TimeCreated>2020-01-01</TimeCreated>
<Name>Republic Services</Name><IsActive>true</IsActive></VendorRet>
<VendorRet><ListID>1-112</ListID><Name>State Farm</Name>
<IsActive>true</IsActive></VendorRet>
</VendorQueryRs></QBXMLMsgsRs></QBXML>"""

CLASS_RESPONSE = """<?xml version="1.0" encoding="utf-8"?>
<QBXML><QBXMLMsgsRs><ClassQueryRs requestID="harvest-classes-1" statusCode="0">
<ClassRet><ListID>2-111</ListID><Name>Unit 200</Name>
<FullName>Unit 200</FullName><Sublevel>0</Sublevel></ClassRet>
<ClassRet><ListID>2-112</ListID><Name>LCD</Name>
<FullName>LCD</FullName><Sublevel>0</Sublevel></ClassRet>
</ClassQueryRs></QBXMLMsgsRs></QBXML>"""


def test_vendor_query_is_read_only():
    xml = build_vendor_query()
    assert "VendorQueryRq" in xml
    for token in ("AddRq", "ModRq", "DelRq", "VoidRq", "Add ", "Mod "):
        assert token not in xml


def test_class_query_is_read_only():
    xml = build_class_query()
    assert "ClassQueryRq" in xml
    for token in ("AddRq", "ModRq", "DelRq", "VoidRq", "Add ", "Mod "):
        assert token not in xml


def test_parse_vendor_response():
    vendors = parse_vendor_response(VENDOR_RESPONSE)
    assert len(vendors) == 2
    assert vendors[0].name == "Republic Services"
    assert vendors[0].list_id == "1-111"
    chart = CompanyVendors(company_key="x", company_name="X", company_file="f.qbw",
                           vendors=vendors)
    assert chart.vendor_names == ["Republic Services", "State Farm"]


def test_parse_class_response():
    classes = parse_class_response(CLASS_RESPONSE)
    assert len(classes) == 2
    assert classes[0].full_name == "Unit 200"
    chart = CompanyClasses(company_key="x", company_name="X", company_file="f.qbw",
                           classes=classes)
    assert chart.class_names == ["Unit 200", "LCD"]


def test_merge_entities_overwrites_per_company():
    old = {"a": {"vendors": []}}
    new = [CompanyVendors(company_key="a", company_name="A", company_file="f",
                          vendors=[])]
    assert merge_entities(old, new)["a"]["company_file"] == "f"


def test_class_query_has_no_ownerid():
    # ClassQueryRq + OwnerID = parse error 0x80040400 on qbXML 13 (found live).
    assert "OwnerID" not in build_class_query()


def test_parse_error_is_not_auth_error():
    from qb_automation.services.qb_connection import _is_auth_error

    parse_err = RuntimeError(
        "(0, 'QBXMLRP2.RequestProcessor.2', 'QuickBooks found an error when "
        "parsing the provided XML text stream.', None, 0, -2147220480)"
    )
    assert not _is_auth_error(parse_err)
    auth_err = RuntimeError("0x80040422 grant permission")
    assert _is_auth_error(auth_err)
