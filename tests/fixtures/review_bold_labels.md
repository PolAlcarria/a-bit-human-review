<!-- shape: bold severity labels with bold finding titles under them -->
**Must-fix:**

1. **Unbounded query** in src/app/reports.py:120 loads every row.
   It also ignores the tenant filter.

**Nit:**

- **Typo in docstring** — src/app/reports.py:7
