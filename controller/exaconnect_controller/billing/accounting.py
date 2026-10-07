"""Accounting export of issued invoices (ADR 0024): Xero and QuickBooks Online
invoice payloads, and a CSV in Xero's sales-invoice import layout.

Nothing is sent anywhere: these are the request bodies ExaCarib's bookkeeper
(or a later connector) posts to ``PUT /api.xro/2.0/Invoices`` and
``POST /v3/company/<realm>/invoice``. Account and item codes come from the
environment (placeholders in .env.example), with the providers' demo defaults.

Both systems work out a line as quantity x unit price. Where our rounded
amount differs from that product (a part month, hourly bandwidth), the line is
exported as quantity 1 at the line amount, with the real quantity kept in the
description, so the books match the invoice to the cent.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import os
from decimal import Decimal

from ..metering.core import money
from .core import BillingError, dec


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _due(inv: dict) -> dt.date:
    issued = inv["issued_at"]
    issued = dt.datetime.fromisoformat(issued) if isinstance(issued, str) else issued
    return issued.date() + dt.timedelta(days=int(_env("EXA_INVOICE_DUE_DAYS", "30")))


def _issued(inv: dict) -> dt.date:
    issued = inv["issued_at"]
    return (dt.datetime.fromisoformat(issued) if isinstance(issued, str) else issued).date()


def _check(inv: dict) -> None:
    if inv["status"] != "issued":
        raise BillingError("Only an issued invoice can be exported to accounting.", 409)


def lines_for_books(inv: dict) -> list[dict]:
    """Each line as (description, quantity, unit price, amount) that multiply exactly."""
    out = []
    for x in inv["lines"]:
        q, p, a = dec(x["quantity"]), dec(x["unit_price"]), money(dec(x["amount"]))
        if money(q * p) == a:
            out.append(
                {"description": x["description"], "quantity": q.normalize(), "unit_price": p.normalize(), "amount": a}
            )
        else:
            desc = f"{x['description']} ({q.normalize():f} {x['unit']} at {p.normalize():f})"
            out.append({"description": desc, "quantity": Decimal(1), "unit_price": a, "amount": a})
    return out


def xero(inv: dict) -> dict:
    _check(inv)
    taxed = dec(inv["tax_rate_pct"]) > 0
    tax_type = _env("EXA_XERO_TAX_TYPE", "OUTPUT" if taxed else "NONE")
    return {
        "Invoices": [
            {
                "Type": "ACCREC",
                "Contact": {"Name": inv["customer"]},
                "InvoiceNumber": inv["number"],
                "Reference": f"Connect, {inv['label']}",
                "Date": _issued(inv).isoformat(),
                "DueDate": _due(inv).isoformat(),
                "CurrencyCode": inv["currency"],
                "Status": "AUTHORISED",
                "LineAmountTypes": "Exclusive",
                "LineItems": [
                    {
                        "Description": x["description"],
                        "Quantity": f"{x['quantity']:f}",
                        "UnitAmount": f"{x['unit_price']:f}",
                        "LineAmount": f"{x['amount']:.2f}",
                        "AccountCode": _env("EXA_XERO_ACCOUNT_CODE", "200"),
                        "TaxType": tax_type,
                    }
                    for x in lines_for_books(inv)
                ],
                "SubTotal": f"{dec(inv['subtotal']):.2f}",
                "TotalTax": f"{dec(inv['tax']):.2f}",
                "Total": f"{dec(inv['total']):.2f}",
            }
        ]
    }


def quickbooks(inv: dict) -> dict:
    _check(inv)
    taxed = dec(inv["tax_rate_pct"]) > 0
    return {
        "DocNumber": inv["number"],
        "TxnDate": _issued(inv).isoformat(),
        "DueDate": _due(inv).isoformat(),
        "CustomerRef": {"name": inv["customer"]},
        "CurrencyRef": {"value": inv["currency"]},
        "PrivateNote": f"ExaCarib Connect, {inv['label']}",
        "Line": [
            {
                "LineNum": i,
                "Description": x["description"],
                "Amount": float(x["amount"]),
                "DetailType": "SalesItemLineDetail",
                "SalesItemLineDetail": {
                    "ItemRef": {"name": _env("EXA_QBO_ITEM_NAME", "Connect")},
                    "Qty": float(x["quantity"]),
                    "UnitPrice": float(x["unit_price"]),
                    "TaxCodeRef": {"value": "TAX" if taxed else "NON"},
                },
            }
            for i, x in enumerate(lines_for_books(inv), 1)
        ],
        "TxnTaxDetail": {"TotalTax": float(dec(inv["tax"]))},
        "TotalAmt": float(dec(inv["total"])),
    }


CSV_HEADER = [
    "*ContactName",
    "*InvoiceNumber",
    "Reference",
    "*InvoiceDate",
    "*DueDate",
    "Description",
    "*Quantity",
    "*UnitAmount",
    "LineAmount",
    "*AccountCode",
    "*TaxType",
    "Currency",
]


def csv_rows(inv: dict) -> str:
    """Xero's sales-invoice import layout (QuickBooks imports the same columns)."""
    _check(inv)
    x = xero(inv)["Invoices"][0]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_HEADER)
    for li in x["LineItems"]:
        w.writerow(
            [
                x["Contact"]["Name"],
                x["InvoiceNumber"],
                x["Reference"],
                x["Date"],
                x["DueDate"],
                li["Description"],
                li["Quantity"],
                li["UnitAmount"],
                li["LineAmount"],
                li["AccountCode"],
                li["TaxType"],
                x["CurrencyCode"],
            ]
        )
    return buf.getvalue()
