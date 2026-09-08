import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from invoice_gate import Policy, ReviewRequired, connect, import_batch, money, read_batch, validate


ROOT = Path(__file__).resolve().parent
POLICY = Policy("demo-supplier", "USD", "en-US")


def sample(number="DEMO-1042"):
    invoice = json.loads((ROOT / "fixtures" / "valid.json").read_text(encoding="utf-8"))[0]
    invoice["invoice_number"] = number
    return invoice


class ValidationTests(unittest.TestCase):
    def test_valid_document_preserves_integer_minor_units(self):
        checked = validate(sample(), POLICY)
        self.assertEqual(checked["total_minor"], 7366)
        self.assertEqual(checked["subtotal_minor"], 6820)
        self.assertEqual(checked["items"][0]["line_total_minor"], 5970)

    def test_valid_json_with_wrong_total_needs_review(self):
        invoice = sample()
        invoice["total"] = "73.86"
        with self.assertRaisesRegex(ReviewRequired, "subtotal plus stated tax"):
            validate(invoice, POLICY)

    def test_wrong_line_total_is_identified(self):
        invoice = sample()
        invoice["items"][0]["line_total"] = "59.10"
        with self.assertRaises(ReviewRequired) as caught:
            validate(invoice, POLICY)
        self.assertEqual(caught.exception.field, "items[0].line_total")

    def test_line_sum_must_match_subtotal(self):
        invoice = sample()
        invoice["subtotal"] = "68.00"
        invoice["total"] = "73.46"
        with self.assertRaisesRegex(ReviewRequired, "sum of the line totals"):
            validate(invoice, POLICY)

    def test_format_is_selected_by_the_caller(self):
        european = Policy("demo-eu", "EUR", "de-DE")
        self.assertEqual(money("1.234,56", "total", european), 123456)
        self.assertEqual(money("1,234.56", "total", POLICY), 123456)
        with self.assertRaises(ReviewRequired):
            money("1.234,56", "total", POLICY)

    def test_amount_parser_does_not_repair_ambiguous_or_ocr_text(self):
        for value in ["1,234", "73.6", "73.6O", " 73.66", "$73.66", "7,36.66", "073.66"]:
            with self.subTest(value=value), self.assertRaises(ReviewRequired):
                money(value, "total", POLICY)

    def test_nonfinite_float_exponent_and_negative_amounts_are_rejected(self):
        for value in [73.66, True, None, "NaN", "Infinity", "1e3", "-73.66", "9999999999.00"]:
            with self.subTest(value=value), self.assertRaises(ReviewRequired):
                money(value, "total", POLICY)

    def test_currency_mismatch_needs_review(self):
        invoice = sample()
        invoice["currency"] = "EUR"
        with self.assertRaisesRegex(ReviewRequired, "configured vendor currency"):
            validate(invoice, POLICY)

    def test_unsupported_fields_are_not_silently_discarded(self):
        invoice = sample()
        invoice["shipping"] = "10.00"
        with self.assertRaisesRegex(ReviewRequired, "supported invoice format"):
            validate(invoice, POLICY)

    def test_unsupported_quantities_are_reviewed(self):
        for quantity in [True, 0, -1, 1.5, "3", 100001]:
            invoice = sample()
            invoice["items"][0]["quantity"] = quantity
            with self.subTest(quantity=quantity), self.assertRaises(ReviewRequired):
                validate(invoice, POLICY)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "ledger.sqlite"
        self.connection = connect(self.path)

    def tearDown(self):
        self.connection.close()
        self.directory.cleanup()

    def count(self):
        return self.connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]

    def test_identical_retry_does_not_add_another_invoice(self):
        first = import_batch(self.connection, [sample()], POLICY)
        retry = sample()
        retry["source_ref"] = "second-scan-of-the-same-synthetic-invoice"
        second = import_batch(self.connection, [retry], POLICY)
        self.assertEqual(first[0]["status"], "imported")
        self.assertEqual(second[0]["status"], "duplicate")
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.connection.execute("SELECT SUM(total_minor) FROM invoices").fetchone()[0], 7366)

    def test_same_number_with_changed_content_is_not_overwritten(self):
        import_batch(self.connection, [sample()], POLICY)
        changed = sample()
        changed["items"][0]["description"] = "Different goods"
        with self.assertRaisesRegex(ReviewRequired, "different content"):
            import_batch(self.connection, [changed], POLICY)
        original = self.connection.execute("SELECT normalized_json FROM invoices").fetchone()[0]
        self.assertEqual(json.loads(original)["items"][0]["description"], "Sample notebooks")

    def test_conflict_rolls_back_other_invoices_in_the_same_batch(self):
        import_batch(self.connection, [sample()], POLICY)
        changed = sample()
        changed["tax"] = "5.47"
        changed["total"] = "73.67"
        with self.assertRaises(ReviewRequired):
            import_batch(self.connection, [sample("DEMO-NEW"), changed], POLICY)
        self.assertEqual(self.count(), 1)
        self.assertIsNone(self.connection.execute("SELECT 1 FROM invoices WHERE invoice_number = 'DEMO-NEW'").fetchone())

    def test_invalid_document_leaves_the_whole_batch_unwritten(self):
        invalid = sample("DEMO-BAD")
        invalid["total"] = "73.86"
        with self.assertRaises(ReviewRequired):
            import_batch(self.connection, [sample(), invalid], POLICY)
        self.assertEqual(self.count(), 0)

    def test_same_invoice_number_from_two_vendors_is_allowed(self):
        import_batch(self.connection, [sample()], POLICY)
        import_batch(self.connection, [sample()], Policy("another-demo-supplier", "USD", "en-US"))
        self.assertEqual(self.count(), 2)

    def test_retry_after_reopening_database_is_still_a_duplicate(self):
        import_batch(self.connection, [sample()], POLICY)
        self.connection.close()
        self.connection = connect(self.path)
        self.assertEqual(import_batch(self.connection, [sample()], POLICY)[0]["status"], "duplicate")
        self.assertEqual(self.count(), 1)

    def test_two_concurrent_importers_record_one_invoice(self):
        def worker():
            connection = connect(self.path)
            try:
                return import_batch(connection, [sample()], POLICY)[0]["status"]
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            states = list(executor.map(lambda _: worker(), range(2)))
        self.assertCountEqual(states, ["imported", "duplicate"])
        self.assertEqual(self.count(), 1)

    def test_original_extracted_fields_are_kept_for_review(self):
        original = sample()
        import_batch(self.connection, [original], POLICY)
        stored = self.connection.execute("SELECT source_json FROM invoices").fetchone()[0]
        self.assertEqual(json.loads(stored), original)


class CommandTests(unittest.TestCase):
    def test_duplicate_json_keys_are_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text('[{"total":"73.66","total":"73.86"}]', encoding="utf-8")
            with self.assertRaisesRegex(ReviewRequired, "duplicate field"):
                read_batch(path)

    def test_review_output_is_machine_readable_and_does_not_create_a_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.json"
            database = Path(directory) / "ledger.sqlite"
            bad = sample("DEMO-BAD")
            bad["total"] = "73.86"
            source.write_text(json.dumps([sample(), bad]), encoding="utf-8")
            common = [str(ROOT / "invoice_gate.py"), "check", str(source), "--vendor", "demo-supplier", "--currency", "USD", "--number-format", "en-US", "--db", str(database)]
            completed = subprocess.run([sys.executable, *common], capture_output=True, text=True, check=False)
            self.assertEqual(completed.returncode, 2)
            self.assertEqual([row["status"] for row in json.loads(completed.stdout)], ["consistent", "review"])
            self.assertFalse(database.exists())
            common[1] = "import"
            rejected = subprocess.run([sys.executable, *common], capture_output=True, text=True, check=False)
            self.assertEqual(rejected.returncode, 2)
            self.assertFalse(database.exists())


if __name__ == "__main__":
    unittest.main()
