---
name: <rule-slug>
description: <one-line description of what this rule asserts>
tables:
  - <table-name-1>
  - <table-name-2>
fields:
  - <field-name-1>
equation:
  lhs:
    table: <table-name>
    field: <field-name>
    agg: null  # or one of: sum, min, max, mean
    group_by: null  # optional grouping column
  rhs:
    op: <add | sub | mul | div>
    args:
      - table: <table-name>
        field: <field-name>
        agg: sum
        group_by: <grouping-column>
      - table: <table-name>
        field: <field-name>
        agg: null
  tolerance: 0.001
  tolerance_type: relative  # or absolute
source_commit: <git-sha-of-producing-code>
confidence: high  # high | medium | low — how confident we are the extracted equation is correct
last_verified: <YYYY-MM-DD>
---

# Rule: <rule-slug>

## What this asserts
<Plain-English statement of the equation.>

## Where it lives in production
<Path to the SQL/Python that implements the equation, plus a permalink commit URL.>

## When it might break
<Common scenarios: schema change upstream, advance-rate input file missing, etc.>

## Investigation hints
<For the RCA agent — likely root causes when this rule fires.>
