"""Field mapping covers the company or account a contact belongs to (ADR 0039)."""

from exaconnect_controller import db
from exaconnect_controller.commai import connectors
from exaconnect_controller.commai.automation import integrations


def test_company_fields_map_to_hubspot_companies(client):
    with db.tx() as conn:
        s = integrations.suggest_mapping(conn, None, "hubspot")
    company = s["objects"]["company"]
    got = {f["source"]: (f["target"], f["needs_person"]) for f in company["fields"]}
    assert got["company.company_name"] == ("name", False)
    assert got["company.domain"] == ("domain", False)
    assert got["company.company_phone"] == ("phone", False)
    assert got["company.country"] == ("country", False)
    assert not [x for x in s["open"] if x.startswith("company.")]


def test_company_maps_to_an_account_object(monkeypatch):
    """An app that calls it an account (Salesforce, Zoho) gets the same suggestions."""
    c = connectors.get("hubspot")
    monkeypatch.setattr(
        type(c),
        "mapping_targets",
        {"account": ["Name", "Website", "Phone", "Industry", "BillingCity", "BillingCountry"]},
        raising=False,
    )
    s = integrations.suggest_mapping(None, None, "hubspot")
    acc = {f["source"]: f["target"] for f in s["objects"]["account"]["fields"]}
    assert s["objects"]["account"]["from"] == "company"
    assert acc == {
        "company.company_name": "Name",
        "company.domain": "Website",
        "company.company_phone": "Phone",
        "company.industry": "Industry",
        "company.city": "BillingCity",
        "company.country": "BillingCountry",
    }
    assert not s["open"]
