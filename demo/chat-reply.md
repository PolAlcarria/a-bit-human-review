Yes. `redeem` returns the coupon's full amount and nothing caps it, so a 50 € coupon on a 30 € invoice leaves
`subtotal` at −20 €, and `invoice_total` returns −24.20 € with tax: a credit the caller would pay out.

`tests/billing/test_coupons.py` only tests `redeem`, so nothing covers this.

```change
Clamp the subtotal at zero after the
coupon: max(subtotal - amount, 0).
Add a test with a coupon larger than
the invoice, expecting a total of 0.
```
