<!-- shape: severity headings with plain list items under them -->
# Review of PR #4242

## Critical

- Missing rounding in `src/app/billing.py:42` when the invoice total is negative.
- Race in the retry loop at src/app/queue.py:10-14.

## Suggestions

- Rename `calc` in src/app/billing.py:88 for clarity.
