title: Fix 7 mypy errors in superset/utils/date_parser.py
labels: type:lint
---
## Bug Description
mypy reports 7 errors in `superset/utils/date_parser.py`: `Unsupported left operand type for << ("ParserElement")` (lines ~948-994) and `Incompatible return value type (got "ParserElement", expected "ParseResults")` (line ~1006).

`datetime_func = Forward().setName("datetime")` types as ParserElement, so the later `<<=` (defined only on Forward) is rejected; and `datetime_parser()` is annotated `-> ParseResults` but returns a grammar object. Both fixes are annotation-level with no behaviour change: split the call (`x = Forward()` then `x.setName("...")`, same for dateadd/datetrunc/lastday/holiday/datediff) and change the return annotation to ParserElement.

## How to Triage / Test
Run the verify command and confirm the errors are reported. The repository's CI hook does not install pyparsing, which hides these errors (the symbols degrade to Any).

## Verify command
```verify
pip install mypy pyparsing && mypy --check-untyped-defs --ignore-missing-imports --follow-imports=silent superset/utils/date_parser.py
```

## Details
- Severity: medium
- Source: audit of the fork (see the audit report)
