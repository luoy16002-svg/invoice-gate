"""Validate extracted invoice data and import it into a local SQLite ledger.

Portfolio sample. This is a check after OCR, not an OCR engine or payment tool.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


class ReviewRequired(ValueError):
    def __init__(self, field: str, reason: str):
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


@dataclass(frozen=True)
class Policy:
    vendor: str
    currency: str
    number_format: str

    def __post_init__(self):
        if not isinstance(self.vendor, str) or not self.vendor.strip() or len(self.vendor) > 120:
            raise ValueError("vendor must be a stable, non-empty ID of at most 120 characters")
        if self.currency not in {"USD", "EUR", "GBP", "CNY"}:
            raise ValueError("this sample supports USD, EUR, GBP and CNY")
        if self.number_format not in {"en-US", "de-DE"}:
            raise ValueError("choose en-US or de-DE explicitly")


def money(raw: object, field: str, policy: Policy) -> int:
    """Return integer minor units; never guess separators or repair OCR characters."""
    if not isinstance(raw, str):
        raise ReviewRequired(field, "expected the original amount as text")
    if len(raw) > 32:
        raise ReviewRequired(field, "amount exceeds the sample's input limit")
    grouping, decimal = (",", ".") if policy.number_format == "en-US" else (".", ",")
    whole = rf"(?:0|[1-9][0-9]{{0,8}}|[1-9][0-9]{{0,2}}(?:{re.escape(grouping)}[0-9]{{3}}){{1,2}})"
    pattern = whole + re.escape(decimal) + r"[0-9]{2}"
    if not re.fullmatch(pattern, raw):
        raise ReviewRequired(field, f"expected an unsigned {policy.number_format} amount with two decimal places")
    return int(Decimal(raw.replace(grouping, "").replace(decimal, ".")) * 100)


def text(value: object, field: str, limit: int = 120) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ReviewRequired(field, f"expected non-empty text, at most {limit} characters")
    if any(ord(char) < 32 for char in value):
        raise ReviewRequired(field, "control characters need review")
    return value


def keys(value: object, expected: set[str], field: str) -> dict:
    if not isinstance(value, dict) or set(value) != expected:
        raise ReviewRequired(field, "fields do not match the supported invoice format")
    return value


def validate(document: object, policy: Policy) -> dict:
    data = keys(document, {"invoice_number", "source_ref", "currency", "items", "subtotal", "tax", "total"}, "invoice")
    number = text(data["invoice_number"], "invoice_number")
    text(data["source_ref"], "source_ref")
    if data["currency"] != policy.currency:
        raise ReviewRequired("currency", "does not match the configured vendor currency")
    if not isinstance(data["items"], list) or not 1 <= len(data["items"]) <= 100:
        raise ReviewRequired("items", "expected between 1 and 100 line items")
    items = []
    for index, raw in enumerate(data["items"]):
        prefix = f"items[{index}]"
        item = keys(raw, {"description", "quantity", "unit_price", "line_total"}, prefix)
        description = text(item["description"], f"{prefix}.description", 240)
        quantity = item["quantity"]
        if type(quantity) is not int or not 1 <= quantity <= 100_000:
            raise ReviewRequired(f"{prefix}.quantity", "this sample requires a positive whole-number quantity")
        price = money(item["unit_price"], f"{prefix}.unit_price", policy)
        stated = money(item["line_total"], f"{prefix}.line_total", policy)
        if quantity * price != stated:
            raise ReviewRequired(f"{prefix}.line_total", "quantity times unit price does not match")
        items.append({"description": description, "quantity": quantity, "unit_price_minor": price, "line_total_minor": stated})
    subtotal, tax, total = (money(data[name], name, policy) for name in ("subtotal", "tax", "total"))
    if sum(item["line_total_minor"] for item in items) != subtotal:
        raise ReviewRequired("subtotal", "does not equal the sum of the line totals")
    if subtotal + tax != total:
        raise ReviewRequired("total", "does not equal subtotal plus stated tax")
    return {"invoice_number": number, "currency": policy.currency, "items": items, "subtotal_minor": subtotal, "tax_minor": tax, "total_minor": total}


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("""CREATE TABLE IF NOT EXISTS invoices (
        vendor TEXT NOT NULL,
        invoice_number TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        currency TEXT NOT NULL,
        total_minor INTEGER NOT NULL,
        normalized_json TEXT NOT NULL,
        source_json TEXT NOT NULL,
        PRIMARY KEY (vendor, invoice_number)
    )""")
    return connection


def import_batch(connection: sqlite3.Connection, documents: list, policy: Policy) -> list[dict]:
    if not isinstance(documents, list) or not 1 <= len(documents) <= 200:
        raise ReviewRequired("batch", "expected between 1 and 200 invoices")
    checked = [(source, validate(source, policy)) for source in documents]
    outcomes = []
    connection.execute("BEGIN IMMEDIATE")
    try:
        for source, invoice in checked:
            canonical = json.dumps(invoice, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            existing = connection.execute(
                "SELECT fingerprint FROM invoices WHERE vendor = ? AND invoice_number = ?",
                (policy.vendor, invoice["invoice_number"]),
            ).fetchone()
            if existing:
                if existing[0] != digest:
                    raise ReviewRequired("invoice_number", "an existing invoice with this vendor and number has different content")
                status = "duplicate"
            else:
                connection.execute(
                    "INSERT INTO invoices VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (policy.vendor, invoice["invoice_number"], digest, invoice["currency"], invoice["total_minor"], canonical, json.dumps(source, ensure_ascii=False)),
                )
                status = "imported"
            outcomes.append({"invoice_number": invoice["invoice_number"], "status": status})
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    return outcomes


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReviewRequired("json", f"duplicate field: {key}")
        result[key] = value
    return result


def read_batch(path: Path) -> list:
    with path.open("rb") as stream:
        raw = stream.read(1_048_577)
    if len(raw) > 1_048_576:
        raise ReviewRequired("file", "maximum input size is 1 MiB")
    documents = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    if not isinstance(documents, list) or not 1 <= len(documents) <= 200:
        raise ReviewRequired("batch", "expected between 1 and 200 invoices")
    return documents


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["check", "import"])
    parser.add_argument("file", type=Path)
    parser.add_argument("--vendor", required=True, help="stable vendor ID from your own records")
    parser.add_argument("--currency", required=True, choices=["USD", "EUR", "GBP", "CNY"])
    parser.add_argument("--number-format", required=True, choices=["en-US", "de-DE"])
    parser.add_argument("--db", type=Path, default=Path("invoices.sqlite"))
    args = parser.parse_args()
    try:
        policy = Policy(args.vendor, args.currency, args.number_format)
        documents = read_batch(args.file)
        if args.operation == "check":
            rows = []
            for index, document in enumerate(documents):
                try:
                    invoice = validate(document, policy)
                    rows.append({"index": index, "status": "consistent", "invoice_number": invoice["invoice_number"], "total_minor": invoice["total_minor"]})
                except ReviewRequired as error:
                    rows.append({"index": index, "status": "review", "field": error.field, "reason": error.reason})
            print(json.dumps(rows, indent=2))
            return 2 if any(row["status"] == "review" for row in rows) else 0
        # Validate before opening a database; a malformed input creates no ledger.
        for document in documents:
            validate(document, policy)
        connection = connect(args.db)
        try:
            outcomes = import_batch(connection, documents, policy)
        finally:
            connection.close()
        print(json.dumps(outcomes, indent=2))
        return 0
    except (ReviewRequired, ValueError, OSError, sqlite3.Error, RecursionError) as error:
        print(json.dumps({"status": "review", "reason": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
