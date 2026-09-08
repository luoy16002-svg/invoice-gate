# Valid JSON can still be the wrong invoice

An invoice extractor returns three notebooks at $19.90, two pens at $4.25, tax of $5.46 and a total of $73.86. Every field is present. Every amount is a string with two decimal places. A schema check can pass it.

The total is still wrong. The line items add up to $68.20; with tax, the invoice should total $73.66. That twenty-cent difference is enough to stop the import.

This is a synthetic example from Invoice Gate, a small Python project that checks extracted data before writing it to SQLite. It starts after extraction. It does not benchmark an OCR model.

## Choose the number format before parsing

`1,234.56` and `1.234,56` describe the same amount under different conventions. Removing every comma is not a general parser. It can turn a decimal separator into a much larger number.

The sample asks the caller to select `en-US` or `de-DE` from trusted configuration. That choice does not come from the extracted document. Each amount must match the selected convention and include two decimal places. Text such as `73.6O` is sent to review; the parser does not silently replace a letter with a digit.

Only then does it construct a Decimal from the normalized text and convert the result to integer minor units. Subsequent arithmetic uses integers. This avoids introducing binary floating-point rounding into a comparison that should be exact. Python's [Decimal documentation](https://docs.python.org/3/library/decimal.html) explains why constructing a Decimal from text differs from constructing one from a float.

## Check the relationships between fields

The checks follow the invoice from the inside out:

1. Quantity times unit price must equal each stated line total.
2. Line totals must add up to the stated subtotal.
3. Subtotal plus stated tax must equal the stated total.

The error identifies the field that needs review. A mismatch in `items[0].line_total` is more useful than a generic failed-import message. The original extracted strings are retained with the imported record, so normalization does not erase the input used for the decision.

These rules deliberately cover a narrow invoice format. Fractional quantities, discounts and shipping need explicit policies. A balanced tax amount does not prove that the tax rate is correct. Unsupported cases should remain visible instead of being squeezed through rules written for another format.

## A retry should add zero invoices

Correct amounts do not prevent duplicates. If a job writes an invoice and its caller misses the response, the caller may submit the same invoice again.

Invoice Gate uses the configured vendor ID and invoice number as the record key. It also fingerprints the normalized content. The same key and content return `duplicate`; changed content under that key requires review. The second path matters: treating every key collision as success would conceal a revised amount or a different line item.

The lookup and insert run inside one `BEGIN IMMEDIATE` transaction. SQLite permits one writer at a time, so competing importers cannot both finish the lookup before either writes. A busy database can still return an error; the caller must handle it. See SQLite's [transaction reference](https://sqlite.org/lang_transaction.html).

The tests also cover a batch containing a new invoice followed by a conflicting revision. The new invoice must roll back with the conflict. Retrying a half-imported batch should not become another reconciliation task.

## Try it, then inspect what it cannot prove

Run `python demo.py` for the scenarios and `python -m unittest -v` for the tests. No packages or API keys are needed.

The command reports `consistent`, not `approved`. An extractor could misread several fields in a way that still balances. The vendor or invoice number could also be wrong. Comparing important fields with the original document remains a separate step. Arithmetic checks and replay handling remove specific failure modes; they do not establish that a document should be paid.
