"""Builds the diff-review demo for record.sh in the scratch folder $1: a git repo with a branch that adds coupons to a
small billing app (synthetic code, not part of the tool), the diff review of that branch, and the code review of
examples/review.md beside it, so the list of reviews has both kinds. Prints the diff review's address."""
import json
import os
import subprocess
import sys
from pathlib import Path

here = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(here))
repo = Path(sys.argv[1]) / 'shop'

BASE = {
    'README.md': '# shop\n\nInvoices, refunds and the retry queue of the demo shop.\n\nRun the tests with `python -m pytest`.\n',
    'src/billing/invoice.py': '''"""Invoice totals."""
from decimal import Decimal

TAX_RATE = Decimal("0.21")


def line_total(price, quantity, discount=0):
    return price * quantity * (1 - discount)


def invoice_total(lines):
    subtotal = sum(line_total(l["price"], l["qty"], l.get("discount", 0)) for l in lines)
    tax = subtotal * TAX_RATE
    return round(subtotal + tax, 2)


def refund(invoice, amount):
    if amount > invoice["total"]:
        raise ValueError("refund exceeds invoice total")
    invoice["total"] -= amount
    return invoice
''',
    'src/billing/legacy_discounts.py': '''"""Discounts from the old checkout. Replaced by coupons."""

SEASONAL = {"summer": 0.1, "winter": 0.15}


def seasonal_discount(season):
    return SEASONAL.get(season, 0)
''',
    'src/queue/retry.py': '''"""Send a payload to the payments service, retrying on connection errors."""
import time


def send_with_retry(client, payload, attempts=3):
    for attempt in range(attempts):
        try:
            return client.send(payload)
        except ConnectionError:
            time.sleep(1)
    return None
''',
    'tests/billing/test_invoice.py': '''from decimal import Decimal

from billing.invoice import invoice_total


def test_total_adds_tax():
    assert invoice_total([{"price": Decimal("10"), "qty": 2}]) == Decimal("24.20")
''',
}

BRANCH = {
    'README.md': '# shop\n\nInvoices, refunds, coupons and the retry queue of the demo shop.\n\nRun the tests with '
                 '`python -m pytest`. Coupons are described in [docs/coupons.md](docs/coupons.md).\n',
    'src/billing/invoice.py': '''"""Invoice totals."""
from decimal import Decimal

from billing.coupons import redeem

TAX_RATE = Decimal("0.21")


def line_total(price, quantity, discount=0):
    return price * quantity * (1 - discount)


def invoice_total(lines, coupon=None):
    subtotal = sum(line_total(l["price"], l["qty"], l.get("discount", 0)) for l in lines)
    if coupon:
        amount = redeem(coupon)
        subtotal -= amount
    tax = subtotal * TAX_RATE
    return round(subtotal + tax, 2)


def refund(invoice, amount):
    if amount > invoice["total"]:
        raise ValueError("refund exceeds invoice total")
    invoice["total"] -= amount
    return invoice
''',
    'src/billing/coupons.py': '''"""Coupons: a fixed amount off an invoice, once, before a date."""
import datetime as dt
from decimal import Decimal


class CouponError(ValueError):
    pass


def redeem(coupon, today=None):
    today = today or dt.date.today()
    if coupon.get("used"):
        raise CouponError(f"coupon {coupon['code']} was already used")
    if coupon["expires"] < today:
        raise CouponError(f"coupon {coupon['code']} expired on {coupon['expires']}")
    coupon["used"] = True
    return Decimal(str(coupon["amount"]))
''',
    'src/queue/retry.py': '''"""Send a payload to the payments service, retrying on connection errors."""
import time

BACKOFF_SECONDS = 0.5


def send_with_retry(client, payload, attempts=5):
    for attempt in range(attempts):
        try:
            return client.send(payload)
        except ConnectionError:
            time.sleep(BACKOFF_SECONDS * 2 ** attempt)
    return None
''',
    'tests/billing/test_coupons.py': '''import datetime as dt
from decimal import Decimal

import pytest

from billing.coupons import CouponError, redeem

TODAY = dt.date(2026, 11, 3)


def coupon(**kw):
    return {"code": "WELCOME10", "amount": 10, "expires": dt.date(2026, 12, 31), **kw}


def test_redeem_gives_the_amount():
    assert redeem(coupon(), TODAY) == Decimal("10")


def test_a_coupon_is_used_once():
    c = coupon()
    redeem(c, TODAY)
    with pytest.raises(CouponError):
        redeem(c, TODAY)


def test_an_expired_coupon_is_refused():
    with pytest.raises(CouponError):
        redeem(coupon(expires=dt.date(2026, 1, 1)), TODAY)
''',
    'docs/coupons.md': '''# Coupons

A coupon takes a fixed amount off an invoice, before tax. Each coupon works once and only until its date.

| Field | What |
|---|---|
| `code` | What the customer types, e.g. `WELCOME10` |
| `amount` | The amount off, in the invoice's currency |
| `expires` | The last day it works |
''',
    'tests/billing/test_invoice.py': BASE['tests/billing/test_invoice.py'],
}


def git(*a):
    return subprocess.run(['git', '-C', str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()


def write(files):
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)


repo.mkdir(parents=True)
git('init', '-q', '-b', 'main')
git('config', 'user.email', 'demo@example.com')
git('config', 'user.name', 'Demo')
git('remote', 'add', 'origin', 'https://github.com/acme/shop.git')
write(BASE)
git('add', '-A')
git('commit', '-qm', 'Base')
git('update-ref', 'refs/remotes/origin/main', 'HEAD')
git('checkout', '-qb', 'coupons')
(repo / 'src/billing/legacy_discounts.py').unlink()
write(BRANCH)
git('add', '-A')
git('commit', '-qm', 'Add coupons, and back off between retries')

# The session that wrote the branch: its transcript, and open in Claude Code, so Send changes to session can reach it.
home = Path(os.environ['HOME'])
sid = 'demo-0002'
(home / '.claude/projects/shop').mkdir(parents=True, exist_ok=True)
(home / f'.claude/projects/shop/{sid}.jsonl').write_text(json.dumps(
    {'type': 'user', 'message': {'role': 'user', 'content': 'Add coupons to invoices'}, 'cwd': str(repo), 'sessionId': sid}) + '\n')
(home / '.claude/sessions').mkdir(parents=True, exist_ok=True)
(home / f'.claude/sessions/{os.getppid()}.json').write_text(json.dumps(
    {'sessionId': sid, 'pid': os.getppid(), 'cwd': str(repo), 'name': 'coupons', 'status': 'idle'}))

import hook, render, triage  # noqa: E402 — after sys.path and the environment are set

# A code review too, so the list shows its two tabs with something in each. The list counts only pages named by their
# date, as real ones are.
spec = hook.spec_from_report(here / 'examples/review.md')
spec['out'] = str(render.store.html_path('20261103-174210-review-of-the-billing-and-queue-changes'))
render.render(spec)
out = render.render({'kind': 'diff', 'cwd': str(repo), 'base': 'main', 'session_id': sid,
                     'out': str(render.store.html_path('20261104-091530-diff-review-coupons'))})
assert triage.ensure_server()
print(triage.page_url(out))
