"""Run synthetic examples; the ledger lives in a temporary directory."""

import copy
import json
import tempfile
from pathlib import Path

from invoice_gate import Policy, ReviewRequired, connect, import_batch, validate


def main():
    policy = Policy("demo-supplier", "USD", "en-US")
    source = json.loads((Path(__file__).parent / "fixtures" / "valid.json").read_text(encoding="utf-8"))[0]
    cases = [("Consistent invoice", source)]
    wrong = copy.deepcopy(source)
    wrong["total"] = "73.86"
    cases.append(("Well-formed JSON, wrong arithmetic", wrong))
    ambiguous = copy.deepcopy(source)
    ambiguous["total"] = "7,366"
    cases.append(("Missing decimal separator", ambiguous))
    for name, invoice in cases:
        try:
            checked = validate(invoice, policy)
            print(f"{name}: consistent, total_minor={checked['total_minor']}")
        except ReviewRequired as error:
            print(f"{name}: review, {error}")
    with tempfile.TemporaryDirectory() as directory:
        connection = connect(Path(directory) / "demo.sqlite")
        try:
            print("First import:", import_batch(connection, [source], policy)[0]["status"])
            print("Retry:", import_batch(connection, [source], policy)[0]["status"])
            new = copy.deepcopy(source)
            new["invoice_number"] = "DEMO-NEW"
            changed = copy.deepcopy(source)
            changed["tax"], changed["total"] = "5.47", "73.67"
            try:
                import_batch(connection, [new, changed], policy)
            except ReviewRequired:
                print("New invoice followed by conflicting revision: entire batch rolled back")
            count, total = connection.execute("SELECT COUNT(*), SUM(total_minor) FROM invoices").fetchone()
            print(f"Final ledger: {count} invoice, {total} minor units")
        finally:
            connection.close()


if __name__ == "__main__":
    main()
