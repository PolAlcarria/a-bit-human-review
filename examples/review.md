# Review of the billing and queue changes

A synthetic review for trying the finding viewer. Open it with
`FINDING_VIEWER_EXTRACT=0 python3 finding_viewer.py examples/review.md`.

## Must-fix

### [C1] A coupon can make the invoice total negative
`examples/src/billing.py:14` — `subtotal -= coupon["amount"]` is not clamped at zero, so a coupon larger than the subtotal gives a negative subtotal,
and `invoice_total` then returns a negative amount (a credit) that the caller charges as a refund.

### [C2] Float and Decimal are mixed
`examples/src/billing.py:12` — `price`, `qty` and `discount` come from JSON as floats, while `TAX_RATE` is a `Decimal`. `subtotal * TAX_RATE` raises
`TypeError` for any invoice with a float price.

## Should-fix

### [W1] The retry loop gives up silently
`examples/src/queue.py:11` — After the last attempt `send_with_retry` returns `None`, which looks like a successful empty reply. The caller cannot
tell a delivered payload from a lost one; re-raise the last `ConnectionError` instead.

### [W2] A retried refund is applied twice
`examples/src/billing.py:22` — `refund` changes the invoice in place and keeps no record of it, so a retried request
refunds the same amount again as long as the remaining total covers it. Key refunds by an idempotency id.
