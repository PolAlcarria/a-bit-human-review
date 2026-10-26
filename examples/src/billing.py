"""Invoice totals for the demo review (synthetic code, not part of the tool)."""
from decimal import Decimal

TAX_RATE = Decimal("0.21")


def line_total(price, quantity, discount=0):
    return price * quantity * (1 - discount)


def invoice_total(lines, coupon=None):
    subtotal = sum(line_total(l["price"], l["qty"], l.get("discount", 0)) for l in lines)
    if coupon:
        subtotal -= coupon["amount"]
    tax = subtotal * TAX_RATE
    return round(subtotal + tax, 2)


def refund(invoice, amount):
    if amount > invoice["total"]:
        raise ValueError("refund exceeds invoice total")
    invoice["total"] -= amount
    return invoice
