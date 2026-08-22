## What this changes

<!-- One or two sentences. Link the issue if there is one. -->

## Why

<!-- What goes wrong without it. For a bug fix, the case that used to produce the
     wrong verdict. -->

## Checklist

- [ ] A test fails without this change and passes with it
- [ ] `make test` and `make lint` pass
- [ ] No new runtime dependency (sqlglot is the only one)
- [ ] Nothing here can return SAFE when columns could not be extracted
- [ ] I have run this and can explain why it is correct
- [ ] Non-trivial change: an issue was agreed first (see CONTRIBUTING)

<!--
The SAFE box is the rule the project is built around: a false warning is fine,
a false safe is not. If columns cannot be determined, return None and let the
caller report a warning.

Adding a SQL pattern? A fixture in tests/fixtures/ with the expected column list
in a header comment is usually the whole change. Use the bookstore demo domain
the other fixtures use (orders, customers, books) rather than names from a real
warehouse.
-->
