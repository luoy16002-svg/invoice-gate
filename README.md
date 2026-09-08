# Invoice Gate

A small Python example of the checks between OCR output and a ledger. Validate extracted invoice JSON, flag inconsistent fields, and import consistent records into SQLite without counting retries twice.

All invoice data is synthetic. This is an independent work sample; no OCR engine or customer deployment is included.

## Run it

Python 3.12 or newer. No external packages.

```sh
python demo.py
python -m unittest -v
```

The demo shows a correct invoice, two review cases, an identical retry, and a conflicting batch. The final ledger contains one invoice for 7,366 minor units.

```sh
python invoice_gate.py check fixtures/valid.json --vendor demo-supplier --currency USD --number-format en-US
python invoice_gate.py import fixtures/valid.json --vendor demo-supplier --currency USD --number-format en-US --db invoices.sqlite
```

Run the import twice: the second result is `duplicate`. Exit code `2` means review or an input/storage error; `0` means the requested operation completed.

## Read the code

- [Validator and transactional importer](invoice_gate.py)
- [20 tests](test_invoice_gate.py), including concurrent imports and rollback
- [Writing sample: valid JSON can still be the wrong invoice](writing-sample.md)

The caller supplies a stable vendor ID, expected currency and number format from trusted configuration. Source strings are kept with the first imported record. A different scan reference does not create a second invoice.

## Deliberate limits

Whole-number quantities, positive invoices, two-decimal USD/EUR/GBP/CNY amounts, no shipping or discounts. Unsupported formats go to review. The tax amount is checked arithmetically, not against tax law. Invoice numbers and vendor identities must be verified against the source separately. A `consistent` result does not establish that an invoice is authentic, correct, approved or payable.

Changed invoices require an explicit correction process outside this sample. There is no payment connection. Test data is disposable; use a separate database when trying the commands.
